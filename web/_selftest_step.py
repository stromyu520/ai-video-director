"""_selftest_step.py — 阶段 0 用的假步骤脚本（不是管线的一部分）

存在的唯一目的：在不接真实管线的前提下，实测运行器的三件承重事。
真实脚本全是 print + sleep + ffmpeg，这里的每个模式都对应一个真实风险。

    emit   [n]  n 行 1s 间隔，再静默 3s，最后收尾 —— 验 stdout 缓冲
    stop   [n]  n 行后 SIGSTOP 自己      —— 验看门狗（进程假死、无输出）
    spawn      起一个 sleep 999 孙进程   —— 验 killpg 能否一次带走整组
    markers     打印真实管线的进度标记    —— 验 ProgressTracker 解析
    boom        打到 stderr 后退出码 3    —— 验 stderr 合并与失败捕获
"""
import os
import signal
import subprocess
import sys
import time


def emit(n):
    for i in range(n):
        print(f'line {i} t={time.time():.3f}')
        time.sleep(1.0)
    time.sleep(3.0)
    print('done')


def stop(n):
    for i in range(n):
        print(f'line {i}')
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGSTOP)      # 假死：读端永远等不到下一行
    time.sleep(300)


def spawn():
    p = subprocess.Popen(['sleep', '999'])    # 不 setsid → 与父同组
    print(f'grandchild {p.pid}', flush=True)
    print(f'self {os.getpid()}', flush=True)
    time.sleep(999)


def markers():
    print('🎬 场景1 平实 | 4.2s')
    print('🎥 共 3 个镜头')
    print('🎤 Phase 2/2: 语音合成与字幕（3 场景）')
    for i in (1, 2, 3):
        print(f'🎬 场景{i} 平实 | 4.2s')
        time.sleep(0.2)
    print('✅ 完成 → /tmp/x.mp4')


def boom():
    print('starting')
    print('something went wrong', file=sys.stderr)
    sys.exit(3)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'emit'
    arg = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    {'emit': lambda: emit(arg), 'stop': lambda: stop(arg),
     'spawn': spawn, 'markers': markers, 'boom': boom}[mode]()
