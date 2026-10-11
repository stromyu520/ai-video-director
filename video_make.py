#!/usr/bin/env python3
"""video_make.py — 读 director.yaml → 合成成品视频

用法:
  python video_make.py <director.yaml路径>

依赖: moviepy, pyyaml, Pillow (video-env 已装)
"""

import sys, os, subprocess, tempfile, shutil, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P

from moviepy import *


# ============================================================
# 配置
# ============================================================
PYTHON = P.PYTHON
TTS    = str(P.ROOT / 'tts_express.py')
W      = 1920
H      = 1080
FONT   = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'  # 中文黑体


def load(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def synth_scene(text, spk_id, express, speed, out_path):
    """调 tts_express.py 合成一段旁白"""
    cmd = [PYTHON, TTS, text, '--spk-id', spk_id, '--output', out_path]
    if express:
        cmd.append('--express')
    if speed and speed != '中':
        cmd.extend(['--speed', speed])
    subprocess.run(cmd, check=True, capture_output=True)


def txt_card(text, bg='#0f0f23', fg='white', size=64):
    """中文文本卡片（指定中文字体防乱码）"""
    return TextClip(
        text=text,
        font=FONT,
        font_size=size,
        color=fg,
        stroke_color='black',
        stroke_width=2,
        bg_color=bg,
        size=(W, H),
        text_align='center',
        method='caption',
    )


def img_clip(path):
    """图片画面（PIL加载→numpy→moviepy，避免resized bug）"""
    from PIL import Image
    import numpy as np
    pil = Image.open(path).convert('RGB').resize((W, H), Image.LANCZOS)
    return ImageClip(np.array(pil))


def mk_subtitle(text, t_start, t_dur):
    """字幕：半透明黑底条 + 白色文字"""
    bar = ColorClip(size=(W, 100), color=(0, 0, 0), duration=t_dur).with_opacity(0.5)
    txt = TextClip(
        text=text,
        font=FONT,
        font_size=28,
        color='white',
        stroke_color='black',
        stroke_width=1,
        method='label',
        duration=t_dur,
    ).with_position('center')
    sub = CompositeVideoClip([bar, txt], size=(W, 100)).with_duration(t_dur)
    return sub.with_position(('center', H - 100)).with_start(t_start)


def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <director.yaml>")
        sys.exit(1)

    yaml_path = sys.argv[1]
    work_dir  = os.path.dirname(os.path.abspath(yaml_path)) or '.'
    tmp_dir   = tempfile.mkdtemp(prefix='vm_', dir=work_dir)

    director = load(yaml_path)
    vc = director['voice']

    print(f"🎬 开始制作: {director['project']['title']}")
    print(f"   音色: {vc.get('spk_id','?')}  express={vc.get('express',False)}")

    clips = []

    # ============================================================
    # 逐场景处理
    # ============================================================
    for sc in director['scenes']:
        sid  = sc['id']
        txt  = sc['narration']
        spoken = sc.get('spoken', txt)  # 优先用 spoken 做语音
        mood = sc.get('mood', '平实')
        spd  = sc.get('speed', vc.get('speed', '中'))
        vis  = sc.get('visuals', {})
        vtyp = vis.get('type', 'auto')
        vsrc = vis.get('source', '')

        print(f"\n   🎬 场景{sid} [{sc['start']}s] {mood} | {spd}速")
        print(f"      {txt[:50]}...")

        # 1. 合成旁白（用 spoken 文本，数字转中文防读错）
        audio_file = os.path.join(tmp_dir, f's{sid}.wav')
        try:
            synth_scene(spoken, vc['spk_id'], vc.get('express', False), spd, audio_file)
        except subprocess.CalledProcessError as e:
            print(f"      ❌ TTS 失败: {e}")
            continue
        audio = AudioFileClip(audio_file)
        dur   = audio.duration
        print(f"      🔊 {dur:.1f}s")

        # 2. 创建画面（深色背景 + 主题文字，避免全黑）
        mood_bg = {
            '悬念': '#1a1a3e', '兴奋': '#1a3e1a', '高潮': '#3e1a1a',
            '搞笑': '#2e2e1a', '沉重': '#1a1a1a', '收尾': '#1a1a2e',
        }.get(mood, '#0f0f23')

        if vtyp == 'text' and vsrc:
            visual = txt_card(vsrc, bg=mood_bg, size=72)
        elif vtyp == 'image':
            ip = vsrc if os.path.isabs(vsrc) else os.path.join(work_dir, vsrc)
            if os.path.exists(ip):
                visual = img_clip(ip)
            else:
                visual = txt_card(f'📌 {mood}\n{txt[:40]}', bg=mood_bg, size=60)
        else:
            # auto → 用解说词做文字卡片
            visual = txt_card(txt, bg=mood_bg, size=56)

        visual = visual.with_duration(dur).with_audio(audio)
        clips.append(visual)

    if not clips:
        print("❌ 没有合成任何场景")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # ============================================================
    # 拼接
    # ============================================================
    print(f"\n   🧩 拼接 {len(clips)} 个场景...")
    final = concatenate_videoclips(clips, method='compose')

    # ============================================================
    # BGM
    # ============================================================
    bgm_cfg = director.get('bgm', {})
    bgm_path = bgm_cfg.get('path', '')
    if bgm_path and os.path.exists(bgm_path):
        print(f"   🎵 BGM: {bgm_path}")
        bgm = AudioFileClip(bgm_path)
        bgm = bgm.with_effects([
            afx.MultiplyVolume(bgm_cfg.get('volume', 0.25)),
            afx.AudioFadeIn(bgm_cfg.get('fade_in', 2)),
            afx.AudioFadeOut(bgm_cfg.get('fade_out', 3)),
        ])
        bgm = bgm.with_duration(final.duration)
        final = final.with_audio(afx.CompositeAudioClip([final.audio, bgm]))

    # ============================================================
    # 字幕（基于实际拼接位置，而非 yaml 里的 start 值）
    # ============================================================
    out_cfg = director.get('output', {})
    if out_cfg.get('subtitle', True):
        print(f"   📝 叠加字幕...")
        # 用实际累积时长计算字幕位置
        t = 0.0
        sub_clips = []
        for sc, clip in zip(director['scenes'], clips):
            txt = sc['narration']
            sub_clips.append(mk_subtitle(txt, t, clip.duration))
            t += clip.duration  # 累积实际时长
        final = CompositeVideoClip([final] + sub_clips, size=(W, H))

    # ============================================================
    # 输出
    # ============================================================
    out_path = out_cfg.get('path', '') or os.path.join(work_dir, 'output.mp4')
    print(f"\n   💾 渲染中 → {out_path}")
    final.write_videofile(
        out_path,
        fps=24,
        codec='libx264',
        audio_codec='aac',
        bitrate='5000k',
        preset='medium',
        logger=None,
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n✅ 完成 → {out_path}  ({final.duration:.1f}s)")


if __name__ == '__main__':
    main()
