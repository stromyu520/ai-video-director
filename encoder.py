#!/usr/bin/env python3
"""encoder.py — 视频编码器选择：h264_nvenc 优先，失败自动回退 libx264

环境检出 NVIDIA NVENC（RTX 4060）后，中间步骤（镜头渲染/xfade 拼接）
用 nvenc 恒定质量模式（-cq）提速数倍；终稿保留 libx264（多代编码下
软编画质最稳，字幕文字边缘更实）。nvenc 运行失败（驱动/资源占用）
自动回退 libx264 重试一次，保证管线不中断。

用法:
  from encoder import run_encode, video_encode_args
  run_encode(['ffmpeg','-y','-i',src, '-pix_fmt','yuv420p', out], crf=20)
  # out 必须是命令末位，编码器参数自动插到 out 前
"""

import functools
import subprocess

CRF_DEFAULT = 20


@functools.lru_cache(maxsize=1)
def detect_video_encoder():
    """探测可用编码器：h264_nvenc > libx264"""
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                           capture_output=True, text=True, timeout=30)
        if 'h264_nvenc' in (r.stdout + r.stderr):
            return 'h264_nvenc'
    except Exception:
        pass
    return 'libx264'


def video_encode_args(crf=CRF_DEFAULT):
    """编码器参数（-c:v + 质量参数），插在输出文件前"""
    if detect_video_encoder() == 'h264_nvenc':
        # nvenc 恒定质量：-cq ≈ libx264 -crf 同量级；-b:v 0 必须显式，否则走默认码率模式
        return ['-c:v', 'h264_nvenc', '-cq', str(crf), '-preset', 'p5', '-b:v', '0']
    return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf)]


def run_encode(args, crf=CRF_DEFAULT, hw=True, **kw):
    """执行 ffmpeg 编码命令；args 末位必须是输出路径，且不含编码器参数。

    hw=True（默认，中间步骤）：优先 nvenc，运行失败自动回退 libx264 重试一次；
    hw=False（终稿）：固定 libx264（画质锚点）。
    其余 kw（如 text=True）透传给 subprocess.run；check 恒为 True。
    成功返回 CompletedProcess；重试后仍失败则抛 CalledProcessError。
    """
    kw.pop('check', None)
    kw.pop('capture_output', None)
    out = args[-1]
    head = args[:-1]
    if hw:
        candidates = [video_encode_args(crf)]
        if candidates[0][1] != 'libx264':
            candidates.append(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf)])
    else:
        candidates = [['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf)]]
    for i, enc in enumerate(candidates):
        try:
            return subprocess.run(head + enc + [out], check=True, capture_output=True, **kw)
        except subprocess.CalledProcessError:
            if i < len(candidates) - 1:
                print("  ⚠️ 硬件编码失败，回退 libx264 重试")
                continue
            raise
