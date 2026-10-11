#!/usr/bin/env python3
"""xfade_join.py — 视频片段交叉溶解拼接（从 render_remix.py 三步法提取泛化）

用法:
  import xfade_join
  ok, joined_v, joined_a = xfade_join.join_with_xfade(
      video_segs, audio_segs, tmp_dir)
  if not ok:
      ok, joined_v, joined_a = xfade_join.concat_join(video_segs, audio_segs, tmp_dir)

数据结构:
  video_segs: [{path, dur, scene_idx}]  镜头（无音轨），scene_idx 用于回退路径切片
  audio_segs: [{path, dur, start}]      场景音频，start = 场景在全局时间轴的起点（秒）
  dur: 各段渲染时长（秒）

三步法（绕开 ffmpeg 4.4 双流 filtergraph EOF 同步 bug）:
  1. 视频链单独跑：逐段 fps/format 归一 + 链式 xfade（fade 帧量化，offset = 当前总长 - fade）
  2. 音频链单独跑：每段 0.15s 淡入淡出 + adelay 对齐场景起点 + amix normalize=0
  3. -c:v copy -c:a copy mux
"""

import os, shutil, subprocess

from encoder import run_encode

DEFAULT_FPS = 25
DEFAULT_FADE = 0.16       # 交叉溶解时长（帧量化到整帧）
DEFAULT_AUDIO_FADE = 0.15 # 每场景音频淡入淡出


# ============================================================
# 时长/偏移计算（build_shots 与 join 共用，避免公式漂移）
# ============================================================
def compute_offsets(durs, fade=DEFAULT_FADE, fps=DEFAULT_FPS):
    """逐段画面在输出时间轴的起点 + 相邻段转场时长

    返回 (offsets, fades)；输出总长 = offsets[-1] + durs[-1]
    """
    n = len(durs)
    if n == 0:
        return [], []
    fades = []
    for k in range(1, n):
        fk = min(fade, durs[k - 1], durs[k])
        fades.append(round(fk * fps) / fps)
    offsets = [0.0]
    for k in range(1, n):
        offsets.append(offsets[k - 1] + durs[k - 1] - fades[k - 1])
    return offsets, fades


# ============================================================
# ffmpeg 辅助
# ============================================================
def probe_video_dur(path):
    """视频流时长，fallback 容器时长"""
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


def probe_audio_dur(path):
    """音频流时长"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                        '-show_entries', 'stream=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_audio_stream(path):
    """是否有音轨"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                        '-show_entries', 'stream=index', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def ensure_audio(path, dur):
    """无音轨片段补静音轨（回退路径兜底）"""
    tmp = path + '.aud.mp4'
    subprocess.run(['ffmpeg', '-y',
                    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                    '-i', path,
                    '-map', '1:v', '-map', '0:a',
                    '-t', str(dur),
                    '-c:v', 'copy',
                    '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                    tmp], capture_output=True)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)


# ============================================================
# 主拼接
# ============================================================
def join_with_xfade(video_segs, audio_segs, tmp_dir,
                    fps=DEFAULT_FPS, fade=DEFAULT_FADE,
                    audio_fade=DEFAULT_AUDIO_FADE):
    """交叉溶解拼接。返回 (ok, joined_v, joined_a) — 视频/音频分开输出

    失败返回 (False, '', '')，调用方回退 concat_join
    """
    joined_v = os.path.join(tmp_dir, 'xv_joined_v.mp4')
    joined_a = os.path.join(tmp_dir, 'xv_joined_a.m4a')

    if not video_segs or not audio_segs:
        return False, '', ''

    # ---------- 单段：直接复用 ----------
    if len(video_segs) == 1:
        shutil.copy2(video_segs[0]['path'], joined_v)
        r = subprocess.run(['ffmpeg', '-y', '-i', audio_segs[0]['path'],
                            '-c:a', 'aac', '-b:a', '192k',
                            '-ar', '44100', '-ac', '1',
                            joined_a], capture_output=True)
        return (r.returncode == 0 and os.path.exists(joined_v)
                and os.path.exists(joined_a)), joined_v, joined_a

    # ---------- 视频链 ----------
    N = len(video_segs)
    durs = [s['dur'] for s in video_segs]
    offsets, fades = compute_offsets(durs, fade, fps)
    total = offsets[-1] + durs[-1]

    v_parts = [f'[{i}:v]fps={fps},format=yuv420p[v_in{i}]' for i in range(N)]
    node = 'v_in0'
    for k in range(1, N):
        v_parts.append(
            f'[{node}][v_in{k}]xfade=transition=fade:'
            f'duration={fades[k-1]:.6f}:offset={offsets[k]:.6f}[vx{k}]')
        node = f'vx{k}'
    v_parts.append(f'[{node}]format=yuv420p[vout]')

    inputs = []
    for s in video_segs:
        inputs += ['-i', s['path']]
    try:
        run_encode(['ffmpeg', '-y'] + inputs + [
            '-filter_complex', ';'.join(v_parts),
            '-map', '[vout]',
            '-r', str(fps),
            '-pix_fmt', 'yuv420p',
            joined_v], capture_output=True, text=True)
        r1_ok, r1_err = True, ''
    except subprocess.CalledProcessError as e:
        r1_ok, r1_err = False, (e.stderr or '')

    # ---------- 音频链 ----------
    a_parts = []
    for i, s in enumerate(audio_segs):
        # st 超出音频长度时 ffmpeg 不产生淡出效果（无副作用）
        a_parts.append(
            f'[{i}:a]aresample=44100,apad,'
            f'afade=t=in:st=0:d={audio_fade},'
            f'afade=t=out:st={max(s["dur"] - audio_fade, 0):.3f}:d={audio_fade},'
            f'adelay={int(s["start"] * 1000)}:all=1,'
            f'atrim=duration={total:.3f}[a{i}]')
    a_parts.append(
        f'[{"][".join(f"a{i}" for i in range(len(audio_segs)))}]'
        f'amix=inputs={len(audio_segs)}:duration=longest:normalize=0[aout]')

    a_inputs = []
    for s in audio_segs:
        a_inputs += ['-i', s['path']]
    r2 = subprocess.run(['ffmpeg', '-y'] + a_inputs + [
        '-filter_complex', ';'.join(a_parts),
        '-map', '[aout]',
        '-c:a', 'aac', '-b:a', '192k',
        joined_a], capture_output=True, text=True)

    ok = (r1_ok and r2.returncode == 0
          and os.path.exists(joined_v) and os.path.exists(joined_a))
    if not ok:
        err = (r1_err or '')[-300:] + (r2.stderr or '')[-300:]
        print(f"   ❌ 交叉溶解拼接失败: {err}")
        return False, '', ''
    return True, joined_v, joined_a


def concat_join(video_segs, audio_segs, tmp_dir):
    """回退：硬切拼接。镜头无音轨 → 先按场景内相对位置切片 mux 场景音频

    返回 (ok, joined_v, joined_a)
    """
    joined_v = os.path.join(tmp_dir, 'xv_joined_v.mp4')
    joined_a = os.path.join(tmp_dir, 'xv_joined_a.m4a')

    # ---------- 音频链（与 join 相同，硬切不损耗时长） ----------
    if len(audio_segs) == 1:
        r = subprocess.run(['ffmpeg', '-y', '-i', audio_segs[0]['path'],
                            '-c:a', 'aac', '-b:a', '192k',
                            '-ar', '44100', '-ac', '1',
                            joined_a], capture_output=True)
        if r.returncode != 0 or not os.path.exists(joined_a):
            return False, '', ''
    else:
        total = sum(s['dur'] for s in audio_segs)
        a_parts = []
        for i, s in enumerate(audio_segs):
            a_parts.append(
                f'[{i}:a]aresample=44100,apad,'
                f'adelay={int(s["start"] * 1000)}:all=1,'
                f'atrim=duration={total:.3f}[a{i}]')
        a_parts.append(
            f'[{"][".join(f"a{i}" for i in range(len(audio_segs)))}]'
            f'amix=inputs={len(audio_segs)}:duration=longest:normalize=0[aout]')
        a_inputs = []
        for s in audio_segs:
            a_inputs += ['-i', s['path']]
        r = subprocess.run(['ffmpeg', '-y'] + a_inputs + [
            '-filter_complex', ';'.join(a_parts),
            '-map', '[aout]',
            '-c:a', 'aac', '-b:a', '192k',
            joined_a], capture_output=True)
        if r.returncode != 0 or not os.path.exists(joined_a):
            return False, '', ''

    # ---------- 视频链：镜头按场景切片 mux 音轨后 concat ----------
    # 按 scene_idx 分组，组内相对偏移 = 前一镜头时长累加（硬切无损耗）
    if not video_segs:
        return False, '', ''
    scenes = {}
    for s in video_segs:
        scenes.setdefault(s.get('scene_idx', 0), []).append(s)

    muxed = []
    for idx, segs in sorted(scenes.items()):
        rel = 0.0
        audio = audio_segs[idx] if idx < len(audio_segs) else None
        for s in segs:
            out = s['path'] + '.mux.mp4'
            if audio:
                cmd = ['ffmpeg', '-y', '-ss', f'{rel:.3f}', '-i', audio['path'],
                       '-i', s['path'], '-map', '1:v', '-map', '0:a',
                       '-t', str(s['dur']), '-af', 'apad',
                       '-c:v', 'copy',
                       '-c:a', 'aac', '-ar', '44100', '-ac', '1', '-b:a', '192k',
                       out]
            else:
                cmd = ['ffmpeg', '-y', '-i', s['path'], '-c:v', 'copy',
                       '-an', out]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                # 兜底：给镜头补静音轨再 concat
                shutil.copy2(s['path'], out)
                ensure_audio(out, s['dur'])
            muxed.append(out)
            rel += s['dur']

    concat_txt = os.path.join(tmp_dir, 'xv_concat.txt')
    with open(concat_txt, 'w') as f:
        for p in muxed:
            f.write(f"file '{p}'\n")
    try:
        run_encode(['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                    '-i', concat_txt,
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '192k',
                    joined_v], capture_output=True)
    except subprocess.CalledProcessError:
        return False, '', ''
    if not os.path.exists(joined_v):
        return False, '', ''
    return True, joined_v, joined_a
