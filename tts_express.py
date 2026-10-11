"""智能表现力TTS：LLM自动理解文本 → 插入呼吸/情绪标记 → CosyVoice合成

用法:
  # 基础克隆（无情感）
  video-env python tts_express.py <文本> <参考音频>

  # 情感表现模式（LLM自动标记 + 语速/方言控制）
  video-env python tts_express.py <文本> <参考音频> --express --speed 快 --lang 广东话

  # 用已注册音色
  video-env python tts_express.py <文本> --spk-id 故事女声 --express
"""

import sys, os, re, time, argparse, tempfile, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P
P.add_cosyvoice_to_path()

from cosyvoice.cli.cosyvoice import AutoModel
import torch, torchaudio

MODEL_PATH = str(P.COSYVOICE_MODEL)
OUTPUT = str(P.ROOT / 'output.wav')
_tmp_refs = []

# LLM 配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import llm as _llm         # 密钥不再硬编码，见 pipeline_config.py
LLM_API_URL, LLM_API_KEY, LLM_MODEL = _llm()

from tts_backends import (                      # 线上音色前缀分发，见 tts_backends.py
    cli_online, cli_online_batch, is_online_voice, synthesize as _synth_online)
PROMPT_FILE  = os.path.join(os.path.dirname(__file__), 'tts_express_prompt.md')


def _load_prompt_template() -> str:
    """从 md 文件提取 LLM 提示词模板"""
    if not os.path.exists(PROMPT_FILE):
        return None
    content = open(PROMPT_FILE, encoding='utf-8').read()
    marker = '## LLM 提示词模板'
    idx = content.find(marker)
    if idx == -1:
        return None
    t = content[idx + len(marker):].strip()
    t = re.sub(r'^```\w*\n', '', t)
    t = re.sub(r'\n```\s*$', '', t)
    return t.strip()


_MARK_RE = re.compile(r'\[[a-z][a-z_-]*\]|</?strong>')

def _strip_marks(text: str) -> str:
    """剥离所有标记，剩下应等于原文"""
    return _MARK_RE.sub('', text)


def _same_core(a: str, b: str) -> bool:
    """去掉空白/标点后逐字一致（容忍 LLM 对引号等做微调）"""
    keep = re.compile(r'[\s，。！？；、：""''（）…—~·]+')
    return keep.sub('', a) == keep.sub('', b)


def tag_text(plain_text: str) -> str:
    """LLM 分析文本语义，插入表现力标记；无 API 或跑偏则启发式回退"""
    import requests
    template = _load_prompt_template()
    if not template:
        return _heuristic_tag(plain_text)
    prompt = template.replace('{text}', plain_text)
    try:
        resp = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 4096, "thinking": {"type": "disabled"},
        }, headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=30)
        if resp.status_code == 200:
            tagged = resp.json()['choices'][0]['message']['content'].strip('"\'')
            # 校验：剥离标记后必须逐字等于原文（防 LLM 扩写/改写文本）
            if _same_core(_strip_marks(tagged), plain_text):
                return tagged
            print(f"  ⚠️ LLM 标记跑偏（增删改原文），回退启发式标记")
            print(f"     原文: {plain_text[:40]}")
            print(f"     返回: {tagged[:60]}...")
    except Exception:
        pass
    return _heuristic_tag(plain_text)


def _heuristic_tag(text: str) -> str:
    """启发式回退：标点/关键词自动加标记"""
    text = re.sub(r'([。！？.!?])', r'\1[breath]', text)
    text = re.sub(r'(……|——)', r'\1[mn]', text)
    text = re.sub(r'(唉|哎)', r'[sigh]\1', text)
    text = re.sub(r'(哈哈|呵呵|嘿嘿)', r'[laughter]\1', text)
    text = re.sub(r'(嘶|咝)', r'[hissing]\1', text)
    text = re.sub(r'「([^」]+)」', r'<strong>\1</strong>', text)
    text = re.sub(r'"([^"]+)"', r'<strong>\1</strong>', text)
    return text


def is_video(path: str) -> bool:
    return bool(re.search(r'\.(mp4|mov|avi|mkv|webm|flv|wmv)$', path, re.I))


def prepare_ref_audio(path: str) -> str:
    """预处理参考音源：视频→音频，统一24000Hz/单声道，whisper找最佳3-5秒"""
    raw = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
    final = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
    print(f"   {'🎬 视频提取音频' if is_video(path) else '🎵 音频转码'}...")
    subprocess.run(['ffmpeg', '-y', '-i', path, '-ar', '24000', '-ac', '1', raw], capture_output=True)
    if not os.path.exists(raw) or os.path.getsize(raw) < 1000:
        os.unlink(raw); return None
    # whisper 找最佳片段
    try:
        from faster_whisper import WhisperModel
        whisper = WhisperModel(str(P.WHISPER_MODEL), device='cuda', compute_type='float16')
        segs = list(whisper.transcribe(raw, language='zh')[0])
        best_s, best_e, best_d = 0, 0, 0
        for i in range(len(segs)):
            for j in range(i, len(segs)):
                dur = segs[j].end - segs[i].start
                if 2.5 <= dur <= 6:
                    d = len(' '.join(s.text for s in segs[i:j+1])) / dur
                    if d > best_d: best_s, best_e, best_d = segs[i].start, segs[j].end, d
        if best_d > 0:
            subprocess.run(['ffmpeg', '-y', '-i', raw, '-ss', str(best_s), '-to', str(best_e),
                           '-ar', '24000', '-ac', '1', final], capture_output=True)
            print(f"   🎯 参考片段: [{best_s:.1f}-{best_e:.1f}s]")
        else:
            subprocess.run(['ffmpeg', '-y', '-i', raw, '-t', '5', '-ar', '24000', '-ac', '1', final], capture_output=True)
    except Exception:
        subprocess.run(['ffmpeg', '-y', '-i', raw, '-t', '5', '-ar', '24000', '-ac', '1', final], capture_output=True)
    os.unlink(raw)
    _tmp_refs.append(final)
    return final


def is_video(path: str) -> bool:
    return bool(re.search(r'\.(mp4|mov|avi|mkv|webm|flv|wmv)$', path, re.I))


def _synth_one(cosyvoice, text, *, spk_id, express=False):
    """按 main() 里 spk_id 两条分支的规则合成一段，返回 (音频tensor, 采样率)。

    刻意只覆盖 spk_id 分支：--batch 只用于「已注册音色的批量旁白」。
    main() 保持原样不动 —— 批量只是提速手段，不是新的合成语义。
    """
    if express:
        print("   🎯 zero_shot + spk_id + 标记文本")
        gen = cosyvoice.inference_zero_shot(tag_text(text), '', '',
                                            zero_shot_spk_id=spk_id, stream=False)
    else:
        print("   🎯 基础克隆（spk_id）")
        gen = cosyvoice.inference_zero_shot(text, '', '',
                                            zero_shot_spk_id=spk_id, stream=False)
    chunks = [r['tts_speech'].cpu() for r in gen if 'tts_speech' in r]
    if not chunks:
        return None, cosyvoice.sample_rate
    return torch.cat(chunks, dim=1), cosyvoice.sample_rate


def run_batch(src):
    """--batch：一次加载模型，合成整部片子的旁白。

    src 为 JSON 文件路径，或 '-' 表示从 stdin 读。
    入参 [{text, spk_id, express, out}, ...]
    出参 末行 `__BATCH_RESULT__[{out, ok, secs, error}, ...]`

    单条失败不中断其余：渲染侧按「目标文件不存在」退回逐场景合成。
    """
    import json as _json
    raw = sys.stdin.read() if src == '-' else open(src, encoding='utf-8').read()
    jobs = _json.loads(raw)

    # ---- 线上音色：在 AutoModel 之前分流 ----
    # 一段都没有本地音色时连模型都不建。这就是线上分支存在的全部意义：
    # 本地加载实测 21.1s + 3.2GB 显存，而线上整批只要几秒。
    # （torch 在模块顶部就 import 了，省不掉；但 import 不建模型、不占显存。）
    n_online = sum(1 for j in jobs if is_online_voice(j.get('spk_id')))
    if jobs and n_online == len(jobs):
        return cli_online_batch(jobs)
    if n_online:
        print(f"   {n_online}/{len(jobs)} 段走线上音色，其余走本地模型")

    print(f"🎤 批量合成：{len(jobs)} 段旁白，模型只加载一次")
    t0 = time.time()
    cosyvoice = AutoModel(model_dir=MODEL_PATH, fp16=True)
    print(f"   模型加载 {time.time() - t0:.1f}s", flush=True)

    results = []
    for i, j in enumerate(jobs, 1):
        out = j['out']
        print(f"   [{i}/{len(jobs)}] {j['text'][:28]}...", flush=True)
        try:
            if is_online_voice(j.get('spk_id')):
                # 混批里的线上段：不走 CosyVoice，但仍要满足同一个出参契约
                r = _synth_online(j.get('spk_id'), j['text'], out)
                if not r['ok']:
                    raise RuntimeError(r['error'])
                secs = r['secs']
            else:
                audio, sr = _synth_one(cosyvoice, j['text'],
                                       spk_id=j.get('spk_id'),
                                       express=bool(j.get('express')))
                if audio is None:
                    raise RuntimeError('合成结果为空')
                torchaudio.save(out, audio, sr)
                secs = audio.shape[1] / sr
            print(f"        ✅ {secs:.1f}s → {os.path.basename(out)}", flush=True)
            results.append({'out': out, 'ok': True, 'secs': round(secs, 2)})
        except Exception as e:                                  # noqa: BLE001
            print(f"        ❌ 失败: {e}", flush=True)
            results.append({'out': out, 'ok': False, 'error': str(e)})

    print(f"   批量完成：{sum(1 for r in results if r['ok'])}/{len(jobs)} 段，"
          f"用时 {time.time() - t0:.1f}s")
    print('__BATCH_RESULT__' + _json.dumps(results, ensure_ascii=False))


def main():
    global OUTPUT
    p = argparse.ArgumentParser(description='智能表现力TTS')
    p.add_argument('text', nargs='?', help='要合成的文本')
    p.add_argument('ref_audio', nargs='?', help='参考音频/视频路径')
    p.add_argument('--text-file', help='从文件读取文本')
    p.add_argument('--spk-id', help='使用已注册音色ID')
    p.add_argument('--express', action='store_true', help='启用情感表现（LLM标记+语速/方言控制）')
    p.add_argument('--speed', choices=['快', '慢'], help='语速（需 --express）')
    p.add_argument('--lang', help='方言：广东话/四川话/东北话等17种（需 --express）')
    p.add_argument('--output', default=OUTPUT)
    p.add_argument('--batch', metavar='JSON',
                   help='批量合成：JSON 文件路径或 - 表示 stdin，模型只加载一次')
    args = p.parse_args()

    if args.batch:
        return run_batch(args.batch)

    # 获取文本
    if args.text_file:
        tts_text = open(args.text_file, encoding='utf-8').read().strip()
    elif args.text:
        tts_text = args.text
    else:
        print("需要提供文本（--text-file 或 直接传参）"); sys.exit(1)

    if not args.ref_audio and not args.spk_id:
        print("需要提供参考音频（或 --spk-id 指定已注册音色）"); sys.exit(1)

    if args.output:
        OUTPUT = args.output

    # ---- 线上音色：前缀分发，合成完就返回 ----
    # 必须挡在 AutoModel 之前：本地加载实测 21.1s + 3.2GB 显存，线上整段几秒。
    # 参考音频的准备也一起跳过 —— 线上后端吃的是纯文本，克隆音色对它们没意义。
    if is_online_voice(args.spk_id):
        return cli_online(args.spk_id, tts_text, OUTPUT, speed=args.speed)

    # 准备参考音频（spk-id 模式可选）
    ref_audio = args.ref_audio or ''
    if ref_audio and os.path.exists(ref_audio):
        tmp = prepare_ref_audio(ref_audio)
        if tmp: ref_audio = tmp

    cosyvoice = AutoModel(model_dir=MODEL_PATH, fp16=True)  # fp16 半精度推理提速（无 GPU 自动回退 fp32）

    if args.express and ref_audio:
        # ====== 情感表现模式（有参考音频：cross_lingual + 标记） ======
        print("📝 LLM 分析文本，插入表现力标记...")
        tagged = tag_text(tts_text)
        print(f"   原始: {tts_text[:60]}...")
        print(f"   标记: {tagged[:80]}...")

        parts = []
        if args.lang:    parts.append(f"请用{args.lang}表达")
        if args.speed == '快': parts.append("请用尽可能快地语速说一句话")
        elif args.speed == '慢': parts.append("请用尽可能慢地语速说一句话")
        instruct = "You are a helpful assistant. " + "，".join(parts) + "。<|endofprompt|>" if parts else ""

        if instruct:
            print(f"   🎯 instruct2（指令+标记+音色克隆）")
            gen = cosyvoice.inference_instruct2(tagged, instruct, ref_audio, stream=False)
        else:
            print(f"   🎯 cross_lingual（细粒度标记+音色克隆）")
            gen = cosyvoice.inference_cross_lingual(
                "You are a helpful assistant.<|endofprompt|>" + tagged, ref_audio, stream=False)
    elif args.express and args.spk_id:
        # ====== 情感表现模式（spk-id，无参考音频：zero_shot + spk_id + 标记文本） ======
        print("📝 LLM 分析文本，插入表现力标记...")
        tagged = tag_text(tts_text)
        print(f"   原始: {tts_text[:60]}...")
        print(f"   标记: {tagged[:80]}...")
        print(f"   🎯 zero_shot + spk_id（标记文本+已注册音色）")
        gen = cosyvoice.inference_zero_shot(tagged, '', '', zero_shot_spk_id=args.spk_id, stream=False)
    elif args.spk_id:
        # ====== 基础克隆 + spk_id ======
        print(f"   🎯 基础克隆（spk_id）")
        gen = cosyvoice.inference_zero_shot(tts_text, '', '', zero_shot_spk_id=args.spk_id, stream=False)
    else:
        # ====== 基础克隆 + 参考音频 ======
        print(f"   🎯 基础克隆（参考音频）")
        gen = cosyvoice.inference_zero_shot(
            tts_text, "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。", ref_audio, stream=False)

    chunks = []
    for r in gen:
        if 'tts_speech' in r: chunks.append(r['tts_speech'].cpu())
    if chunks:
        full = torch.cat(chunks, dim=1)
        torchaudio.save(OUTPUT, full, cosyvoice.sample_rate)
        secs = full.shape[1] / cosyvoice.sample_rate
        print(f"\n✅ 生成 {secs:.1f}秒 → {OUTPUT}")
    else:
        print("❌ 合成失败")

    for f in _tmp_refs:
        try: os.unlink(f)
        except OSError: pass


if __name__ == '__main__':
    main()
