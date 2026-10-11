#!/usr/bin/env python3
"""bgm_prep.py — BGM预处理：提取/裁剪/人声分离/循环/标准化

用法:
  # 提取音频（自动规范化）
  python bgm_prep.py 音乐.mp3 --output BGM/轻快.wav

  # 从短视频提取纯BGM（人声分离）
  python bgm_prep.py 抖音视频.mp4 --separate --output BGM/纯音乐.wav

  # 裁剪片段 + 循环
  python bgm_prep.py 音乐.mp3 --start 10 --end 30 --loop-to 60

  # 从视频提取+分离+循环
  python bgm_prep.py 抖音视频.mp4 --separate --start 5 --end 25 --loop-to 90
"""

import sys, os, subprocess, argparse, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P
import numpy as np

DEMUCS_REPO = str(P.DEMUCS_REPO)

SAMPLE_RATE = 24000


def extract_audio(src, tmp_wav):
    subprocess.run([
        'ffmpeg', '-y', '-i', src,
        '-ar', str(SAMPLE_RATE), '-ac', '1', tmp_wav
    ], capture_output=True, check=True)


def trim_audio(src_wav, out_wav, start, end):
    cmd = ['ffmpeg', '-y', '-i', src_wav, '-ar', str(SAMPLE_RATE), '-ac', '1']
    if start is not None:
        cmd += ['-ss', str(start)]
    if end is not None:
        cmd += ['-to', str(end)]
    cmd.append(out_wav)
    subprocess.run(cmd, capture_output=True, check=True)


def get_duration(wav):
    r = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries',
        'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', wav
    ], capture_output=True, text=True)
    return float(r.stdout.strip())


def separate(src_wav, out_dir):
    """Demucs 人声分离：返回 (纯BGM路径, 人声路径)"""
    from pathlib import Path
    from demucs.api import Separator
    separator = Separator(model='htdemucs', repo=Path(DEMUCS_REPO),
                          device='cuda', jobs=0, progress=True)
    origin, stems = separator.separate_audio_file(src_wav)
    # 4-stems: drums, bass, other, vocals → BGM = drums + bass + other
    base = os.path.splitext(os.path.basename(src_wav))[0]
    out_sub = os.path.join(out_dir, 'htdemucs', base)
    os.makedirs(out_sub, exist_ok=True)
    noise_path = os.path.join(out_sub, 'no_vocals.wav')
    vocals_path = os.path.join(out_sub, 'vocals.wav')
    import soundfile as sf
    # 合成纯BGM（鼓+贝斯+其他）
    bgm = stems['drums'].cpu().numpy() + stems['bass'].cpu().numpy() + stems['other'].cpu().numpy()
    sf.write(noise_path, bgm.T, separator.samplerate)
    sf.write(vocals_path, stems['vocals'].cpu().numpy().T, separator.samplerate)
    return noise_path, vocals_path


def loop_with_crossfade(src_wav, out_wav, target_dur, crossfade=2):
    import soundfile as sf
    data, sr = sf.read(src_wav)
    seg_len = len(data)
    if seg_len == 0:
        return False
    needed = int(np.ceil(target_dur * sr / seg_len))
    result = np.tile(data, needed)[:int(target_dur * sr)]
    # 循环衔接处交叉淡化
    fade_len = min(int(crossfade * sr), seg_len // 2)
    for i in range(1, needed):
        offset = i * seg_len
        if offset >= len(result):
            break
        fade_end = min(offset, len(result))
        fade_out = np.linspace(1, 0, fade_len)
        fade_in  = np.linspace(0, 1, fade_len)
        # 前段淡出
        result[offset - fade_len:offset] *= fade_out[:fade_len]
        # 后段淡入
        if offset + fade_len <= len(result):
            result[offset:offset + fade_len] *= fade_in[:fade_len]
        # 叠加
        overlap = min(fade_len, len(result) - offset + fade_len)
        if overlap > 0:
            result[offset - fade_len:offset - fade_len + overlap] += (
                data[-fade_len:-fade_len + overlap] * fade_out[:overlap]
                + data[:overlap] * fade_in[:overlap]
            ) / 2
    # 音量标准化
    peak = np.max(np.abs(result))
    if peak > 0:
        result *= 0.89 / peak
    sf.write(out_wav, result, sr)
    return True


def normalize_volume(src_wav, out_wav):
    import soundfile as sf
    data, sr = sf.read(src_wav)
    peak = np.max(np.abs(data))
    if peak > 0:
        data = data * (0.89 / peak)
    sf.write(out_wav, data, sr)


def main():
    p = argparse.ArgumentParser(description='BGM预处理')
    p.add_argument('input', help='源文件（音频或视频）')
    p.add_argument('--output', '-o', default='', help='输出路径')
    p.add_argument('--start', type=float, help='裁剪起始秒')
    p.add_argument('--end', type=float, help='裁剪结束秒')
    p.add_argument('--loop-to', type=float, help='循环至目标时长（秒）')
    p.add_argument('--fade-in', type=float, default=2, help='淡入秒数')
    p.add_argument('--fade-out', type=float, default=3, help='淡出秒数')
    p.add_argument('--separate', action='store_true', help='人声分离（Demucs），提取纯BGM')
    args = p.parse_args()

    src = args.input
    if not os.path.exists(src):
        print(f"❌ 文件不存在: {src}")
        sys.exit(1)

    tmp = tempfile.mkdtemp(prefix='bgm_')
    tmp_wav = os.path.join(tmp, 'raw.wav')
    out = args.output

    try:
        # 1. 提取音频
        print(f"📥 提取音频: {src}")
        extract_audio(src, tmp_wav)
        dur = get_duration(tmp_wav)
        print(f"   原始时长: {dur:.1f}s")

        # 2. 裁剪
        seg = tmp_wav
        if args.start is not None or args.end is not None:
            s = args.start or 0
            e = args.end or dur
            print(f"✂️  裁剪 [{s:.1f}s - {e:.1f}s]")
            trimmed = os.path.join(tmp, 'trim.wav')
            trim_audio(tmp_wav, trimmed, s, e)
            seg = trimmed
            dur = get_duration(seg)

        # 3. 人声分离
        if args.separate:
            print(f"🔊 Demucs 人声分离中...")
            noise, vocals = separate(seg, tmp)
            if os.path.exists(noise) and os.path.getsize(noise) > 1000:
                print(f"   ✅ 纯BGM提取成功（已去除人声）")
                seg = noise
            else:
                print(f"   ⚠ 分离结果异常，使用原始音频")

        seg_dur = get_duration(seg)
        print(f"   片段时长: {seg_dur:.1f}s")

        # 4. 循环
        final = seg
        if args.loop_to:
            print(f"🔁 循环至 {args.loop_to:.0f}s")
            looped = os.path.join(tmp, 'loop.wav')
            loop_with_crossfade(seg, looped, args.loop_to, args.fade_in)
            final = looped
            final_dur = args.loop_to
        else:
            normalized = os.path.join(tmp, 'norm.wav')
            normalize_volume(seg, normalized)
            final_dur = seg_dur
            final = normalized

        # 5. 输出
        if not out:
            base = os.path.splitext(os.path.basename(src))[0]
            out = str(P.BGM_DIR / f'{base}.wav')

        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        subprocess.run(['ffmpeg', '-y', '-i', final,
                       '-af', f'afade=t=in:d={args.fade_in},afade=t=out:st={final_dur-args.fade_out}:d={args.fade_out}',
                       '-ar', str(SAMPLE_RATE), '-ac', '1', out],
                      capture_output=True, check=True)
        print(f"\n✅ BGM → {out}  ({final_dur:.1f}s)")

    finally:
        subprocess.run(['rm', '-rf', tmp], capture_output=True)


if __name__ == '__main__':
    main()
