#!/usr/bin/env python3
"""render_remix.py — 按 remix.yaml 渲染二创视频

用法:
  python render_remix.py <工作目录> [--bgm <音乐.mp3>]

工作目录需包含:
  analysis.json  # 分析报告（用于找原视频路径）
  remix.yaml     # 二创方案
  图片/          # 图片素材
  短片/          # 短片素材
"""
import sys, os, subprocess, tempfile, shutil, yaml, json, re

# 同目录模块：ASS 圆角字幕 / Pexels 素材 / 编码器
import project_paths as P
from ass_subtitle import make_ass_from_srt
from pexels_materials import ensure_scene_videos
from encoder import video_encode_args

PYTHON   = P.PYTHON
TTS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tts_express.py')
FONT     = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'


def wrap_subtitle(text, max_chars=15):
    """字幕按标点换行，完整显示所有内容"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    lines = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            lines.append(remaining)
            break
        # 在 max_chars 范围内找最后一个标点断句
        cut = max_chars
        for sep in '。！？；、，':
            pos = remaining[:max_chars].rfind(sep)
            if pos > 2:
                cut = pos + 1
                break
        lines.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
        if len(lines) >= 3:
            # 超3行则剩余追加到最后一行
            lines[-1] += remaining
            break
    return '\n'.join(lines)

BGM_MOOD = {
    'upbeat':    0.15,
    'calm':      0.10,
    'highlight': 0.08,
    'dramatic':  0.12,
}

FADE_DUR = 0.3  # 场景切换淡入淡出时长（秒）


def apply_fades(seg_path):
    """给视频片段加淡入淡出"""
    dur = float(subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', seg_path],
        capture_output=True, text=True).stdout.strip() or 0)
    if dur < 1:
        return
    faded = seg_path + '.faded.mp4'
    fade_out_start = max(dur - FADE_DUR, 0)
    subprocess.run([
        'ffmpeg', '-y', '-i', seg_path,
        '-vf', f'fade=t=in:st=0:d={FADE_DUR},fade=t=out:st={fade_out_start}:d={FADE_DUR}',
        '-af', f'afade=t=in:st=0:d={FADE_DUR},afade=t=out:st={fade_out_start}:d={FADE_DUR}',
        *video_encode_args(20), '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k',
        faded
    ], capture_output=True)
    if os.path.exists(faded):
        os.replace(faded, seg_path)

# ============================================================
# 预处理：所有素材统一转 1920x1080 / H.264 / 24000Hz
# ============================================================
def preprocess_assets(work_dir, tmp_dir, OUT_W=1920, OUT_H=1080):
    """预处理图片和短片，统一参数"""
    pre_dir = os.path.join(tmp_dir, 'preprocessed')
    os.makedirs(pre_dir)

    # 图片 → 目标尺寸（保持原格式和文件名）
    img_dir = os.path.join(work_dir, '图片')
    if os.path.isdir(img_dir):
        print(f"  🖼️ 预处理图片...")
        from PIL import Image
        for fn in sorted(os.listdir(img_dir)):
            if not fn.lower().endswith(('.jpg','.jpeg','.png','.webp')):
                continue
            src = os.path.join(img_dir, fn)
            try:
                img = Image.open(src).convert('RGB')
                w, h = img.size
                ratio = OUT_W / w
                nh = int(h * ratio)
                img = img.resize((OUT_W, nh), Image.LANCZOS)
                if nh > OUT_H:
                    top = (nh - OUT_H) // 2
                    img = img.crop((0, top, OUT_W, top + OUT_H))
                elif nh < OUT_H:
                    pad = Image.new('RGB', (OUT_W, OUT_H), (15, 15, 30))
                    pad.paste(img, (0, (OUT_H - nh) // 2))
                    img = pad
                img.save(src, quality=85)
            except Exception as e:
                print(f"    ⚠️ 图片处理失败: {fn} - {e}")

    # 短片 → H.264 目标尺寸 24fps AAC（保留原文件名）
    clip_dir = os.path.join(work_dir, '短片')
    if os.path.isdir(clip_dir):
        print(f"  🎬 预处理短片...")
        for fn in sorted(os.listdir(clip_dir)):
            if not fn.lower().endswith(('.mp4','.mov','.avi','.webm')):
                continue
            src = os.path.join(clip_dir, fn)
            dst_tmp = os.path.join(pre_dir, fn)
            # 检测黑边（cropdetect）：内容小画面先裁黑边，再放大铺满居中裁剪
            # （否则小内容只占帧一小块，成片里观感"画面小"）
            crop_opt = ''
            r = subprocess.run(['ffmpeg', '-i', src, '-vf', 'cropdetect=limit=24:round=2',
                                '-f', 'null', '-'], capture_output=True, text=True)
            crops = re.findall(r'crop=(\d+:\d+:\d+:\d+)', r.stderr or '')
            if crops:
                best = max(set(crops), key=crops.count)  # 取多帧最常见值
                cw = int(best.split(':')[0])
                ch = int(best.split(':')[1])
                if cw < OUT_W * 0.8 or ch < OUT_H * 0.8:
                    crop_opt = f'crop={best},'
            vf = (f'{crop_opt}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,'
                  f'crop={OUT_W}:{OUT_H}:(iw-ow)/2:(ih-oh)/2')
            r = subprocess.run([
                'ffmpeg', '-y', '-i', src,
                '-vf', vf,
                '-r', '25', *video_encode_args(20), '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-ar', '24000', '-ac', '1',
                dst_tmp
            ], capture_output=True)
            if r.returncode == 0 and os.path.exists(dst_tmp):
                os.remove(src)
                shutil.move(dst_tmp, src)

    print(f"  ✅ 素材预处理完成")


def discover_clips(work_dir):
    """扫描 短片/ 与 素材/ 目录下的视频（按文件名排序），手动素材自动发现"""
    found = []
    for d in ('短片', '素材'):
        p = os.path.join(work_dir, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if fn.startswith('.'):
                continue
            if fn.lower().endswith(('.mp4', '.mov', '.avi', '.webm', '.mkv')):
                found.append(os.path.normpath(os.path.join(p, fn)))
    return found


def render(work_dir, bgm_path=None, allow_search=True):
    """渲染二创成片。

    `allow_search=False`（页面上的「自动搜索」关了）→ `搜索:` 的 clip 场景
    不发 Pexels 请求，退到本地素材轮转；顶不上就按原来的路降级。
    渲染阶段是**最后一道**闸门：就算 remix.yaml 是别的什么流程留下的、
    里面还写着 `搜索:`，关了开关的这一版也绝不会偷偷联网下载。
    """
    remix_path = os.path.join(work_dir, 'remix.yaml')
    analysis_path = os.path.join(work_dir, 'analysis.json')

    if not os.path.exists(remix_path):
        print(f"❌ 未找到: {remix_path}")
        sys.exit(1)

    # 加载方案
    with open(remix_path, encoding='utf-8') as f:
        remix = yaml.safe_load(f)
    if isinstance(remix, dict) and 'remix' in remix:
        remix = remix['remix']

    scenes = remix.get('scenes', [])
    title = remix.get('title', '未命名')
    global_bgm = bgm_path or remix.get('bgm', '')

    if not allow_search:
        print("   🔌 自动搜索已关闭：搜索: 场景不联网，改用本地短片（没有就降级文字卡片）")

    # 找原视频路径
    orig_video = remix.get('source', '')
    if not orig_video or not os.path.exists(orig_video):
        if os.path.exists(analysis_path):
            with open(analysis_path) as f:
                a = json.load(f)
            orig_video = a.get('source', orig_video)

    # 检测原视频尺寸（用于统一输出比例）
    OUT_W, OUT_H = 1080, 1920  # 默认 9:16（抖音竖屏），有原视频时自动跟随
    if orig_video and os.path.exists(orig_video):
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', orig_video],
                           capture_output=True, text=True)
        wh = r.stdout.strip().split(',')
        if len(wh) == 2:
            try:
                OUT_W, OUT_H = int(wh[0]), int(wh[1])
                print(f"   📐 视频尺寸: {OUT_W}x{OUT_H}")
            except:
                pass

    print(f"🎬 二创渲染: {title}")
    print(f"   场景数: {len(scenes)}")

    # 预处理素材（统一参数）
    tmp_dir = tempfile.mkdtemp(prefix='rmx_', dir=work_dir)
    preprocess_assets(work_dir, tmp_dir, OUT_W, OUT_H)
    seg_dir = os.path.join(tmp_dir, 'segs')
    audio_dir = os.path.join(tmp_dir, 'audio')
    os.makedirs(seg_dir)
    os.makedirs(audio_dir)

    seg_files = []
    last_orig_end = 0  # 上一个原片段结束时间（用于图片叠加背景）
    auto_clips = discover_clips(work_dir)  # 手动素材（短片/ 素材/ 目录）
    auto_clip_used = 0

    for i, sc in enumerate(scenes):
        sid = i + 1
        stype = sc.get('type', 'original')
        narration = sc.get('narration', '')
        use_original_audio = (narration is None)
        mood = sc.get('bgm', '')

        # TTS
        wav_path = None
        if not use_original_audio and narration:
            wav_path = os.path.join(audio_dir, f'voice_{sid}.wav')
            spk = os.environ.get('REMIX_SPK', '故事女声')
            print(f"  🎤 场景{sid}: TTS [{spk}]")
            r = subprocess.run([
                PYTHON, TTS_SCRIPT, narration,
                '--spk-id', spk, '--output', wav_path
            ], capture_output=True, timeout=120)
            if r.returncode != 0:
                # 不跳过整段：original 回退原声，image/clip/text 走各自的静音/原声兜底
                print(f"     ⚠️ TTS 失败，回退")
                use_original_audio = True
                narration = None

        # 获取 TTS 音频时长（用于自适应场景长度）
        tts_dur = 0
        if wav_path and os.path.exists(wav_path):
            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'format=duration', '-of', 'csv=p=0', wav_path],
                               capture_output=True, text=True)
            tts_dur = float(r.stdout.strip() or 0)

        # TTS 时长管理：场景时长由旁白朗读长度决定（不压缩），文案明显超长才截短
        if stype in ('clip', 'image', 'text') and narration and tts_dur > 0 and sc.get('audio', 'tts') != 'original':
            src_dur = 0
            src_path = resolve_path(sc.get('source', ''), work_dir)
            if src_path and os.path.exists(src_path) and stype == 'clip':
                r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                    'format=duration', '-of', 'csv=p=0', src_path],
                                   capture_output=True, text=True)
                try:
                    src_dur = float(r.stdout.strip())
                except ValueError:
                    src_dur = 0
            max_expected = max(src_dur, sc.get('duration', 3))

            # 文案明显超长（>1.8x 预期时长）才截短重合成；正常偏差（±50%）由场景时长自适应吸收
            if tts_dur > max_expected * 1.8 + 0.5:
                chars_per_sec = len(narration) / max(tts_dur, 0.1)
                target_chars = int(max_expected * chars_per_sec * 0.85)  # 留余量
                target_chars = max(target_chars, 8)  # 最短8字
                if target_chars < len(narration):
                    # 按标点截短：从前往后累计完整短句
                    shortened = ''
                    for sep in '，。！？；、':
                        parts = [p for p in narration.split(sep) if p]
                        acc = parts[0] if parts else ''
                        for p in parts[1:]:
                            if len(acc) + len(p) + 1 <= target_chars:
                                acc = acc + sep + p
                            else:
                                break
                        if acc:
                            shortened = acc
                        if len(shortened) <= target_chars:
                            break
                    if len(shortened) > target_chars or not shortened:
                        shortened = narration[:target_chars]  # 硬切兜底
                    if len(shortened) < len(narration) and len(shortened) >= 8:
                        print(f"     📏 文案过长({len(narration)}字>{target_chars}字)，自动截短")
                        print(f"       原: {narration}")
                        print(f"       新: {shortened}")
                        narration = shortened
                        r = subprocess.run([
                            PYTHON, TTS_SCRIPT, narration,
                            '--spk-id', spk, '--output', wav_path
                        ], capture_output=True, timeout=120)
                        if r.returncode == 0:
                            r2 = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                                  'format=duration', '-of', 'csv=p=0', wav_path],
                                                 capture_output=True, text=True)
                            tts_dur = float(r2.stdout.strip() or 0)

        seg_path = os.path.join(seg_dir, f'seg_{sid:04d}.mp4')

        if stype == 'original':
            if not orig_video or not os.path.exists(orig_video):
                print(f"  ⚠️ 原视频不存在: {orig_video}")
                continue
            start = sc.get('start', 0)
            dur = sc.get('end', report_duration(work_dir)) - start
            dur = max(dur, 1)

            if use_original_audio:
                subprocess.run(['ffmpeg', '-y', '-ss', str(start),
                                '-i', orig_video, '-t', str(dur),
                                '-vf', f'scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2',
                                '-r', '25',
                                *video_encode_args(20), '-pix_fmt', 'yuv420p',
                                '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                                seg_path], capture_output=True)
            else:
                # 旁白短于片段时 apad 补静音，保证画面播满 dur（不用 -shortest 截断画面）
                subprocess.run(['ffmpeg', '-y', '-ss', str(start),
                                '-i', orig_video, '-i', wav_path,
                                '-t', str(dur),
                                '-vf', f'scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2',
                                '-r', '25',
                                *video_encode_args(20), '-pix_fmt', 'yuv420p',
                                '-af', 'apad',
                                '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                                seg_path], capture_output=True)
            last_orig_end = sc.get('end', start + dur)

        elif stype == 'image':
            # 全屏 Ken Burns 慢推（原视频时间线不前进）
            img = resolve_path(sc.get('source', ''), work_dir)
            # 时长由旁白朗读决定：旁白读完 + 0.4s 停顿再转场（不压缩内容）
            dur = float(sc.get('duration', 3))
            if wav_path and os.path.exists(wav_path) and tts_dur > 0:
                dur = tts_dur + 0.4
            if not os.path.exists(img):
                print(f"  ⚠️ 图片不存在: {img}"); continue

            # 全屏铺满 + 缓慢推近（zoompan Ken Burns），统一 25fps
            # 注意：单帧输入（不 -loop），d=总输出帧数；-loop 会让 on 每帧归零导致缩放不动
            # 输入放大到 2x 再 zoompan（取整误差减半）+ bicubic → 推镜更丝滑
            zoom = (f"z='min(1+0.0015*on,1.2)':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={max(int(round(dur*25)), 1)}:s={OUT_W}x{OUT_H}:fps=25")
            subprocess.run(['ffmpeg', '-y', '-i', img,
                            '-sws_flags', 'bicubic',
                            '-vf', f'scale={2*OUT_W}:{2*OUT_H}:force_original_aspect_ratio=1,'
                                    f'pad={2*OUT_W}:{2*OUT_H}:(ow-iw)/2:(oh-ih)/2,'
                                    f'zoompan={zoom}',
                            *video_encode_args(20), '-pix_fmt', 'yuv420p',
                            seg_path], capture_output=True)

            # 图片不推进原视频时间线

            # 音轨模式：audio: original = 保留原视频背景音频（无旁白）；
            #           默认 tts = 纯旁白（原视频背景音频静音）
            if not os.path.exists(seg_path):
                print(f"  ⚠️ 图片渲染失败: {img}"); continue
            use_img_audio = (sc.get('audio', 'tts') == 'original')
            inputs = ['-i', seg_path]   # 输入流索引: 0=片段, 1=旁白/背景
            fc = []
            if use_img_audio:
                # 原视频背景音频：从插入点前 0.5s 提取铺满图片时段（转场处声音连续）
                bg_wav = None
                if orig_video and os.path.exists(orig_video) and last_orig_end > 0.5:
                    orig_dur = probe_video_dur(orig_video) or report_duration(work_dir)
                    ss = max(last_orig_end - 0.5, 0)
                    if orig_dur > 0.5:
                        ss = min(ss, orig_dur - 0.5)
                    bg_wav = os.path.join(tmp_dir, f'bg_{sid}.wav')
                    r = subprocess.run(['ffmpeg', '-y',
                                        '-ss', str(ss),
                                        '-i', orig_video, '-t', str(max(12, dur + 1)),
                                        '-map', '0:a:0',
                                        '-ar', '44100', '-ac', '1', '-c:a', 'pcm_s16le',
                                        bg_wav], capture_output=True)
                    if r.returncode != 0 or not (os.path.exists(bg_wav) and os.path.getsize(bg_wav) > 1024):
                        bg_wav = None
                if bg_wav:
                    inputs += ['-i', bg_wav]
                    fc.append('[1:a]apad[bg]')
                else:
                    fc.append('anullsrc=r=44100:cl=mono[bg]')  # 无原视频时静音兜底
                fc.append('[bg]anull[a]')
            else:
                if wav_path and os.path.exists(wav_path):
                    inputs += ['-i', wav_path]
                    fc.append('[1:a]apad[tts]')          # 旁白不足 dur 时补静音
                else:
                    fc.append('anullsrc=r=44100:cl=mono[tts]')  # TTS 失败兜底静音
                fc.append('[tts]anull[a]')
            tmp_aud = seg_path + '.aud.mp4'
            r = subprocess.run(['ffmpeg', '-y'] + inputs + [
                '-filter_complex', ';'.join(fc),
                '-map', '0:v', '-map', '[a]',
                '-t', str(dur),
                '-c:v', 'copy',
                '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                tmp_aud], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"     ⚠️ image 音频混音失败: {r.stderr[-200:]}")
            if os.path.exists(tmp_aud):
                os.replace(tmp_aud, seg_path)

        elif stype == 'clip':
            # 有 TTS 旁白时由旁白时长决定（不压缩）；保留原声时按方案时长
            dur = float(sc.get('duration', 3))
            clip = resolve_path(sc.get('source', ''), work_dir)
            if not os.path.exists(clip):
                # 素材来源：搜索:前缀 → Pexels 下载；否则自动发现 短片/ 素材/ 目录
                src = (sc.get('source') or '').strip()
                is_search = src.startswith('搜索:') or src.startswith('搜索：')
                if is_search and allow_search:
                    term = src[3:].strip().lstrip(':：').strip()
                    orient = 'portrait' if OUT_H > OUT_W else 'landscape'
                    clips = ensure_scene_videos(term, max(dur, 5), orientation=orient)
                    clip = clips[0] if clips else ''
                elif auto_clips:
                    # 关了自动搜索时，搜索: 场景也走这条：用本地短片轮转顶，
                    # 顶不上再往下走原来的降级路（不联网）。
                    clip = auto_clips[auto_clip_used % len(auto_clips)]
                    auto_clip_used += 1
                    print(f"  📂 手动素材: {os.path.basename(clip)}")
            if not os.path.exists(clip):
                print(f"  ⚠️ 短片不存在: {clip}"); continue
            if wav_path and os.path.exists(wav_path) and tts_dur > 0:
                dur = max(dur, tts_dur + 0.4)
            use_clip_audio = (sc.get('audio', 'tts') == 'original')
            if use_clip_audio:
                # 保留短片原声，不足 dur 时 apad 补静音
                subprocess.run(['ffmpeg', '-y', '-i', clip,
                    '-vf', f'scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2',
                    '-r', '25',
                    *video_encode_args(20), '-pix_fmt', 'yuv420p',
                    '-af', 'apad',
                    '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                    '-t', str(dur),
                    seg_path], capture_output=True)
            elif wav_path and os.path.exists(wav_path):
                # 短片画面 + TTS，时长用 dur 控制
                clip_dur = float(subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'csv=p=0', clip],
                    capture_output=True, text=True).stdout.strip() or 0)
                try:
                    clip_dur = float(clip_dur)
                except:
                    clip_dur = 0
                if clip_dur > 0 and tts_dur > clip_dur + 0.2:
                    # TTS 比短片长：播放短片后冻结末帧等 TTS 播完
                    pad = tts_dur + 0.3 - clip_dur
                    subprocess.run(['ffmpeg', '-y', '-i', clip, '-i', wav_path,
                        '-filter_complex',
                        f'[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,'
                        f'pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,'
                        f'tpad=stop_mode=clone:stop_duration={pad}[v]',
                        '-map', '[v]', '-map', '1:a',
                        '-t', str(dur),
                        '-r', '25',
                        *video_encode_args(20), '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                        seg_path], capture_output=True)
                else:
                    # 短片比 TTS 长：用前 dur 秒，TTS 不足时 apad 补静音
                    subprocess.run(['ffmpeg', '-y', '-i', clip, '-i', wav_path,
                        '-map', '0:v', '-map', '1:a',
                        '-t', str(dur),
                        '-vf', f'scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2',
                        '-r', '25',
                        *video_encode_args(20), '-pix_fmt', 'yuv420p',
                        '-af', 'apad',
                        '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                        seg_path], capture_output=True)
            else:
                # TTS 失败：保留短片原声，不足 dur 时 apad 补静音
                subprocess.run(['ffmpeg', '-y', '-i', clip,
                    '-vf', f'scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=1,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2',
                    '-r', '25',
                    *video_encode_args(20), '-pix_fmt', 'yuv420p',
                    '-af', 'apad',
                    '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                    '-t', str(dur),
                    seg_path], capture_output=True)
            # 短片不推进原视频时间轴，下一个 original 从原位置继续

        elif stype == 'text':
            # 有 TTS 旁白时由旁白时长决定（不压缩）
            dur = float(sc.get('duration', 3))
            if wav_path and os.path.exists(wav_path) and tts_dur > 0:
                dur = max(dur, tts_dur + 0.4)
            text = sc.get('text', narration or '')
            safe = text.replace("'", "'\\\\''").replace(':', '\\:')
            # TTS 失败时用 anullsrc 静音轨兜底，保证 concat 音轨一致
            # 旁白输入：wav 文件走 -i；TTS 失败时用 lavfi 静音源兜底
            if wav_path and os.path.exists(wav_path):
                audio_in = ['-i', wav_path]
            else:
                audio_in = ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono']
            r = subprocess.run(['ffmpeg', '-y', '-f', 'lavfi',
                            '-i', f'color=c=#0f0f23:s={OUT_W}x{OUT_H}:d={dur}',
                            ] + audio_in + [
                            '-vf', f"drawtext=text='{safe}':fontfile={FONT}:"
                                   f"fontsize=40:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
                            '-r', '25',
                            *video_encode_args(20), '-pix_fmt', 'yuv420p',
                            '-af', 'apad',  # 音频补足到视频时长（音频<视频会导致 join 双流 demux 错位）
                            '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                            '-t', str(dur),
                            seg_path], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"     ⚠️ text 场景渲染失败: {r.stderr[-200:]}")

        # 字幕叠加 + 淡入淡出（合并到一次 ffmpeg，减少重编码）
        seg_dur = float(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', seg_path],
            capture_output=True, text=True).stdout.strip() or 0)
        try:
            seg_dur = float(seg_dur)
        except:
            seg_dur = 0
        if seg_dur > 0.5:
            final_path = seg_path + '.fn.mp4'
            vf_parts = []
            # 字幕：仅非原片场景，按标点分段（专业剪辑风格）
            if narration and stype != 'original':
                # 按标点拆分成短句
                phrases = []
                buf = ''
                for ch in narration:
                    buf += ch
                    if ch in '，。！？；、' and len(buf.replace('，。！？；、','')) >= 6:
                        phrases.append(buf)
                        buf = ''
                if buf:
                    phrases.append(buf)
                if not phrases:
                    phrases = [narration]
                # 生成 SRT（按字符比例分配时间）
                total_chars = sum(len(p) for p in phrases)
                t = 0.0
                srt_lines = []
                for pi, phrase in enumerate(phrases):
                    dur = seg_dur * len(phrase) / max(total_chars, 1)
                    dur = max(dur, 0.5)
                    def srt_ts(sec):
                        h = int(sec // 3600)
                        m = int((sec % 3600) // 60)
                        s = sec % 60
                        return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
                    srt_lines.append(f'{pi+1}')
                    srt_lines.append(f'{srt_ts(t)} --> {srt_ts(min(t+dur, seg_dur))}')
                    srt_lines.append(phrase)
                    srt_lines.append('')
                    t += dur
                srt_path = seg_path + '.srt'
                with open(srt_path, 'w', encoding='utf-8') as sf:
                    sf.write('\n'.join(srt_lines))
                # ASS 圆角半透明背景字幕（ass_subtitle.py，60px 大字，画布跟随输出尺寸）
                ass_path = seg_path + '.ass'
                make_ass_from_srt(srt_path, ass_path, offset=0,
                                  play_w=OUT_W, play_h=OUT_H)
                vf_parts.append(f"ass={ass_path}")
            # 淡入淡出交给 join 层的 xfade/acrossfade，段内不做（避免段尾静音塌陷）
            cmd = ['ffmpeg', '-y', '-i', seg_path]
            if vf_parts:
                cmd += ['-vf', ':'.join(vf_parts)]
            cmd += ['-r', '25',
                    *video_encode_args(20), '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '192k', final_path]
            r2 = subprocess.run(cmd, capture_output=True, text=True)
            if r2.returncode != 0:
                print(f"     ⚠️ 合成失败: {r2.stderr[:120]}")
            if os.path.exists(final_path):
                os.replace(final_path, seg_path)
        # 音轨兜底 + 记录最终元数据（字幕 pass 重编码后重新 probe 视频时长）
        if os.path.exists(seg_path):
            if not has_audio_stream(seg_path):
                print(f"     🔊 片段无音轨，补静音轨")
                ensure_audio(seg_path, seg_dur)
            elif probe_audio_dur(seg_path) < seg_dur - 0.1:
                # 音频比视频短：补静音到视频时长（否则 join 双流 demux 错位导致 xfade 截断）
                print(f"     🔊 片段音频短于视频，补静音轨")
                ensure_audio(seg_path, seg_dur)
            vdur = probe_video_dur(seg_path)
            seg_files.append({'path': seg_path, 'type': stype, 'dur': vdur or seg_dur})
        else:
            print(f"     ⚠️ 片段未生成: {stype}")

    if not seg_files:
        print("❌ 没有可渲染的场景"); shutil.rmtree(tmp_dir); return

    # 拼接：视频 xfade 交叉溶解 + 音频短淡入淡出叠加（三步法）
    FPS = 25  # 与原视频帧率一致

    if len(seg_files) == 1:
        print(f"\n   🧩 单个片段，直接使用")
        joined = os.path.join(tmp_dir, 'joined.mp4')
        shutil.copy2(seg_files[0]['path'], joined)
    else:
        N = len(seg_files)
        durs = [sf['dur'] for sf in seg_files]
        types = [sf['type'] for sf in seg_files]

        # 转场统一 0.16s（帧量化 4 帧）：0.4s 交叉会让段尾内容（最后一个字/画面）
        # 在渐出中播放，听感"字没播完就转场"；0.16s 重叠几乎无感，内容完整
        fades = []
        for k in range(1, N):
            fk = min(0.16, durs[k-1], durs[k])
            fades.append(round(fk * FPS) / FPS)

        print(f"\n   🧩 交叉溶解拼接 {N} 个片段...")
        print(f"      转场: " + ', '.join(f'{f}s' for f in fades))

        # 视频链：每输入统一 fps/像素格式；xfade offset = 当前合并总长 - fade
        v_parts = [f'[{i}:v]fps={FPS},format=yuv420p[v_in{i}]' for i in range(N)]
        offsets = [0.0]   # 每段画面在输出时间轴的起点（音频 adelay 对齐用）
        merged = durs[0]
        node = 'v_in0'
        for k in range(1, N):
            off = merged - fades[k-1]
            offsets.append(off)
            v_parts.append(
                f'[{node}][v_in{k}]xfade=transition=fade:'
                f'duration={fades[k-1]:.6f}:offset={off:.6f}[vx{k}]')
            node = f'vx{k}'
            merged += durs[k] - fades[k-1]
        v_parts.append(f'[{node}]format=yuv420p[vout]')

        # 音频链：每段独立 0.15s 淡入淡出（不吞句尾字）+ adelay 精确对齐到
        # 画面起点 + amix 叠加。acrossfade 无法兼得"内容起点对齐"和"短淡出"
        # （d 与视频 fade 相同才对齐，但 0.4s 淡出吞字；d 不同则时间轴错位）。
        AF = 0.15
        a_parts = []
        for i in range(N):
            a_parts.append(
                f'[{i}:a]aresample=44100,apad,'
                f'afade=t=in:st=0:d={AF},'
                f'afade=t=out:st={max(durs[i]-AF, 0):.3f}:d={AF},'
                f'adelay={int(offsets[i]*1000)}:all=1,'
                f'atrim=duration={merged:.3f}[a{i}]')
        a_parts.append(
            f'[{"][".join(f"a{i}" for i in range(N))}]'
            f'amix=inputs={N}:duration=longest:normalize=0[aout]')

        inputs = []
        for sf in seg_files:
            inputs += ['-i', sf['path']]

        # 三步拼接：视频链 / 音频链分开跑，再流拷贝 mux。
        # ffmpeg 4.4 的双流 filtergraph（视频+音频同时输出）存在 EOF 同步 bug，
        # 会截断视频链（xfade 输出在 offset 处终止），分步跑完全绕开。
        joined = os.path.join(tmp_dir, 'joined.mp4')
        joined_v = os.path.join(tmp_dir, 'joined_v.mp4')
        joined_a = os.path.join(tmp_dir, 'joined_a.m4a')
        r = subprocess.run(['ffmpeg', '-y'] + inputs + [
            '-filter_complex', ';'.join(v_parts),
            '-map', '[vout]',
            '-r', str(FPS),
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            joined_v], capture_output=True, text=True)
        r2 = subprocess.run(['ffmpeg', '-y'] + inputs + [
            '-filter_complex', ';'.join(a_parts),
            '-map', '[aout]',
            '-c:a', 'aac', '-b:a', '192k',
            joined_a], capture_output=True, text=True)
        ok = (r.returncode == 0 and r2.returncode == 0
              and os.path.exists(joined_v) and os.path.exists(joined_a))
        if ok:
            r3 = subprocess.run(['ffmpeg', '-y',
                                 '-i', joined_v, '-i', joined_a,
                                 '-map', '0:v', '-map', '1:a',
                                 '-c:v', 'copy', '-c:a', 'copy',
                                 joined], capture_output=True, text=True)
            ok = r3.returncode == 0 and os.path.exists(joined)
        if not ok:
            err = (r.stderr or '') + (r2.stderr or '')
            if not err:
                err = 'mux 失败'
            print(f"     ❌ 交叉溶解拼接失败: {err[-400:]}")
            print(f"     ⚠️ 回退 concat 硬切...")
            concat_txt = os.path.join(tmp_dir, 'concat.txt')
            with open(concat_txt, 'w') as f:
                for sf in seg_files:
                    f.write(f"file '{sf['path']}'\n")
            subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                            '-i', concat_txt,
                            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                            '-c:a', 'aac', '-b:a', '192k',
                            joined], check=True, capture_output=True)

    # 生成柔和环境音垫底（超低频嗡嗡声，不抢耳）
    ambient_wav = os.path.join(tmp_dir, 'ambient.wav')
    if not global_bgm or not os.path.exists(global_bgm):
        subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=80:duration=5,volume=0.02',
                        '-af', 'lowpass=f=150', ambient_wav],
                       capture_output=True)

    # BGM + 环境音混音
    final_out = os.path.join(work_dir, 'final_remix.mp4')
    joined_dur = float(subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', joined],
        capture_output=True, text=True).stdout.strip())

    if global_bgm and os.path.exists(global_bgm):
        print(f"   🎵 BGM 混音...")
        subprocess.run(['ffmpeg', '-y', '-i', joined, '-i', global_bgm,
                        '-filter_complex',
                        f'[1:a]volume=0.12,afade=t=in:d=2,afade=t=out:st={max(0,joined_dur-3)}:d=3[bgm];'
                        f'[0:a][bgm]amix=inputs=2:duration=first:normalize=0[out]',
                        '-map', '0:v', '-map', '[out]',
                        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                        final_out], capture_output=True)
    elif os.path.exists(ambient_wav):
        print(f"   🎵 添加环境音垫底...")
        # 循环环境音铺满全片
        subprocess.run(['ffmpeg', '-y', '-i', joined,
                        '-filter_complex',
                        f'[0:a]volume=1.0[main];'
                        f'amovie={ambient_wav}:loop=0,volume=0.08[amb];'
                        f'[main][amb]amix=inputs=2:duration=first:normalize=0[out]',
                        '-map', '0:v', '-map', '[out]',
                        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                        final_out], capture_output=True)
    else:
        shutil.copy2(joined, final_out)

    shutil.rmtree(tmp_dir)
    mb = os.path.getsize(final_out) / 1e6
    print(f"\n✅ 二创完成 → {final_out} ({mb:.0f}MB)")


def add_subtitle(seg_path, text):
    """给视频片段叠加字幕（白底黑边，底部居中）"""
    if not text or not os.path.exists(seg_path):
        return
    safe = text.replace("'", "'\\\\''").replace(':', '\\:').replace('\n', ' ')
    subed = seg_path + '.sub.mp4'
    subprocess.run(['ffmpeg', '-y', '-i', seg_path,
        '-vf', f"drawtext=text='{safe}':fontfile={FONT}:fontsize=28:"
               f"fontcolor=white:bordercolor=black:borderw=2:"
               f"x=(w-text_w)/2:y=h-80:box=0",
        *video_encode_args(20), '-pix_fmt', 'yuv420p',
        '-c:a', 'copy',
        subed], capture_output=True)
    if os.path.exists(subed):
        os.replace(subed, seg_path)


def resolve_path(path, work_dir):
    if not path:
        return ''
    return path if os.path.isabs(path) else os.path.join(work_dir, path)


def probe_video_dur(path):
    """probe 视频流时长（字幕 pass 重编码后最准），fallback 容器时长"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    s = r.stdout.strip()
    if s:
        try:
            return float(s)
        except ValueError:
            pass
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_audio_stream(path):
    """片段是否有音轨（无音轨时 xfade/acrossfade 链引用 [k:a] 会失败）"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                        '-show_entries', 'stream=index', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def probe_audio_dur(path):
    """probe 音频流时长（用于检测音频短于视频的片段）"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                        '-show_entries', 'stream=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def ensure_audio(path, dur):
    """无音轨片段补静音轨，保证 join 层音频链可用"""
    tmp = path + '.aud.mp4'
    subprocess.run(['ffmpeg', '-y',
                    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                    '-i', path,
                    '-map', '1:v', '-map', '0:a',
                    '-t', str(dur),
                    '-c:v', 'copy',
                    '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                    tmp], capture_output=True)
    if os.path.exists(tmp):
        os.replace(tmp, path)


def report_duration(work_dir):
    ap = os.path.join(work_dir, 'analysis.json')
    if os.path.exists(ap):
        with open(ap) as f:
            return json.load(f).get('duration', 0)
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    work_dir = sys.argv[1]
    bgm = None
    if '--bgm' in sys.argv:
        idx = sys.argv.index('--bgm')
        if idx + 1 < len(sys.argv):
            bgm = sys.argv[idx + 1]
    # 页面上的「自动搜索」开关（见 web/pipelines.py）
    render(work_dir, bgm, allow_search='--no-search' not in sys.argv)
