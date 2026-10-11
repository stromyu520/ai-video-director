#!/usr/bin/env python3
"""pexels_materials.py — Pexels 视频素材搜索/缓存/下载

用法:
  python pexels_materials.py "日出" --need-dur 20
  python pexels_materials.py "城市夜景" --need-dur 30 --list
  python pexels_materials.py "幼儿园" --photo         # 照片模式

逻辑移植自 MoneyPrinterTurbo app/services/material.py：
  - 搜索：Authorization 裸 key + 浏览器 UA、orientation 按项目比例（竖屏项目
    传 portrait，见 render_remix/resolve_materials 调用），优先精确 1920x1080
    档（竖屏 1080x1920）
  - 下载：URL 去 query 后 md5 命名 vid-<md5>.mp4，存在且 size>0 复用
  - 缓存：24h TTL、SHA-256 键（含 orientation）、原子写、空结果不缓存
API key 读取链：PEXELS_API_KEY 环境变量 → config.toml [pexels] api_key → MPT config.toml
"""

import os, sys, json, time, hashlib, tempfile, subprocess, argparse
import requests

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import project_paths as P
CACHE_DIR = os.path.join(BASE_DIR, '素材缓存')
PEXELS_DIR  = os.path.join(CACHE_DIR, 'pexels')        # 视频文件
SEARCH_DIR  = os.path.join(CACHE_DIR, 'search_cache')  # 视频搜索结果 JSON
PHOTOS_DIR  = os.path.join(CACHE_DIR, 'pexels_photos') # 照片文件
SEARCH_PHOTO_DIR = os.path.join(CACHE_DIR, 'search_cache_photo')  # 照片搜索结果 JSON
SEARCH_CACHE_TTL = 24 * 60 * 60

LOCAL_CONFIG = os.path.join(BASE_DIR, 'config.toml')
MPT_CONFIG   = P.MPT_CONFIG
PEXELS_API   = 'https://api.pexels.com/v1/videos/search'
PEXELS_PHOTO_API = 'https://api.pexels.com/v1/search'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36')

TARGET_W, TARGET_H = 1920, 1080


# ============================================================
# API key
# ============================================================
def get_pexels_api_key():
    """key 读取链：环境变量 → 本地 config.toml → MPT config.toml"""
    k = os.environ.get('PEXELS_API_KEY', '').strip()
    if k:
        return k
    try:
        import tomllib
        with open(LOCAL_CONFIG, 'rb') as f:
            v = (tomllib.load(f).get('pexels') or {}).get('api_key', '')
        if str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    try:
        import tomllib
        with open(MPT_CONFIG, 'rb') as f:
            keys = (tomllib.load(f).get('app') or {}).get('pexels_api_keys', [])
        if isinstance(keys, list) and keys and str(keys[0]).strip():
            return str(keys[0]).strip()
    except Exception:
        pass
    return None


# ============================================================
# 搜索
# ============================================================
def _pick_rendition(video_files, target_w=TARGET_W, target_h=TARGET_H):
    """从 Pexels 多档清晰度中选档位：
    横屏：优先精确 1920x1080，次选 16:9 且宽≥1280 的最大档
    竖屏：优先精确 1080x1920，次选 9:16 且高≥1280 的最大档"""
    portrait = target_h > target_w
    best = None
    for f in video_files:
        w, h = f.get('width'), f.get('height')
        if not w or not h:
            continue
        if w == target_w and h == target_h:
            return f
        if portrait:
            if abs(w / h - 9 / 16) < 0.03 and h >= 1280:
                if best is None or h > best['height']:
                    best = f
        else:
            if abs(w / h - 16 / 9) < 0.03 and w >= 1280:
                if best is None or w > best['width']:
                    best = f
    return best


def search_videos_pexels(term, min_duration=5, orientation='landscape'):
    """搜索 Pexels 视频 → [{url, width, height, duration}]

    orientation: 'landscape'|'portrait'，与成片比例一致（竖屏项目搜 portrait，
    避免横屏素材裁切损失大）
    """
    key = get_pexels_api_key()
    if not key:
        print("  ⚠️ 未配置 Pexels API key（env PEXELS_API_KEY / config.toml / MPT config.toml）")
        return []
    try:
        r = requests.get(
            PEXELS_API,
            params={'query': term, 'per_page': 20, 'orientation': orientation},
            headers={'Authorization': key, 'User-Agent': UA},
            timeout=(30, 60))
    except Exception as e:
        print(f"  ⚠️ Pexels 搜索失败: {e}")
        return []
    if r.status_code == 429:
        print(f"  ⚠️ Pexels 限流 429（免费额度 200 req/h），稍后重试")
        return []
    if r.status_code != 200:
        print(f"  ⚠️ Pexels 搜索失败 HTTP {r.status_code}")
        return []
    try:
        data = r.json()
    except Exception:
        print("  ⚠️ Pexels 响应解析失败")
        return []

    results = []
    for v in data.get('videos', []):
        try:
            dur = int(float(v.get('duration', 0)))
        except (TypeError, ValueError):
            continue
        if dur < min_duration:
            continue
        tw, th = (1080, 1920) if orientation == 'portrait' else (TARGET_W, TARGET_H)
        pick = _pick_rendition(v.get('video_files', []), tw, th)
        if pick:
            results.append({
                'url': pick['link'],
                'width': pick.get('width'),
                'height': pick.get('height'),
                'duration': dur,
            })
    if not results:
        print(f"  ℹ️ 关键词 '{term}' 无可用素材")
    return results


# ============================================================
# 搜索结果 24h 缓存（空结果不缓存）
# ============================================================
def _cache_key(term, min_duration, orientation):
    payload = json.dumps({'term': term, 'min_duration': min_duration,
                          'orientation': orientation},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def search_with_cache(term, min_duration=5, orientation='landscape'):
    """搜索 + 24h 磁盘缓存；空结果/失败不缓存（避免限流错误被冻住一天）"""
    os.makedirs(SEARCH_DIR, exist_ok=True)
    path = os.path.join(SEARCH_DIR, _cache_key(term, min_duration, orientation) + '.json')

    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if 0 <= age < SEARCH_CACHE_TTL:
            try:
                with open(path, encoding='utf-8') as f:
                    items = json.load(f)
                if isinstance(items, list) and items:
                    print(f"  ♻️ 搜索缓存命中: '{term}' ({len(items)} 条)")
                    return items
            except Exception:
                pass
        else:
            # 过期或系统时间回拨（负龄）→ 删除
            try:
                os.remove(path)
            except OSError:
                pass

    items = search_videos_pexels(term, min_duration, orientation)
    if items:
        try:
            fd, tmp = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=SEARCH_DIR)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            print(f"  ⚠️ 搜索缓存写入失败: {e}")
    return items


# ============================================================
# 下载（URL md5 去重 + ffprobe 校验）
# ============================================================
def probe_video_dur(path):
    """容器时长（秒）"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _probe_ok(path):
    """校验视频有效：时长 > 0 且帧率可解析"""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=duration,r_frame_rate,width,height',
                        '-of', 'json', path], capture_output=True, text=True)
    try:
        j = json.loads(r.stdout)
        s = (j.get('streams') or [{}])[0]
        dur = float(s.get('duration', 0) or 0)
        fps = str(s.get('r_frame_rate', '0/0'))
        num, _, den = fps.partition('/')
        fv = float(num) / float(den) if float(den or 1) else 0.0
        return dur > 0 and fv > 0
    except Exception:
        return False


def download_video(url):
    """下载单个视频到全局缓存，返回路径；失败返回 None"""
    os.makedirs(PEXELS_DIR, exist_ok=True)
    url_hash = hashlib.md5(url.split('?')[0].encode('utf-8')).hexdigest()
    path = os.path.join(PEXELS_DIR, f'vid-{url_hash}.mp4')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=(60, 240))
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ 下载失败: {str(e)[:120]}")
        return None
    with open(path, 'wb') as f:
        f.write(r.content)
    if not _probe_ok(path):
        try:
            os.remove(path)
        except OSError:
            pass
        print(f"  ⚠️ 下载文件校验失败（非视频/损坏），已删除")
        return None
    return path


# ============================================================
# 照片（Pexels /v1/search）— 供二创 image 场景使用，缓存与视频隔离
# ============================================================
def search_photos_pexels(term, orientation='landscape'):
    """搜索 Pexels 照片 → [{url, width, height}]，比例匹配 orientation"""
    key = get_pexels_api_key()
    if not key:
        print("  ⚠️ 未配置 Pexels API key（env PEXELS_API_KEY / config.toml / MPT config.toml）")
        return []
    try:
        r = requests.get(
            PEXELS_PHOTO_API,
            params={'query': term, 'per_page': 20, 'orientation': orientation},
            headers={'Authorization': key, 'User-Agent': UA},
            timeout=(30, 60))
    except Exception as e:
        print(f"  ⚠️ Pexels 照片搜索失败: {e}")
        return []
    if r.status_code == 429:
        print(f"  ⚠️ Pexels 限流 429（免费额度 200 req/h），稍后重试")
        return []
    if r.status_code != 200:
        print(f"  ⚠️ Pexels 照片搜索失败 HTTP {r.status_code}")
        return []
    try:
        data = r.json()
    except Exception:
        print("  ⚠️ Pexels 照片响应解析失败")
        return []

    results = []
    portrait = orientation == 'portrait'
    for p in data.get('photos', []):
        w = p.get('width') or 0
        h = p.get('height') or 0
        if not w or not h:
            continue
        if portrait:
            if h < 1600:                    # 太小放大后发糊
                continue
            if abs(w / h - 9 / 16) > 0.35:  # 太方的图铺满时裁剪损失大，跳过
                continue
        else:
            if w < 1600:
                continue
            if abs(w / h - 16 / 9) > 0.35:
                continue
        src = (p.get('src') or {})
        url = src.get('large2k') or src.get('large') or src.get('original')
        if not url:
            continue
        results.append({'url': url, 'width': w, 'height': h})
    if not results:
        print(f"  ℹ️ 关键词 '{term}' 无可用照片")
    return results


def search_photos_with_cache(term, orientation='landscape'):
    """照片搜索 + 24h 磁盘缓存（目录与视频缓存隔离，键 = term+orientation 哈希）"""
    os.makedirs(SEARCH_PHOTO_DIR, exist_ok=True)
    path = os.path.join(SEARCH_PHOTO_DIR,
                        hashlib.sha256(f'{term}|{orientation}'.encode('utf-8')).hexdigest() + '.json')

    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if 0 <= age < SEARCH_CACHE_TTL:
            try:
                with open(path, encoding='utf-8') as f:
                    items = json.load(f)
                if isinstance(items, list) and items:
                    print(f"  ♻️ 照片缓存命中: '{term}' ({len(items)} 张)")
                    return items
            except Exception:
                pass
        else:
            try:
                os.remove(path)
            except OSError:
                pass

    items = search_photos_pexels(term, orientation)
    if items:
        try:
            fd, tmp = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=SEARCH_PHOTO_DIR)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            print(f"  ⚠️ 照片缓存写入失败: {e}")
    return items


def download_photo(url):
    """下载单张照片到全局缓存，PIL 校验；失败返回 None"""
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    url_hash = hashlib.md5(url.split('?')[0].encode('utf-8')).hexdigest()
    path = os.path.join(PHOTOS_DIR, f'img-{url_hash}.jpg')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=(60, 240))
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ 照片下载失败: {str(e)[:120]}")
        return None
    with open(path, 'wb') as f:
        f.write(r.content)
    try:
        from PIL import Image
        Image.open(path).verify()
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        print(f"  ⚠️ 下载文件校验失败（非图片/损坏），已删除")
        return None
    return path


def ensure_scene_photo(term, orientation='landscape'):
    """为场景准备一张照片：搜索 → 逐张尝试下载，返回第一个成功路径或 None"""
    if len(term.strip()) < 2:
        print(f"  ⚠️ 搜索词过短，跳过: '{term}'")
        return None
    for c in search_photos_with_cache(term, orientation):
        p = download_photo(c['url'])
        if p:
            print(f"  ✅ 照片就绪: '{term}' ({c['width']}x{c['height']})")
            return p
    print(f"  ⚠️ 照片不足: '{term}' 下载失败或候选耗尽")
    return None


def ensure_scene_videos(term, need_dur, max_files=8, orientation='landscape',
                        skip_hashes=None):
    """为场景准备视频素材：搜索候选逐一下载直到累计时长 ≥ need_dur

    skip_hashes: 已用于其他场景的 URL md5 集合（跨场景去重轮转，
    避免 Pexels 中文/热门词把同一热门视频顶到多个场景的 [0]）
    返回已下载素材路径列表（可能为空，调用方降级）
    """
    if len(term.strip()) < 2:
        print(f"  ⚠️ 搜索词过短，跳过: '{term}'")
        return []
    skip_hashes = set(skip_hashes or ())
    candidates = search_with_cache(term, min_duration=5, orientation=orientation)
    paths, got = [], 0.0
    for c in candidates:
        if len(paths) >= max_files:
            break
        h = hashlib.md5(c['url'].split('?')[0].encode('utf-8')).hexdigest()
        if h in skip_hashes:
            continue
        p = download_video(c['url'])
        if not p:
            continue
        paths.append(p)
        skip_hashes.add(h)
        got += probe_video_dur(p)
        if got >= need_dur:
            break
    if paths:
        print(f"  ✅ 素材就绪: '{term}' {len(paths)} 个，总时长 {got:.0f}s")
    else:
        print(f"  ⚠️ 素材不足: '{term}' 下载失败或候选耗尽，将降级文字卡片")
    return paths


# ============================================================
# CLI
# ============================================================
def main():
    ap = argparse.ArgumentParser(description='Pexels 素材下载（视频/照片）')
    ap.add_argument('term', help='搜索关键词')
    ap.add_argument('--need-dur', type=float, default=20, help='需要的总时长（秒，视频模式）')
    ap.add_argument('--list', action='store_true', help='只列出搜索结果不下载')
    ap.add_argument('--photo', action='store_true', help='照片模式（搜索静态照片）')
    ap.add_argument('--orientation', choices=['landscape', 'portrait'], default='landscape',
                    help='素材方向（默认 landscape，竖屏项目传 portrait）')
    args = ap.parse_args()

    if args.photo:
        if args.list:
            for i, it in enumerate(search_photos_with_cache(args.term, args.orientation)):
                print(f"  [{i}] {it['width']}x{it['height']}  {it['url'][:90]}")
            return
        p = ensure_scene_photo(args.term, args.orientation)
        if p:
            print(f"  📁 {p}")
            print(f"已下载 1 张照片 → {PHOTOS_DIR}")
        return

    if args.list:
        for i, it in enumerate(search_with_cache(args.term, orientation=args.orientation)):
            print(f"  [{i}] {it['duration']}s  {it['width']}x{it['height']}  {it['url'][:90]}")
        return

    paths = ensure_scene_videos(args.term, args.need_dur, orientation=args.orientation)
    for p in paths:
        print(f"  📁 {p}  ({probe_video_dur(p):.1f}s)")
    print(f"共 {len(paths)} 个文件 → {PEXELS_DIR}")


if __name__ == '__main__':
    main()
