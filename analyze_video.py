#!/usr/bin/env python3
"""analyze_video.py — 分析原视频：镜头检测 + 音频转录 + 关键帧 VLM

用法:
  python analyze_video.py <视频.mp4> [输出目录]

输出:
  <输出目录>/analysis.json  # 分析报告
"""
import sys, os, json, subprocess, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P

VLM_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vlm_analyze.py')
VLM_PYTHON = P.VLM_PYTHON

SCENE_THRESHOLD = 0.3
MIN_SCENE_DUR   = 1.5


def analyze(video_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    report = {'source': os.path.abspath(video_path), 'duration': 0, 'scenes': [], 'transcript': []}

    # 1. 时长
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', video_path],
                       capture_output=True, text=True)
    report['duration'] = float(r.stdout.strip())
    print(f"📹 时长: {report['duration']:.1f}s")

    # 2. 镜头检测（基于 I 帧 + scene detect）
    print(f"📹 镜头检测...")
    # 这一步整片解码，耗时时长成正比。原来硬编码 120s 且不在 try 内 ——
    # 任何超过约 2 分钟的视频都会直接 traceback。给 4 倍时长余量，下限 120s。
    decode_timeout = max(120, report['duration'] * 4)
    r = subprocess.run([
        'ffmpeg', '-i', video_path, '-vf', 'showinfo', '-f', 'null', '-'
    ], capture_output=True, text=True, timeout=decode_timeout)

    scenes = []
    seen_times = set()
    for line in r.stderr.split('\n'):
        if 'pts_time:' in line:
            m = re.search(r'pts_time:([\d.]+)', line)
            if m:
                t = round(float(m.group(1)), 1)
                if t not in seen_times and t >= 0:
                    seen_times.add(t)

    # 按时间排序
    seen_times = sorted(seen_times)
    if seen_times:
        scenes = [seen_times[0]]
        for t in seen_times[1:]:
            if t - scenes[-1] >= MIN_SCENE_DUR:
                scenes.append(t)
    if not scenes or scenes[0] > 0:
        scenes.insert(0, 0.0)
    if scenes[-1] < report['duration'] - 0.5:
        scenes.append(report['duration'])

    print(f"  → {len(scenes)-1} 个镜头")

    # 3. 提取音频 + Whisper 转录
    print(f"🎤 音频转录...")
    audio_wav = os.path.join(out_dir, '_audio.wav')
    subprocess.run(['ffmpeg', '-y', '-i', video_path, '-ar', '24000', '-ac', '1', audio_wav],
                   capture_output=True)

    transcript = []
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(str(P.WHISPER_MODEL),
                              device='cuda', compute_type='float16')
        segs, _ = model.transcribe(audio_wav, language='zh', beam_size=5, word_timestamps=True)
        for s in segs:
            transcript.append({'start': round(s.start, 1), 'end': round(s.end, 1), 'text': s.text.strip()})
    except Exception as e:
        print(f"  ⚠️ 转录失败: {e}")

    report['transcript'] = transcript
    print(f"  → {len(transcript)} 条字幕")

    # 4. 关键帧提取（不做 VLM）
    # VLM 的触发条件是「**手动上传的、是图片的**素材」—— 见 video_make_ffmpeg.is_manual_image()。
    # 这里是原片抽出来的帧：既不是用户上传的图片，数量还跟着镜头数走
    # （一个 8 镜头的片子就是 8 张 × 10s + 4.2GB 显存），因此不跑。
    # 帧本身照常抽出来写到 _keyframes/，供人工核对。
    print(f"🖼️ 关键帧提取...")
    vlm_ok = False
    keyframe_dir = os.path.join(out_dir, '_keyframes')
    os.makedirs(keyframe_dir, exist_ok=True)

    scene_details = []
    for i in range(len(scenes) - 1):
        ts, te = scenes[i], scenes[i+1]
        mid = (ts + min(ts + 1, te))  # 取前半秒
        kf = os.path.join(keyframe_dir, f'scene_{i:04d}.jpg')
        subprocess.run(['ffmpeg', '-y', '-ss', str(mid), '-i', video_path,
                        '-frames:v', '1', kf], capture_output=True)

        scene_text = ' '.join(
            s['text'] for s in transcript if s['start'] < te and s['end'] > ts
        )

        vlm = {}
        if vlm_ok and os.path.exists(kf) and os.path.getsize(kf) > 1000:
            try:
                r = subprocess.run([VLM_PYTHON, VLM_SCRIPT, kf, '--output-format', 'json'],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    vlm = json.loads(r.stdout)
            except:
                pass

        scene_details.append({
            'id': i+1,
            'start': round(ts, 1),
            'end': round(te, 1),
            'duration': round(te - ts, 1),
            'narration': scene_text[:200],
            'vlm': {
                'visual_key': vlm.get('visual_key', ''),
                'text_content': vlm.get('text_content', ''),
                'match': vlm.get('match', ''),
            } if vlm else {}
        })

    report['scenes'] = scene_details

    report_path = os.path.join(out_dir, 'analysis.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 分析完成 → {report_path}")
    return report_path


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    video_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else '.'
    if not os.path.exists(video_path):
        print(f"❌ 视频不存在: {video_path}")
        sys.exit(1)
    analyze(video_path, out_dir)
