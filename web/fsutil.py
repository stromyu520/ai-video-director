"""fsutil.py — 路径与压缩包安全工具

只放无副作用的纯函数/工具，不 import 任何重资源（torch 等）。
唯一例外是 probe_video()：它要起一个短命 ffprobe 子进程，
但同样不 import 任何重模块，放在这里是为了只有**一份**实现。
"""
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

# 允许的字符集之外的字符（保留中文、字母数字、空格与常见文件名符号）
_NAME_BAD = re.compile(r'[^0-9A-Za-z一-鿿 ._\-()（）]')

VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.webm', '.avi')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')


def safe_join(root, *parts):
    """在 root 下解析 parts；若逃逸出 root 则抛 ValueError。

    root 与 parts 可以是 str 或 Path。返回值是 resolve 后的 Path。
    """
    root = Path(root).resolve()
    p = root.joinpath(*[str(x) for x in parts]).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f'路径逃逸: {parts}')
    return p


def sanitize_name(name, fallback='未命名'):
    """清洗用户提供的名称，返回可安全用作目录名的字符串。"""
    s = _NAME_BAD.sub('', str(name)).strip(' .-')
    if not s or s in ('.', '..') or s.startswith('.'):
        s = fallback
    return s[:80]


def ext_ok(filename, exts):
    return str(filename).lower().endswith(tuple(exts))


def safe_extract(zip_path, dest, *, max_total_bytes=6 << 30,
                 max_entries=20_000, max_ratio=200.0):
    """安全解压：拒绝路径穿越、符号链接与解压炸弹。

    先按条目头部声明的大小/数量快速拒绝，再在流式复制时按实际写入字节二次校验
    （头部大小可以撒谎）。
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest_r = dest.resolve()

    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > max_entries:
            raise ValueError(f'压缩包条目过多: {len(infos)} > {max_entries}')
        declared = sum(i.file_size for i in infos)
        if declared > max_total_bytes:
            raise ValueError(
                f'解压后体积超限: {declared / 2**30:.1f}GB > '
                f'{max_total_bytes / 2**30:.1f}GB')

        written = 0
        for info in infos:
            # 符号链接：Python 的 extractall 会真的创建它，必须跳过
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            name = info.filename
            rel = PurePosixPath(name)
            if rel.is_absolute() or '\\' in name or '..' in rel.parts:
                raise ValueError(f'非法路径: {name}')
            if info.file_size > 0 and info.compress_size > 0:
                if info.file_size / info.compress_size > max_ratio:
                    raise ValueError(f'压缩比异常（疑似解压炸弹）: {name}')

            target = (dest_r / rel).resolve()
            if target != dest_r and dest_r not in target.parents:
                raise ValueError(f'路径穿越: {name}')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, 'wb') as out:
                while chunk := src.read(1 << 20):
                    written += len(chunk)
                    if written > max_total_bytes:
                        raise ValueError('解压体积超限（实际字节数）')
                    out.write(chunk)
    return dest


def probe_video(path):
    """ffprobe 出宽高与时长。探测失败回空 dict，绝不抛。

    判据缺失不该让整个页面打不开 —— 调用方按「0 就是不知道」处理。
    结果不缓存：只在打开工作台时问一次，本地文件几十毫秒，不值得为它
    维护一套失效逻辑（缓存键得同时看 mtime 和大小，很容易写错）。
    """
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,duration',
             '-show_entries', 'format=duration',
             '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or '{}')
    except Exception:                                        # noqa: BLE001
        return {}
    st = (data.get('streams') or [{}])[0]
    dur = st.get('duration') or (data.get('format') or {}).get('duration') or 0
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = 0.0
    return {'width': int(st.get('width') or 0),
            'height': int(st.get('height') or 0),
            'duration': round(dur, 2)}


def dir_size(path):
    """目录占用字节数（不跟随符号链接）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total
