#!/usr/bin/env python3
"""video_make_ffmpeg.py — 用 ffmpeg 拼接视频（比 moviepy 快 10 倍）

用法:
  python video_make_ffmpeg.py <director.yaml>

原理: 图片+音频 → ffmpeg 逐段渲染 → concat 拼接 → 字幕 → BGM混音
"""

import sys, os, subprocess, tempfile, shutil, yaml, json, re, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P

# 同目录模块：Pexels 素材 / ASS 字幕 / xfade 拼接
from pexels_materials import ensure_scene_videos, ensure_scene_photo
from ass_subtitle import srt_file_to_cues, assemble_global_ass
import xfade_join
from encoder import run_encode

PYTHON = P.PYTHON
TTS    = str(P.ROOT / 'tts_express.py')
FONT   = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
W, H   = 1080, 1920   # 默认 9:16（抖音竖屏）；由 director output.size 覆盖

# 镜头化 + 转场参数
VIDEO_FPS     = 25
XFADE_FADE    = 0.16   # 交叉溶解（帧量化，与 render_remix 一致）
AUDIO_FADE    = 0.15   # 每场景音频淡入淡出
MAX_SHOT_DUR  = 5.0    # 单镜头最大时长（对齐 MPT max_clip_duration）
SCENE_TAIL    = 0.3    # 场景末尾停顿（旁白读完 + 0.3s 再转场）
CRF           = 20     # 统一码率控制（多代编码防质量衰减）
VIDEO_EXTS    = {'.mp4', '.mov', '.webm', '.mkv', '.avi'}

# 提示词模板路径
PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
DIRECTOR_PROMPT_FILE = os.path.join(PROMPT_DIR, 'director_optimize.md')


def load(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def _normalize_speed(speed):
    """归一化语速值：LLM 可能输出 中速/正常/标准 → 中，快速/极快 → 快"""
    if not speed:
        return '中'
    s = speed.strip()
    if s in ('快', '慢', '中'):
        return s
    if s in ('快速', '极快', '飞快', '很快'):
        return '快'
    if s in ('慢速', '极慢', '很慢'):
        return '慢'
    return '中'


def _batch_tts(scene_inventory, vc, tmp_dir):
    """整片旁白一次性交给一个 TTS 进程合成。

    动机（实测）：CosyVoice 每次加载模型约 21.1s、合成约 8.4s/段，
    而 synth_scene 是每段起一个进程 —— 等于每段白付一次加载费。
    8 场景 236s → 88s（省 63%）。

    这只是提速手段：任何失败都退回逐场景合成，成片内容不变。
    不传 speed —— spk_id 的两条分支都不用它（与 main() 一致）。
    """
    jobs = []
    for i, item in enumerate(scene_inventory):
        sc = item['sc']
        sid = sc['id'] if sc else (i + 1)
        jobs.append({
            'text':    convert_spoken_numbers(item['spoken']),
            'spk_id':  vc['spk_id'],
            'express': bool(vc.get('express', False)),
            'out':     os.path.join(tmp_dir, f's{sid}.wav'),
        })
    if not jobs:
        return
    print(f"   ⚡ 批量合成 {len(jobs)} 段旁白（模型只加载一次）")
    try:
        r = subprocess.run([PYTHON, TTS, '--batch', '-'],
                           input=json.dumps(jobs, ensure_ascii=False),
                           capture_output=True, text=True,
                           timeout=max(900, len(jobs) * 120))
    except Exception as e:                                      # noqa: BLE001
        print(f"   ⚠️ 批量合成异常（{e}），退回逐场景合成")
        return
    if r.returncode != 0:
        print(f"   ⚠️ 批量合成失败（退出码 {r.returncode}），退回逐场景合成")
        tail = ((r.stderr or '') + (r.stdout or '')).strip().splitlines()[-4:]
        for ln in tail:
            print(f"      {ln}")
        return
    done = sum(1 for j in jobs
               if os.path.exists(j['out']) and os.path.getsize(j['out']) > 0)
    print(f"   ⚡ 批量合成完成 {done}/{len(jobs)} 段（缺的会逐场景补）")


def synth_scene(text, spk_id, express, speed, out_path):
    # 批量预合成已产出则直接复用（见 _batch_tts）。必须非空才算数，
    # 免得半截文件被当成成品继续走。
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return
    cmd = [PYTHON, TTS, text, '--spk-id', spk_id, '--output', out_path]
    if express:
        cmd.append('--express')
    speed_norm = _normalize_speed(speed)
    if speed_norm and speed_norm != '中':
        cmd.extend(['--speed', speed_norm])
    subprocess.run(cmd, check=True, capture_output=True)


def get_duration(wav):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', wav],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def clean_subtitle(text):
    """清理字幕显示文本：保留换行，句尾不保留标点"""
    import re
    # 保留换行 \n，只去除多余空白
    text = re.sub(r'[^\S\n]+', '', text)
    # 连续重复标点去重
    text = re.sub(r'([，、；：。！？]){2,}', r'\1', text)
    # 每行末尾去掉标点（字幕句尾不需要 。，！？等）
    lines = text.split('\n')
    lines = [re.sub(r'[，、；：。！？]+$', '', l) for l in lines]
    text = '\n'.join(lines)
    return text.strip()


def split_phrases(text, max_chars=10):
    """将文本按标点分成字幕短句，每句 max_chars 字左右"""
    import re
    parts = re.split(r'(?<=[，、；：。！？])', text)
    phrases = [p.strip() for p in parts if p.strip()]
    merged = []
    buf = ''
    for p in phrases:
        buf += p
        if len(re.sub(r'[，、；：。！？\s]', '', buf)) >= max_chars:
            merged.append(buf)
            buf = ''
    if buf:
        if merged:
            merged[-1] += buf
        else:
            merged.append(buf)
    # 确保每句不太长
    result = []
    for p in merged:
        clean = re.sub(r'[，、；：。！？\s]', '', p)
        if len(clean) > max_chars + 5:
            # 强行再拆
            mid = len(p) // 2
            for ch in '，；、' :
                pos = p[:mid].rfind(ch)
                if pos > 2:
                    result.append(p[:pos+1])
                    p = p[pos+1:]
                    break
        result.append(p)
    return result if result else [text]


def convert_spoken_numbers(text):
    """语音文本中数字转中文读法：211→二百一十一，2.0→二点零，16.37万→十六万三千七百"""
    DIGITS = '零一二三四五六七八九'
    UNITS  = ['', '十', '百', '千']

    def _int_to_cn(n):
        """0~99999 整数转中文"""
        if n == 0:
            return '零'
        s = str(n)
        result = ''
        for i, ch in enumerate(s):
            d = int(ch)
            pos = len(s) - 1 - i
            if d == 0:
                if result and not result.endswith('零'):
                    result += '零'
            else:
                result += DIGITS[d] + UNITS[pos]
        result = re.sub(r'零+', '零', result).rstrip('零')
        if result.startswith('一十'):
            result = result[1:]
        return result if result else '零'

    def _decimal_to_cn(m):
        """X.Y → 二点零 / 十二点五"""
        ip = m.group(1)
        dp = m.group(2)
        return _int_to_cn(int(ip)) + '点' + ''.join(DIGITS[int(d)] for d in dp)

    # 0. 去掉数字中的千分位逗号：50,000 → 50000
    text = re.sub(r'(?<=\d),(?=\d)', '', text)

    # 1. X.Y万 → 十六万三千七百
    def _wan_price(m):
        int_p = m.group(1)
        dec_p = m.group(2)
        int_cn = _int_to_cn(int(int_p))
        # 0.37万 = 3700
        dec_val = int(dec_p) * 100
        if dec_val > 0:
            dec_cn = _int_to_cn(dec_val)
            return int_cn + '万' + dec_cn
        return int_cn + '万'
    text = re.sub(r'(\d{1,2})\.(\d{1,2})万', _wan_price, text)

    # 2. 独立整数[万] → 五万 / 十二万
    def _int_wan(m):
        return _int_to_cn(int(m.group(1))) + '万'
    text = re.sub(r'(?<=[^\w.])(\d+)万(?![\w.])', _int_wan, text, flags=re.ASCII)
    text = re.sub(r'^(\d+)万(?![\w.])', _int_wan, text)

    # 3. 其他 X.Y 小数
    text = re.sub(r'(\d+)\.(\d+)', _decimal_to_cn, text)

    # 4. 短整数（1~4位）后跟中文 → 211匹 → 二百一十一匹；7月16日 → 七月十六日
    def _standalone_int(m):
        return _int_to_cn(int(m.group(1))) + m.group(2)
    text = re.sub(r'(?<=[^\w.])(\d{1,4})([一-鿿])', _standalone_int, text, flags=re.ASCII)
    text = re.sub(r'^(\d{1,4})([一-鿿])', _standalone_int, text)

    # 5. 数字+字母后缀（508L → 五零八L；X.Y 已由规则3消费，1位数字不处理）
    def _digit_letter(m):
        return ''.join(DIGITS[int(d)] for d in m.group(1)) + m.group(2)
    text = re.sub(r'(?<=[^\w.])(\d{2,})([A-Za-z]+)', _digit_letter, text, flags=re.ASCII)
    text = re.sub(r'^(\d{2,})([A-Za-z]+)', _digit_letter, text, flags=re.ASCII)

    return text


def _is_cjk(ch):
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or
            0x3400 <= cp <= 0x4DBF or
            0xF900 <= cp <= 0xFAFF or
            0x3000 <= cp <= 0x303F or
            0xFF00 <= cp <= 0xFFEF)


def _find_break(remaining, max_chars):
    """找 natural 断点，避免单字孤行（不吞孤行，确保每行 >= 3 字）"""
    BREAK_PUNCT = '，、；：。！？'
    n = len(remaining)
    if n <= max_chars + 2:
        return remaining, ''

    # 优先找标点断点（在 max_chars 范围内，且断后行不少于 3 字）
    for bp in BREAK_PUNCT:
        best = -1
        for pos in range(min(max_chars, n - 3)):
            if remaining[pos] == bp:
                best = pos
        if best >= 3:
            return remaining[:best + 1], remaining[best + 1:].lstrip()

    # 其次找 CJK/非CJK 边界
    for i in range(min(max_chars - 1, n - 4), max_chars // 2, -1):
        if _is_cjk(remaining[i]) != _is_cjk(remaining[i + 1]):
            return remaining[:i + 1], remaining[i + 1:].lstrip()

    # 硬断：在 max_chars 处断，带上孤行
    cut = min(max_chars, n)
    if cut < 3:
        return remaining, ''
    tail = remaining[cut:].lstrip()
    if tail and len(tail) <= 3:
        return remaining, ''  # 带走上行
    return remaining[:cut], tail


def wrap_lines(text, max_chars=10, max_lines=10):
    """字幕折行：每行约 max_chars 字，不会截断文本"""
    text = text.strip()
    if len(text) <= max_chars + 2:
        return text
    lines = []
    remaining = text
    while remaining and len(lines) < max_lines:
        line, remaining = _find_break(remaining, max_chars)
        lines.append(line)

    # 如有剩余（max_lines 上限或 _find_break 返回全部），全追加到末行
    if remaining:
        if lines:
            lines[-1] += remaining
        else:
            lines.append(remaining)

    return '\n'.join(lines)


def make_srt_phrases(phrases, total_dur):
    lines = []
    if len(phrases) == 0:
        return ''
    import re
    def phrase_weight(text):
        w = len(text)
        w += len(re.findall(r'[，、；：]', text)) * 0.3
        w += len(re.findall(r'[。！？]', text)) * 0.6
        return w
    weights = [max(phrase_weight(p), 0.5) for p in phrases]
    total_w = sum(weights)
    t = 0.0
    for i, p in enumerate(phrases):
        dur = (weights[i] / total_w) * total_dur if total_w > 0 else total_dur / len(phrases)
        dur = max(dur, 0.5)
        start = t
        end   = min(t + dur, total_dur)
        t = end
        def ts(sec):
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = sec % 60
            return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
        wrapped = wrap_lines(p, max_chars=10)
        display = clean_subtitle(wrapped)
        if not display:
            continue
        lines.append(f'{i+1}')
        lines.append(f'{ts(start)} --> {ts(end)}')
        lines.append(display)
        lines.append('')
    return '\n'.join(lines)



# ============================================================
# Whisper 字幕对齐（精确音频→字幕时间戳）
# ============================================================
def fmt_ts(sec):
    """秒 → SRT 时间戳格式 HH:MM:SS,mmm"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')


def make_aligned_srt(audio_path, display_text, align_text, total_dur):
    """用 whisper 词级时间戳精确对齐字幕与音频

    display_text: 字幕显示的文本（带数字，如 1.8T）
    align_text:   用于对齐的文本（中文数字，如 一点八T，与 TTS 输出一致）

    原理：
        1. 对 TTS 生成的音频跑 whisper（带 word_timestamps）→ 真实语音时间轴
        2. display_text 按标点分短语，逐段在 whisper 词序列中做字符重叠匹配
           （容错 TTS 表达性发音导致的 whisper 个别错字，如 首→守）
        3. 匹配不到的短语按字符比例回退定位
        4. 生成精确 SRT — 显示 narration，时间来自真实语音位置

    返回 SRT 字符串，失败返回 None（调用方 fallback 到等比例分配）
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None

    try:
        model = WhisperModel(
            str(P.WHISPER_MODEL),
            device='cuda', compute_type='float16')
    except Exception:
        try:
            model = WhisperModel(
                str(P.WHISPER_MODEL),
                device='cpu', compute_type='int8')
        except Exception:
            return None

    try:
        segs, _ = model.transcribe(
            audio_path, language='zh',
            word_timestamps=True, vad_filter=True, beam_size=5)
        segments = list(segs)
    except Exception:
        return None

    if not segments:
        return None

    # 收集所有词级时间戳
    words = []
    for seg in segments:
        for w in (seg.words or []):
            t = w.word.strip()
            if t:
                words.append({'text': t, 'start': w.start, 'end': w.end})

    if not words:
        return None

    def clean_str(t):
        return re.sub(r'[^一-鿿\w]', '', t)

    # 每个 whisper 词的干净字符（用于重叠打分和字符累计）
    word_chars = [clean_str(w['text']) for w in words]
    word_char_ends = []
    total_wc = 0
    for c in word_chars:
        total_wc += len(c)
        word_char_ends.append(total_wc)

    def _find_word_at_char_ratio(ratio, total_wc, word_ends):
        """在 whisper 词序列中找到字符比例位置对应的词索引"""
        target = ratio * total_wc
        for idx, ce in enumerate(word_ends):
            if target < ce:
                return idx
        return len(word_ends) - 1

    def _score_window(phrase_chars, window_chars):
        """字符多重集重叠：窗口内与短语相同的字符数（容错错位/错字）"""
        from collections import Counter
        pc = Counter(phrase_chars)
        wc = Counter(window_chars)
        return sum(min(pc[c], wc.get(c, 0)) for c in pc)

    def _match_phrase_in_words(phrase, from_idx):
        """在 whisper 词序列中匹配短语 → (i, j)；找不到返回 None

        单调前进（from_idx 起）、取第一个达标窗口，避免跳过前半段；
        容错 = 短语长度 - 2 个错字/多字
        """
        L = len(phrase)
        n = len(words)
        for need in (L, max(2, L - 2)):   # 先全字匹配，再放宽容错
            for i in range(from_idx, n):
                acc = 0
                for j in range(i, min(n, i + 12)):
                    acc += len(word_chars[j])
                    if acc > L * 3:
                        break
                    s = _score_window(phrase, ''.join(word_chars[i:j + 1]))
                    if s >= need:
                        return i, j
        return None

    phrases = split_phrases(display_text)
    total_clean = sum(len(clean_str(p)) for p in phrases)
    if total_clean < 2:
        return None

    lines = []
    prev_end = 0.0
    prev_j = -1
    char_sofar = 0

    for i, phrase in enumerate(phrases):
        p_clean = clean_str(phrase)
        if not p_clean:
            continue

        # 主路径：字符重叠匹配真实语音位置
        m = _match_phrase_in_words(p_clean, prev_j + 1)
        if m:
            idx_start, idx_end = m
        elif total_wc > 0:
            # 回退：字符比例定位
            r_start = char_sofar / total_clean
            r_end   = (char_sofar + len(p_clean)) / total_clean
            idx_start = _find_word_at_char_ratio(r_start, total_wc, word_char_ends)
            idx_end   = _find_word_at_char_ratio(r_end, total_wc, word_char_ends)
        else:
            idx_start, idx_end = 0, len(words) - 1

        t_start = words[idx_start]['start']
        t_end   = words[idx_end]['end']

        # 第一个字幕强制从 0 开始
        if i == 0:
            t_start = 0.0

        # 无缝衔接：不早于上一个结束
        if t_start < prev_end:
            t_start = prev_end
        # 最短显示时间
        if t_end - t_start < 0.5:
            t_end = min(t_start + 0.5, total_dur)

        char_sofar += len(p_clean)
        prev_end = t_end
        prev_j = idx_end

        display = wrap_lines(phrase.strip(), max_chars=10)
        display = clean_subtitle(display)
        if not display:
            continue

        lines.append(f'{i+1}')
        lines.append(f'{fmt_ts(t_start)} --> {fmt_ts(t_end)}')
        lines.append(display)
        lines.append('')

    return '\n'.join(lines) if lines else None


def make_subtitle(narration, total_dur, tmp_dir, sid, audio_path=None, spoken=None):
    """生成 SRT 字幕：优先 whisper 对齐，失败则等比例分配

    参数:
        narration: 字幕显示文本（带数字，如 1.8T）
        spoken:    用于对齐的 TTS 实际文本（中文数字，如 一点八T）
                   为 None 时用 narration 做对齐
    """
    if audio_path and os.path.exists(audio_path):
        align_text = spoken or narration  # 用 TTS 实际念的文本做对齐
        aligned = make_aligned_srt(audio_path, narration, align_text, total_dur)
        if aligned:
            srt_file = os.path.join(tmp_dir, f's{sid}.srt')
            with open(srt_file, 'w', encoding='utf-8') as f:
                f.write(aligned)
            return srt_file

    # Fallback: 等比例分配
    phrases = split_phrases(narration)
    srt_text = make_srt_phrases(phrases, total_dur)
    srt_file = os.path.join(tmp_dir, f's{sid}.srt')
    with open(srt_file, 'w', encoding='utf-8') as f:
        f.write(srt_text)
    return srt_file


# ============================================================
# 本地视觉模型图片分析器（通过子进程调用独立 vlm-env）
# ============================================================
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import llm as _llm, vlm as _vlm   # 密钥不再硬编码，见 pipeline_config.py
# 用户素材目录（`素材/` + 存量 `图片/`）—— 约定只有一份，见 pipeline_config.MANUAL_DIRS
from pipeline_config import MANUAL_DIRS                               # noqa: E402
LLM_API_URL, LLM_API_KEY, LLM_MODEL = _llm()

VLM_PYTHON = P.VLM_PYTHON
VLM_SCRIPT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_analyze.py")
# 权重目录：与 vlm_analyze.py 的 MODEL_DIR 同一个来源（pipeline_config.vlm()），
# 两边不会再各写一个路径。配置在 config.toml 的 [vlm] 段，页面上可改。
VLM_MODEL_DIR = _vlm()['model_dir']
VLM_ENABLED = _vlm()['enabled']
VLM_TIMEOUT = _vlm()['timeout']
def is_manual_image(rel_path):
    """这张图是用户手动上传的吗？—— VLM 只跑手动上传的图片。

    为什么必须卡这一条：discover_images() 是**递归**扫整个项目目录的，
    上面那些自动产出的图会被一起捞进来：
      · `_keyframes/scene_0001.jpg` —— analyze_video.py 从原片抽的帧；
      · `.vlm_tmp.jpg` —— 本模块自己缩放出的临时图（另有名字过滤）。
    本地 VLM 一张图约 10s + 4.2GB 显存，几十张帧喂进去能把出片时间翻几倍，
    而它们的"分析结果"还要挤占上下文。手动上传的图才值得这个开销。

    判据用 rel_path 的第一段而不是绝对路径：项目目录是 `分类/项目名/`，
    绝对路径里含有仓库位置和分类名，写死任何一个都会在换机器/换项目时失效。
    """
    head = os.path.normpath(str(rel_path or '')).split(os.sep, 1)[0]
    return head in MANUAL_DIRS


class ImageAnalyzer:
    def __init__(self):
        self._available = None

    def is_available(self):
        if self._available is None:
            self._available = (
                VLM_ENABLED
                and os.path.isdir(VLM_MODEL_DIR)
                and os.path.exists(VLM_PYTHON)
                and os.path.exists(VLM_SCRIPT)
            )
        return self._available

    def load(self):
        ok = self.is_available()
        if not ok:
            why = ('配置里关掉了 [vlm] enabled' if not VLM_ENABLED
                   else f'{VLM_MODEL_DIR} 或 {VLM_PYTHON} 不存在')
            print(f"   ℹ️ 视觉模型不可用（{why}），跳过图片分析")
        return ok

    def analyze(self, image_path, narration=""):
        if not self.is_available():
            return {}
        try:
            cmd = [
                VLM_PYTHON, VLM_SCRIPT,
                image_path,
                "--narration", narration[:200],
                "--output-format", "json",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=VLM_TIMEOUT)
            if r.returncode != 0:
                print(f"      ⚠️ VLM 进程异常: {r.stderr.strip()[:80]}")
                return {}
            result = json.loads(r.stdout)
            if "error" in result:
                print(f"      ⚠️ VLM 错误: {result['error']}")
                return {}
            return result
        except subprocess.TimeoutExpired:
            print(f"      ⚠️ VLM 超时（>{VLM_TIMEOUT}s），跳过")
            return {}
        except Exception as e:
            print(f"      ⚠️ VLM 调用失败: {e}")
            return {}


# ============================================================
# 工具：发现工作目录下的所有图片
# ============================================================
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff'}

def discover_images(work_dir):
    """扫描工作目录，返回 [{abs_path, rel_path}]，按文件名排序"""
    imgs = []
    for root, dirs, files in os.walk(work_dir):
        # 跳过隐藏目录和临时目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('vm_')]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext not in IMG_EXTS:
                continue
            # 跳过 VLM 临时缩放文件
            if '.vlm_tmp.' in f:
                continue
            abs_path = os.path.normpath(os.path.join(root, f))
            rel_path = os.path.relpath(abs_path, work_dir)
            imgs.append({'abs_path': abs_path, 'rel_path': rel_path})
    return imgs


def resolve_img_path(vsrc, work_dir):
    """将 yaml 里的图片路径转为绝对路径"""
    if not vsrc:
        return None
    p = vsrc if os.path.isabs(vsrc) else os.path.join(work_dir, vsrc)
    return os.path.normpath(p)


def manual_images_in_scenes(work_dir, scenes):
    """导演文件里引用的、**手动上传**的图片，返回 [(abs_path, rel_path)]。

    给 VLM 开关用：一张都没有就完全不去碰视觉模型（也就不必查可用性、
    不必打「模型不可用」那行废话）。搜索:前缀 与视频一律跳过 ——
    那不是图片，由素材阶段处理。
    """
    out = []
    for sc in scenes:
        vsrc = (sc.get('visuals') or {}).get('source', '')
        abs_vsrc = resolve_img_path(vsrc, work_dir)
        if not abs_vsrc or not os.path.exists(abs_vsrc):
            continue
        if os.path.splitext(abs_vsrc)[1].lower() in VIDEO_EXTS:
            continue
        if is_manual_image(os.path.relpath(abs_vsrc, work_dir)):
            out.append((abs_vsrc, os.path.relpath(abs_vsrc, work_dir)))
    return out


def build_image_inventory(work_dir, scenes):
    """构建完整图片清单：工作目录下所有图片 + 关联场景信息

    返回 [{abs_path, rel_path, scene, narration, spoken, mood, speed}]
    scene=None 表示该图片不在原始 director 中，是新增的
    """
    all_imgs = discover_images(work_dir)

    # 原场景图片路径 → 场景映射
    scene_img_map = {}
    for sc in scenes:
        vsrc = sc.get('visuals', {}).get('source', '')
        abs_vsrc = resolve_img_path(vsrc, work_dir)
        if abs_vsrc:
            scene_img_map[abs_vsrc] = sc

    inventory = []
    for img in all_imgs:
        sc = scene_img_map.get(img['abs_path'])
        inventory.append({
            **img,
            'scene': sc,
            'has_scene': sc is not None,
            'narration': sc['narration'] if sc else '',
            'spoken': sc.get('spoken', sc['narration']) if sc else '',
            'mood': sc.get('mood', '平实') if sc else '',
            'speed': sc.get('speed', '中') if sc else '',
        })

    return inventory


def make_tmp_jpg(image_path, tmp_dir, key):
    """PIL 处理图片 → WxH JPG（跟随输出尺寸），返回临时路径"""
    from PIL import Image
    ip = os.path.join(tmp_dir, f'img_{key}.jpg')
    with Image.open(image_path) as img:
        img = img.convert('RGB')
        w, h = img.size
        ratio = W / w
        nh = int(h * ratio)
        img = img.resize((W, nh), Image.LANCZOS)
        if nh > H:
            top = (nh - H) // 2
            img = img.crop((0, top, W, top + H))
        elif nh < H:
            pad = Image.new('RGB', (W, H), (15, 15, 30))
            pad.paste(img, (0, (H - nh) // 2))
            img = pad
        if img.width % 2 != 0:
            img = img.crop((0, 0, img.width - 1, img.height))
        if img.height % 2 != 0:
            img = img.crop((0, 0, img.width, img.height - 1))
        img.save(ip, quality=85)
    return ip


# ============================================================
# 导演编排：DeepSeek 基于图片内容重新设计场景
# ============================================================
def optimize_scenes_with_llm(director, image_inventory, work_dir):
    """把全部图片分析 + 原 yaml 给 DeepSeek → 输出全新 director_optimized.yaml

    参数:
        director: 原始导演字典
        image_inventory: build_image_inventory() 的返回，每项含 VLM 分析结果
        work_dir: 工作目录

    返回:
        str — 优化后的 yaml 文件路径，或 None
    """
    if not LLM_API_KEY:
        print("   ⚠️ 未配置 DeepSeek API key，跳过导演编排")
        return None

    # 加载提示词模板
    if not os.path.exists(DIRECTOR_PROMPT_FILE):
        print(f"   ⚠️ 提示词文件不存在: {DIRECTOR_PROMPT_FILE}")
        return None
    with open(DIRECTOR_PROMPT_FILE, encoding='utf-8') as f:
        prompt_template = f.read()

    import copy

    # 构建场景描述（已有场景 + VLM 分析）
    existing = [inv for inv in image_inventory if inv['has_scene']]
    new_only = [inv for inv in image_inventory if not inv['has_scene']]

    scene_lines = ["## 已有场景"]
    for inv in existing:
        v = inv.get('vlm', {})
        scene_lines.append(
            f"  图片: {inv['rel_path']}\n"
            f"    原字幕: {inv['narration'][:100]}\n"
            f"    原语音: {inv['spoken'][:100]}\n"
            f"    画面: {v.get('visual_key', '?')}\n"
            f"    画面文字: {v.get('text_content', '')[:80]}\n"
            f"    主体位置: {v.get('subject_pos', 'center')}\n"
            f"    原情绪: {inv['mood']} | 原语速: {inv['speed']}"
        )

    if new_only:
        scene_lines.append("\n## 新增图片（需你创作文案）")
        for inv in new_only:
            v = inv.get('vlm', {})
            scene_lines.append(
                f"  图片: {inv['rel_path']}\n"
                f"    画面: {v.get('visual_key', '?')}\n"
                f"    画面文字: {v.get('text_content', '')[:80]}\n"
                f"    主体位置: {v.get('subject_pos', 'center')}"
            )

    # 填充模板
    prompt = prompt_template.replace('{project_title}', director.get('project', {}).get('title', '未命名'))
    prompt = prompt.replace('{scene_info}', '\n'.join(scene_lines))

    # 提取 system prompt（模板第一段 # 标题后的说明文字）
    system_prompt = "你是一个短视频导演，擅长根据画面内容优化视频节奏和场景顺序。只输出 YAML，不要多余内容。"

    try:
        import requests
        resp = requests.post(
            LLM_API_URL,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.8,
                # 推理模型（如 deepseek-v4-flash）的思考 token 计入 max_tokens，
                # 4096 会被推理吃光导致 content 为空；8192 保证正式输出。
                # 导演编排保留轻量思考（effort=low）：关闭思考会降低指令遵循度
                "max_tokens": 8192,
                "reasoning_effort": "low",
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=120,
        )
        if resp.status_code != 200:
            print(f"   ⚠️ LLM 编排失败: HTTP {resp.status_code}")
            return None

        content = resp.json()["choices"][0]["message"]["content"].strip()

        # 去掉 markdown 代码块
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            content = content.rsplit("```", 1)[0]
        content = content.strip()

        # 记录前 200 字符用于调试
        print(f"      📄 LLM 输出预览: {content[:200]}...")

        # 尝试解析 YAML，失败时打印完整内容
        try:
            parsed = yaml.safe_load(content)
        except Exception as ye:
            print(f"      ⚠️ YAML 解析失败，尝试清理特殊字符后重试")
            # 替换可能导致 YAML 解析失败的字符
            cleaned = content.replace('°', '度').replace('·', '·')
            parsed = yaml.safe_load(cleaned)
        if isinstance(parsed, dict) and 'scenes' in parsed:
            new_scenes = parsed['scenes']
        elif isinstance(parsed, list):
            new_scenes = parsed
        else:
            print(f"   ⚠️ LLM 输出格式异常: {type(parsed).__name__}")
            return None

        if not isinstance(new_scenes, list) or len(new_scenes) == 0:
            print("   ⚠️ LLM 输出非有效场景列表")
            return None

        # 构建新 director
        new_director = copy.deepcopy(director)
        new_director['scenes'] = new_scenes

        out_path = os.path.join(work_dir, 'director_optimized.yaml')
        with open(out_path, 'w', encoding='utf-8') as f:
            yaml.dump(new_director, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        print(f"   💾 已保存: {out_path}  （{len(new_scenes)} 场景）")
        return out_path

    except Exception as e:
        print(f"   ⚠️ LLM 编排异常: {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================
# Phase 1: 图片预处理 + VLM 分析
# ============================================================
def process_image(img, img_analyzer, vlm_ok, tmp_dir):
    """处理单张图片：PIL 缩放 + VLM 分析，返回处理结果

    缩放对所有图片都做（出片要用），VLM **只对手动上传的图片做** ——
    见 is_manual_image()。
    """
    ip = make_tmp_jpg(img['abs_path'], tmp_dir, img['key'])

    vlm_result = {}
    if vlm_ok and is_manual_image(img.get('rel_path', '')):
        narration = img.get('narration', '')
        vlm_result = img_analyzer.analyze(img['abs_path'], narration)
        if vlm_result:
            vk = vlm_result.get('visual_key', '')
            mt = vlm_result.get('match', 'unknown')
            tc = vlm_result.get('text_content', '')
            print(f"      👁️ {vk} | 匹配:{mt}" + (f" | 📝 {tc[:40]}" if tc else ""))

    return ip, vlm_result


def render_shot(shot, out_path):
    """渲染单个镜头（只出视频，无音轨 — 音频由 join 层统一处理）

    shot: {kind: video|image|text, src, dur}
      video: Pexels/本地视频素材，scale+crop 铺满 1920x1080，短于镜头循环
      image: 静态图 Ken Burns 慢推（zoompan 单帧输入不 -loop，d=总帧数）
      text:  深色卡片 + mood 大字（旁白由全局 ASS 字幕承担）
    """
    kind, src, dur = shot['kind'], shot['src'], shot['dur']

    if kind == 'video':
        src_dur = xfade_join.probe_video_dur(src)
        cmd = ['ffmpeg', '-y']
        if src_dur < dur - 0.05:
            cmd += ['-stream_loop', '-1']   # 素材短于镜头 → 循环
        cmd += ['-i', src]
        start = shot.get('src_start', 0.0)
        if start > 0.01 and src_dur > 0:
            # 输出 seek（-i 后）精确切片：多镜头同素材时按时间段取不同画面
            cmd += ['-ss', f'{start:.3f}']
        cmd += ['-vf', (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                        f"crop={W}:{H}:(iw-ow)/2:(ih-oh)/2,"
                        f"fps={VIDEO_FPS},format=yuv420p"),
                '-t', f'{dur:.3f}',
                '-pix_fmt', 'yuv420p',
                out_path]

    elif kind == 'image':
        # 输入放大到 4K 再 zoompan（取整误差减半）+ bicubic 插值 → 推镜更丝滑
        # （zoompan 整数像素取整会导致逐帧位移抖动，实测 2x 放大后帧间差异均匀减半）
        zoom = (f"z='min(1+0.0015*on,1.2)'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={max(int(round(dur * VIDEO_FPS)), 1)}"
                f":s={W}x{H}:fps={VIDEO_FPS}")
        cmd = ['ffmpeg', '-y', '-i', src,
               '-sws_flags', 'bicubic',
               '-vf', (f"scale={2 * W}:{2 * H}:force_original_aspect_ratio=1,"
                       f"pad={2 * W}:{2 * H}:(ow-iw)/2:(oh-ih)/2,"
                       f"zoompan={zoom}"),
               '-pix_fmt', 'yuv420p',
               out_path]

    else:   # text 卡片（无图兜底）
        safe = (shot.get('mood', '平实')).replace("'", "'\\\\''").replace(':', '\\:')
        vf = (f"drawtext=text='{safe}':fontfile={FONT}:fontsize=60"
              f":fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-40")
        cmd = ['ffmpeg', '-y', '-f', 'lavfi',
               '-i', f'color=c=#0f0f23:s={W}x{H}:d={dur:.3f}',
               '-vf', vf,
               '-pix_fmt', 'yuv420p',
               out_path]

    run_encode(cmd, crf=CRF)
    return out_path


def render_final(joined_v, joined_a, bgm_cfg, ass_path, out_path):
    """终稿单 pass：烧全局 ASS 字幕 + BGM 混音 + 输出"""
    total = xfade_join.probe_video_dur(joined_v)
    inputs = ['-i', joined_v, '-i', joined_a]
    fc = f'[0:v]subtitles={ass_path}[vout]'

    bgm_path = (bgm_cfg or {}).get('path', '')
    if bgm_path and os.path.exists(bgm_path):
        vol = bgm_cfg.get('volume', 0.25)
        fi  = bgm_cfg.get('fade_in', 2)
        fo  = bgm_cfg.get('fade_out', 3)
        fo_start = max(0, total - fo)
        inputs += ['-i', bgm_path]
        fc += (f'[1:a]anull[a0];'
               f'[2:a]volume={vol},afade=t=in:d={fi},'
               f'afade=t=out:st={fo_start}:d={fo}[bgm];'
               f'[a0][bgm]amix=inputs=2:duration=first:normalize=0[aout]')
        audio_map, audio_args = '[aout]', ['-c:a', 'aac', '-b:a', '192k']
    else:
        audio_map, audio_args = '1:a', ['-c:a', 'copy']

    # 终稿固定 libx264（hw=False）：多代编码下软编画质最稳、字幕文字边缘更实
    run_encode(['ffmpeg', '-y'] + inputs + [
        '-filter_complex', fc,
        '-map', '[vout]', '-map', audio_map,
        '-pix_fmt', 'yuv420p',
        *audio_args, out_path], crf=CRF, hw=False)


def discover_manual_videos(work_dir):
    """扫描 work_dir 下用户素材目录里的视频文件（按文件名排序），自动发现手动素材。

    `短片/` 也在扫描集里，是为了和 render_remix.discover_clips 对齐 ——
    两个管线的「手动素材」若认不同的目录，就会出现「二创找得到、director 找不到」
    这种只看结果看不出来的偏差。`短片/` 本身是 Pexels 视频的落点，那些是空镜，
    拿来当手动素材轮转兜底正合适。
    """
    out = []
    for sub in ('素材', '短片'):
        d = os.path.join(work_dir, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.startswith('.'):
                continue
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                out.append(os.path.normpath(os.path.join(d, f)))
    return out


def discover_manual_images(work_dir, work_relative=True):
    """扫描用户素材目录里的**图片**（按相对路径排序），返回 rel（或绝对）路径列表。

    只扫 MANUAL_DIRS 这两层，不递归 —— 依据的是 is_manual_image 的同一条判据。
    别拿 discover_images() 顶替：那个是**递归扫整个项目**的，`_keyframes/` 里
    几百张自动抽的帧会被一起捞进来，而这份清单是要喂给 LLM 的。

    这是「用户传了什么」的**唯一**一份实现：image_brief / director_agent /
    text_to_director 都调这里。以前它们各拼一份 `图片/` 路径，改目录名时
    只要漏掉一处，那一处就会静默看不见图。
    """
    out = []
    for sub in MANUAL_DIRS:
        d = os.path.join(work_dir, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.startswith('.') or '.vlm_tmp.' in f:
                continue
            if os.path.splitext(f)[1].lower() not in IMG_EXTS:
                continue
            abs_p = os.path.normpath(os.path.join(d, f))
            out.append(os.path.relpath(abs_p, work_dir) if work_relative else abs_p)
    return sorted(out)


def detect_visual_kind(sc, ip, work_dir):
    """场景视觉类型：video（Pexels 搜索/手动素材/本地视频）/ image / text

    source 以 "搜索:"（含全角冒号）开头 → video（Pexels 下载）
    type: video（source 可为空）→ video（手动素材目录自动分配）
    source 为空 且 素材/短片 目录有视频 → video（自动分配）；无素材 → text
    source 是存在的本地视频文件 → video（精确指定）
    type: text → text；其余（图片文件）→ image；图片不存在 → text 兜底
    """
    visuals = sc.get('visuals') or {}
    src = (visuals.get('source') or '').strip()
    vtype = visuals.get('type', '')
    manual = discover_manual_videos(work_dir)

    if not src:
        if vtype == 'text':
            return 'text'
        return 'video' if manual else 'text'
    if src.startswith('搜索:') or src.startswith('搜索：'):
        return 'image' if vtype == 'image' else 'video'  # type: image → Pexels 照片；其余 → 视频
    if vtype == 'text':
        return 'text'
    p = src if os.path.isabs(src) else os.path.join(work_dir, src)
    if os.path.exists(p):
        if os.path.splitext(p)[1].lower() in VIDEO_EXTS or vtype == 'video':
            return 'video'
        return 'image'
    if vtype == 'video':
        return 'video' if manual else 'text'
    return 'image' if ip else 'text'


def build_shots(items):
    """全片拍平成镜头序列。返回 (shots, scene_starts)

    视频场景拆 max(1, ceil(D/5)) 个镜头（≤5s，对齐 MPT 节奏）；
    图片/文字场景 1 个镜头（图片动感由 Ken Burns 承担，拆镜头会跳变）。
    镜头时长 d = (D + (n-1)*f) / n 帧量化、末镜头吸收余差，
    保证 xfade 链（每个过渡损耗 f）输出精确等于 D_k。
    scene_starts[k] = 场景 k 音频起点 = 该场景第一镜头的视频 offset。
    """
    plan = []
    for idx, it in enumerate(items):
        D = it['D']
        if it['kind'] == 'video' and it.get('video_paths'):
            srcs = it['video_paths']
            src_durs = [xfade_join.probe_video_dur(s) or 0.0 for s in srcs]
            n = max(1, math.ceil(D / MAX_SHOT_DUR))
            base = round(((D + (n - 1) * XFADE_FADE) / n) * VIDEO_FPS) / VIDEO_FPS
            acc = 0.0   # 素材内切片起点累积（多镜头同素材时按时间段切片，画面不重复）
            for j in range(n):
                dd = base
                if j == n - 1:   # 末镜头吸收帧量化余差
                    dd = D + (n - 1) * XFADE_FADE - (n - 1) * base
                    dd = max(round(dd * VIDEO_FPS) / VIDEO_FPS, 0.04)
                src = srcs[j % len(srcs)]
                sd = src_durs[j % len(srcs)]
                start = (acc % sd) if sd > 0.01 else 0.0
                acc += dd
                plan.append({'kind': 'video', 'src': src, 'dur': dd,
                             'src_start': start, 'scene_idx': idx,
                             'mood': it['mood']})
        else:
            plan.append({'kind': it['kind'],
                         'src': it['ip'] if it['kind'] == 'image' else None,
                         'dur': D, 'scene_idx': idx,
                         'mood': it['mood']})

    durs = [p['dur'] for p in plan]
    offsets, _ = xfade_join.compute_offsets(durs, XFADE_FADE, VIDEO_FPS)

    # 场景起点 = 场景第一镜头的视频 offset（plan 按 items 顺序生成）
    scene_starts = []
    last_idx = None
    for i, p in enumerate(plan):
        if p['scene_idx'] != last_idx:
            scene_starts.append(offsets[i])
            last_idx = p['scene_idx']
    return plan, scene_starts


# ============================================================
# 主流程
# ============================================================
def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <director.yaml> [--no-search]")
        sys.exit(1)

    yaml_path = sys.argv[1]
    # 页面上的「自动搜索」开关（见 web/pipelines.py）。关了就只有 2b 段的行为变化：
    # `搜索:` 场景不发 Pexels 请求，退到本地素材 → 文字卡片。
    # 用 `in sys.argv` 而不是 argparse：本文件只吃一个位置参数，几十行的解析器
    # 换一个布尔值不值当，而且这里的参数顺序与其它脚本一致（标志都排在后面）。
    allow_search = '--no-search' not in sys.argv
    work_dir  = os.path.dirname(os.path.abspath(yaml_path)) or '.'
    tmp_dir   = tempfile.mkdtemp(prefix='vm_', dir=work_dir)
    seg_dir   = os.path.join(tmp_dir, 'segs')
    os.makedirs(seg_dir)

    director = load(yaml_path)
    vc = director['voice']

    # ---------- 音色覆盖：页面选的音色 > director.yaml 里写的 ----------
    # web 层通过 SPK_OVERRIDE 注入。没有它的话，在页面上选「线上 Edge」再渲染
    # 一份已有的 director.yaml，出来的还是文件里写死的「故事女声」，
    # 而且**一声不吭** —— 用户以为线上音色生效了，其实根本没有。
    # 覆盖只作用于本次运行，不回写 director.yaml。
    _spk_override = os.environ.get('SPK_OVERRIDE', '').strip()
    if _spk_override and _spk_override != vc.get('spk_id'):
        print(f"   🎚️ 音色覆盖: {vc.get('spk_id', '?')} → {_spk_override}（来自页面）")
        vc = dict(vc, spk_id=_spk_override)

    # ---------- 输出尺寸：director output.size 指定，默认 9:16（抖音） ----------
    global W, H
    size = (director.get('output') or {}).get('size')
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        try:
            W, H = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            pass
    orient_label = '竖屏 9:16' if H > W else '横屏 16:9'
    print(f"   📐 输出尺寸: {W}x{H}（{orient_label}）")

    print(f"🎬 {director['project']['title']}")
    print(f"   音色: {vc.get('spk_id','?')}  express={vc.get('express',False)}")

    # ---------- 决定是否跳过 DeepSeek 编排 ----------
    # VLM 与 optimize 是**两件事**，别合并成一个开关：
    #   optimize 管「让 DeepSeek 重排场景、重写字幕」；
    #   VLM     管「看一眼手动上传的图，把画面信息补给字幕」。
    # 以前 VLM 整个嵌在 optimize 分支里，而 director_agent 生成的 yaml 一律
    # optimize=false（见 director_agent.py），于是视觉能力一次都没触发过。
    optimize = director.get('optimize', True)
    if not optimize:
        print("   ⏭️ optimize=false，跳过 DeepSeek 编排")
        print(f"     直接使用 director.yaml 中的 {len(director['scenes'])} 个场景")

        img_analyzer = ImageAnalyzer()
        manual = manual_images_in_scenes(work_dir, director['scenes'])
        vlm_ok = bool(manual) and img_analyzer.is_available() and img_analyzer.load()
        if manual and not vlm_ok:
            print(f"   ℹ️ 有 {len(manual)} 张手动上传的图片，但视觉模型不可用，跳过识别")
        elif vlm_ok:
            print(f"   👁️ 识别手动上传的 {len(manual)} 张图片（其余图片不做 VLM）")

        scene_inventory = []
        for sc in director['scenes']:
            vsrc = sc.get('visuals', {}).get('source', '')
            abs_vsrc = resolve_img_path(vsrc, work_dir)
            # 图片预处理（仅缩放）；搜索:前缀 / 本地视频文件跳过（由素材阶段处理）
            ip = None
            if abs_vsrc and os.path.exists(abs_vsrc):
                if os.path.splitext(abs_vsrc)[1].lower() in VIDEO_EXTS:
                    pass
                else:
                    key = sc.get('id', 0)
                    ip = make_tmp_jpg(abs_vsrc, tmp_dir, key)
            narration = sc.get('narration', '')
            # VLM 轻量补充：画面和解说对不上时，把画面关键信息补一句进字幕。
            # 与下面「optimize 开着但没走 LLM 编排」那条用的是同一套规则。
            if vlm_ok and ip and is_manual_image(os.path.relpath(abs_vsrc, work_dir)):
                v = img_analyzer.analyze(abs_vsrc, narration) or {}
                vk = v.get('visual_key', '')
                mt = v.get('match', '')
                if vk and mt in ('no', 'partial') and vk[:15] not in narration:
                    narration = narration + ' · ' + vk[:20]
                    print(f"      👁️ {vk} | 匹配:{mt} → 已补进字幕")
                elif vk:
                    print(f"      👁️ {vk} | 匹配:{mt}")
            scene_inventory.append({
                'sc': sc,
                'ip': ip,
                'narration': narration,
                # 默认取**补充后**的 narration：取原句的话字幕和配音会对不上
                'spoken': sc.get('spoken', narration),
                'mood': sc.get('mood', '平实'),
                'speed': sc.get('speed', vc.get('speed', '中')),
            })
    else:
        # ---------- VLM 初始化 ----------
        img_analyzer = ImageAnalyzer()
        vlm_ok = img_analyzer.is_available() and img_analyzer.load()
        if not vlm_ok:
            print("   ℹ️ 未加载视觉模型，使用原始字幕")

        # ============================================================
        # Phase 0: 发现所有图片 + 构建完整图片清单
        # ============================================================
        inventory = build_image_inventory(work_dir, director['scenes'])
        existing = [inv for inv in inventory if inv['has_scene']]
        new_only = [inv for inv in inventory if not inv['has_scene']]
        print(f"\n📷 发现 {len(inventory)} 张图片（{len(existing)} 张已有场景，{len(new_only)} 张新增）")

        if vlm_ok and len(inventory) == 0:
            print("   ⚠️ 未找到任何图片，仅生成文字卡片视频")

        # ============================================================
        # Phase 1: 预处理所有图片 + VLM 分析
        # ============================================================
        print("\n📷 Phase 1/2: 图片预处理 + VLM 内容识别")
        for inv in inventory:
            inv['key'] = inv['scene']['id'] if inv['scene'] else f"new_{hash(inv['abs_path']) % 10000}"
            inv['ip'], inv['vlm'] = process_image(inv, img_analyzer, vlm_ok, tmp_dir)

        # ============================================================
        # 导演编排：DeepSeek 重新设计所有场景
        # ============================================================
        opt_yaml = optimize_scenes_with_llm(director, inventory, work_dir)
        if opt_yaml and os.path.exists(opt_yaml):
            new_director = load(opt_yaml)
            optimized_scenes = new_director['scenes']
            print(f"\n🎬 导演编排完成 → 使用优化版（{len(optimized_scenes)} 场景）: {opt_yaml}")

            original_map = {}
            for sc in director.get('scenes', []):
                vsrc = sc.get('visuals', {}).get('source', '')
                abs_vsrc = resolve_img_path(vsrc, work_dir)
                if abs_vsrc:
                    original_map[abs_vsrc] = sc

            scene_inventory = []
            for sc in optimized_scenes:
                vsrc = sc.get('visuals', {}).get('source', '')
                abs_vsrc = resolve_img_path(vsrc, work_dir)
                matched = next((inv for inv in inventory if inv['abs_path'] == abs_vsrc), None)
                orig = original_map.get(abs_vsrc)

                if orig:
                    narration = sc.get('narration', orig['narration'])
                    spoken = sc.get('spoken', orig.get('spoken', orig['narration']))
                    v = matched.get('vlm', {}) if matched else {}
                    visual_key = v.get('visual_key', '') if v.get('match') in ('no', 'partial') else ''
                    if visual_key and visual_key[:15] not in narration:
                        narration = narration + ' · ' + visual_key[:20]
                        print(f"      📝 补充画面: {visual_key[:20]}")
                    print(f"      📝 字幕排版优化")
                else:
                    narration = sc.get('narration', '')
                    spoken = sc.get('spoken', narration)

                scene_inventory.append({
                    'scene': sc,
                    'sc': sc,
                    'ip': matched['ip'] if matched else None,
                    'narration': narration,
                    'spoken': spoken,
                    'mood': sc.get('mood', '平实'),
                    'speed': sc.get('speed', vc.get('speed', '中')),
                })
        else:
            # 没走 LLM 编排 → 用原有场景 + VLM 轻量补充
            print("   ℹ️ 使用原始场景顺序和字幕")
            scene_inventory = []
            for inv in inventory:
                if not inv['has_scene']:
                    continue
                sc = inv['scene']
                txt = sc['narration']
                v = inv.get('vlm', {})
                visual_key = v.get('visual_key', '') if v.get('match') in ('no', 'partial') else ''
                if visual_key and visual_key[:15] not in txt:
                    txt = txt + ' · ' + visual_key[:20]
                scene_inventory.append({
                    'sc': sc,
                    'ip': inv['ip'],
                    'narration': txt,
                    'spoken': sc.get('spoken', txt),
                    'mood': sc.get('mood', '平实'),
                    'speed': sc.get('speed', vc.get('speed', '中')),
                })

    if not scene_inventory:
        print("❌ 没有可合成的场景")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # ============================================================
    # Phase 2: TTS → 素材 → 镜头化 → 拼接 → 字幕+BGM 终稿
    # ============================================================
    print(f"\n🎤 Phase 2/2: 语音合成 + 视频渲染（{len(scene_inventory)} 场景）")

    # ---- 2a. 全部场景先 TTS ----
    _batch_tts(scene_inventory, vc, tmp_dir)
    items = []
    for i, item in enumerate(scene_inventory):
        sc      = item['sc']
        sid     = sc['id'] if sc else (i + 1)
        mood    = item['mood']
        spd     = item['speed']
        spoken  = item['spoken']

        spoken_cn = convert_spoken_numbers(spoken)
        if spoken_cn != spoken:
            print(f"      🔢 数字转换: {spoken[:60]}→{spoken_cn[:60]}")
        wav = os.path.join(tmp_dir, f's{sid}.wav')
        try:
            synth_scene(spoken_cn, vc['spk_id'], vc.get('express', False), spd, wav)
        except subprocess.CalledProcessError as e:
            print(f"      ❌ 场景{sid} TTS 失败: {e}")
            continue
        dur = get_duration(wav)
        print(f"      🎬 场景{sid} {mood} | {dur:.1f}s")
        items.append({'sc': sc, 'ip': item['ip'], 'narration': item['narration'],
                      'mood': mood, 'wav': wav, 'dur': dur,
                      'D': dur + SCENE_TAIL, 'spoken_cn': spoken_cn})

    if not items:
        print("❌ 没有成功合成任何场景")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # ---- 2b. 素材准备（搜索: → Pexels 下载；手动素材目录自动分配；本地直用）----
    if not allow_search:
        print("   🔌 自动搜索已关闭：搜索: 场景不联网，改用本地素材（没有就降级文字卡片）")
    auto_videos = discover_manual_videos(work_dir)
    if auto_videos:
        print(f"   📂 手动素材: {len(auto_videos)} 个（{os.path.join(work_dir, '素材')}）")
    auto_used = 0
    used_hashes = set()   # 跨场景素材去重（Pexels 中文词会重复顶同一热门视频）
    for it in items:
        sc = it['sc']
        sid = sc.get('id', '?')
        it['kind'] = detect_visual_kind(sc, it['ip'], work_dir)
        it['video_paths'] = []
        if it['kind'] == 'video':
            src = ((sc.get('visuals') or {}).get('source') or '').strip()
            is_search = src.startswith('搜索:') or src.startswith('搜索：')
            if is_search and allow_search:
                term = src[3:].strip() if '搜索' in src[:2] else src
                term = term.lstrip(':：').strip()
                orient = 'portrait' if H > W else 'landscape'
                it['video_paths'] = ensure_scene_videos(term, it['D'], orientation=orient,
                                                        skip_hashes=used_hashes)
            else:
                # 非搜索场景：看 source 指的本地文件在不在。
                # `搜索:` 且关了搜索时 p 留空，直接落到下面的本地视频轮转。
                p = '' if is_search else (
                    src if os.path.isabs(src) else os.path.join(work_dir, src))
                if p and src and os.path.exists(p):
                    it['video_paths'] = [p]
                elif auto_videos:
                    # 手动素材按场景顺序轮流分配
                    it['video_paths'] = [auto_videos[auto_used % len(auto_videos)]]
                    auto_used += 1
                    if is_search:
                        print(f"      🔌 场景{sid} 关了自动搜索，改用本地素材: "
                              f"{os.path.basename(it['video_paths'][0])}")
            if not it['video_paths']:
                it['kind'] = 'text'
                print(f"      ⚠️ 场景{sid} 素材不可用，降级文字卡片: {src[:40]}")
        elif it['kind'] == 'image' and not it['ip']:
            # 搜索: 图片场景 → Pexels 照片下载；失败才降级文字卡
            src = ((sc.get('visuals') or {}).get('source') or '').strip()
            if src.startswith('搜索:') or src.startswith('搜索：'):
                if not allow_search:
                    # 图片不做「轮换顶替」：用户传的图都是**内容特定**的
                    # （比如某个车型的前脸），随便塞一张到讲别的字幕下，
                    # 正是「画面和字幕对不上」。宁可退成文字卡，但要吵一声。
                    it['kind'] = 'text'
                    print(f"      🔌 场景{sid} 关了自动搜索，'{src[:40]}' 取不到素材 "
                          f"→ 文字卡片（重新生成导演文件可让它少写几场）")
                    continue
                term = src[3:].strip().lstrip(':：').strip()
                orient = 'portrait' if H > W else 'landscape'
                photo = ensure_scene_photo(term, orientation=orient)
                if photo:
                    it['ip'] = make_tmp_jpg(photo, tmp_dir, sc.get('id', 0))
                    print(f"      🖼️ 搜索照片: '{term}' → 图片场景")
                else:
                    it['kind'] = 'text'
                    print(f"      ⚠️ 照片不可用，降级文字卡片: {src[:40]}")
            else:
                it['kind'] = 'text'

    # ---- 2c. 镜头化 ----
    shots, scene_starts = build_shots(items)
    print(f"   🎥 共 {len(shots)} 个镜头（转场 {XFADE_FADE}s 交叉溶解）")

    # ---- 2d. 逐镜头渲染（无音轨）----
    seg_dir = os.path.join(tmp_dir, 'segs')
    os.makedirs(seg_dir, exist_ok=True)
    shot_files = []
    for si, shot in enumerate(shots):
        out = os.path.join(seg_dir, f'shot{si:03d}.mp4')
        render_shot(shot, out)
        shot_files.append(out)

    # ---- 2e. 全局拼接（xfade 三步法，失败回退硬切）----
    video_segs = [{'path': p, 'dur': s['dur'], 'scene_idx': s['scene_idx']}
                  for p, s in zip(shot_files, shots)]
    audio_segs = [{'path': it['wav'], 'dur': it['D'], 'start': scene_starts[k]}
                  for k, it in enumerate(items)]
    print(f"   🧩 拼接 {len(video_segs)} 个镜头...")
    ok, joined_v, joined_a = xfade_join.join_with_xfade(
        video_segs, audio_segs, tmp_dir,
        fps=VIDEO_FPS, fade=XFADE_FADE, audio_fade=AUDIO_FADE)
    if not ok:
        print("   ⚠️ 交叉溶解失败，回退 concat 硬切")
        ok, joined_v, joined_a = xfade_join.concat_join(video_segs, audio_segs, tmp_dir)
    if not ok:
        print("❌ 视频拼接失败")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # ---- 2f. 全局 ASS 字幕 + BGM 终稿单 pass ----
    all_cues = []
    for k, it in enumerate(items):
        sid = it['sc']['id'] if it['sc'] else (k + 1)
        srt_file = make_subtitle(it['narration'], it['dur'], tmp_dir, sid,
                                 audio_path=it['wav'], spoken=it['spoken_cn'])
        cues = srt_file_to_cues(srt_file) if os.path.exists(srt_file) else []
        all_cues.append(cues)
    ass_path = os.path.join(tmp_dir, 'final.ass')
    assemble_global_ass(all_cues, scene_starts, ass_path, play_w=W, play_h=H)
    print(f"   💬 字幕: {sum(len(c) for c in all_cues)} 条 → 全局 ASS 烧录")

    bgm_cfg = director.get('bgm', {})
    if bgm_cfg.get('path'):
        print(f"   🎵 BGM 混音...")
    final_video = os.path.join(tmp_dir, 'final.mp4')
    render_final(joined_v, joined_a, bgm_cfg, ass_path, final_video)

    # ============================================================
    # 输出
    # ============================================================
    out_cfg = director.get('output', {})
    out_path = out_cfg.get('path', '') or os.path.join(work_dir, 'output.mp4')
    shutil.move(final_video, out_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    final_dur = get_duration(out_path)
    size_mb = os.path.getsize(out_path) / 1e6
    print(f"\n✅ 完成 → {out_path}  ({final_dur:.1f}s, {size_mb:.0f}MB)")

    # ---- 回填语速实测值，并核对是否达到了要求的片长 ----
    # 这是「用户填 30 秒，实际出 43 秒」这条链路上唯一能看见真相的地方：
    # 到这里才同时握着「念了多少字」和「成片多少秒」。
    try:
        import speech_rate
        n = sum(len(re.sub(r'\s', '', s.get('spoken') or s.get('narration') or ''))
                for s in director.get('scenes') or [])
        rec = speech_rate.record(vc.get('spk_id'), n, final_dur)
        if rec:
            print(f"   🗣️ 语速实测: {n} 字 / {final_dur:.1f}s = "
                  f"{rec['rate']:.2f} 字/秒（累计 {rec['samples']} 条）")
    except Exception as e:                                   # noqa: BLE001
        print(f"   （语速回填跳过: {e}）")

    want = float((director.get('project') or {}).get('duration') or 0)
    if want > 0:
        off = (final_dur - want) / want
        if abs(off) > 0.15:
            print(f"   ⚠️ 片长偏差 {off:+.0%}：要求 {want:.0f}s，实际 {final_dur:.1f}s"
                  f"（旁白字数决定片长，改时长请调 --dur 后的旁白预算）")


if __name__ == '__main__':
    main()
