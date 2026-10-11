#!/usr/bin/env python3
"""resolve_materials.py — 解析 remix.yaml 中的 搜索: 素材（二创管线）

用法:
  python resolve_materials.py <工作目录> --plan   # 只列出搜索场景和关键词，不下载
  python resolve_materials.py <工作目录>           # 下载素材并改写 remix.yaml（有确认）
  python resolve_materials.py <工作目录> -y        # 跳过确认直接下载
  python resolve_materials.py <工作目录> -y --no-search   # 页面关了自动搜索：不下载

流程:
  1. 读取 remix.yaml，找出 source 以 "搜索:" 开头的 image/clip 场景
  2. --plan：打印关键词清单供确认（可先直接改 remix.yaml 里的关键词）
  3. 下载：image → Pexels 静态照片；clip → Pexels 视频空镜（优先时长足够的）
  4. 素材复制到 <工作目录>/图片 或 /短片（缓存文件不动；渲染前 preprocess 统一尺寸）
  5. 改写 remix.yaml 的 source 为本地路径；失败降级：
     image → 原视频关键帧 → text 卡片；clip → text 卡片
"""
import sys, os, re, shutil, yaml, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pexels_materials import (search_photos_with_cache, download_photo,
                              search_with_cache, download_video, probe_video_dur)


def load_remix(path):
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict) and 'remix' in data:
        data = data['remix']
    return data


def detect_orientation(data):
    """从原视频探测成片方向：竖屏项目（白岩松 720x1280）搜 portrait 素材；
    探测失败默认 landscape"""
    src = str(data.get('source', '') or '')
    if src and os.path.exists(src):
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=width,height',
                                '-of', 'csv=p=0', src],
                               capture_output=True, text=True, timeout=30)
            wh = r.stdout.strip().split(',')
            if len(wh) == 2:
                return 'portrait' if int(wh[1]) > int(wh[0]) else 'landscape'
        except Exception:
            pass
    return 'landscape'


def find_search_scenes(data):
    """返回 [(场景索引, 场景dict, 关键词, 素材类型)]"""
    out = []
    for i, sc in enumerate(data.get('scenes', [])):
        src = str(sc.get('source', '') or '')
        if src.startswith('搜索:') or src.startswith('搜索：'):
            term = src[3:].strip()
            kind = '照片' if sc.get('type') == 'image' else '视频'
            out.append((i, sc, term, kind))
    return out


def sanitize(term):
    """关键词 → 安全文件名段（中英文数字，截断 20 字）"""
    s = re.sub(r'[^\w一-鿿]+', '', term) or 'material'
    return s[:20]


def scene_need_dur(sc):
    """clip 场景预估所需时长：旁白朗读时长 + 0.4s（与渲染逻辑一致）"""
    narration = sc.get('narration') or ''
    if narration:
        return max(2.0, len(narration) / 4.5 + 0.4)
    return float(sc.get('duration', 3) or 3)


def keyframes(work_dir):
    d = os.path.join(work_dir, '_keyframes')
    if os.path.isdir(d):
        return sorted(f for f in os.listdir(d) if f.lower().endswith('.jpg'))
    return []


def to_text_card(sc):
    """降级：改造成 text 卡片（保留旁白/BGM，时长钳制 2.5s）"""
    return {
        'type': 'text',
        'duration': 2.5,
        'narration': sc.get('narration') or '本期视频到此结束',
        'bgm': sc.get('bgm'),
    }


def resolve_image(sc, idx, work_dir, term, orientation='landscape'):
    """image 场景：下载 Pexels 照片 → 图片/；失败降级关键帧"""
    for c in search_photos_with_cache(term, orientation):
        p = download_photo(c['url'])
        if p:
            dst = os.path.join(work_dir, '图片', f"img_{idx:02d}_{sanitize(term)}.jpg")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            print(f"  ✅ 照片 → {dst}")
            return f"图片/{os.path.basename(dst)}"
    # 降级：原视频关键帧（画面与原视频一致）
    kfs = keyframes(work_dir)
    if kfs:
        kf = kfs[idx % len(kfs)]
        print(f"  ⚠️ 照片下载失败 '{term}'，降级关键帧 {kf}")
        return f"_keyframes/{kf}"
    print(f"  ⚠️ 照片下载失败 '{term}' 且无关键帧，降级文字卡片")
    return None


def resolve_clip(sc, idx, work_dir, term, orientation='landscape', skip_hashes=None):
    """clip 场景：下载 Pexels 视频 → 短片/；优先时长足够的，否则最长"""
    skip_hashes = set(skip_hashes or ())
    need = scene_need_dur(sc)
    cands = search_with_cache(term, min_duration=5, orientation=orientation)
    for c in cands:
        if c['duration'] >= need:
            import hashlib
            h = hashlib.md5(c['url'].split('?')[0].encode('utf-8')).hexdigest()
            if h in skip_hashes:
                continue
            skip_hashes.add(h)
            pick = c
            break
    else:
        pick = max(cands, key=lambda c: c['duration']) if cands else None
    pick = next((c for c in cands if c['duration'] >= need), None)
    if pick is None and cands:
        pick = max(cands, key=lambda c: c['duration'])
    if pick:
        p = download_video(pick['url'])
        if p:
            dst = os.path.join(work_dir, '短片', f"clip_{idx:02d}_{sanitize(term)}.mp4")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            got = probe_video_dur(p)
            print(f"  ✅ 视频 → {dst}  ({got:.1f}s, 场景需 {need:.1f}s)")
            return f"短片/{os.path.basename(dst)}"
    print(f"  ⚠️ 视频下载失败 '{term}'，降级文字卡片")
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    work_dir = sys.argv[1]
    plan_only = '--plan' in sys.argv
    auto_yes = '-y' in sys.argv
    # 页面上的「自动搜索」开关（见 web/pipelines.py）
    allow_search = '--no-search' not in sys.argv

    remix_path = os.path.join(work_dir, 'remix.yaml')
    if not os.path.exists(remix_path):
        print(f"❌ 未找到: {remix_path}")
        sys.exit(1)

    data = load_remix(remix_path)
    scenes = find_search_scenes(data)

    if not scenes:
        print("ℹ️ remix.yaml 中没有 搜索: 素材，无需解析")
        return

    orientation = detect_orientation(data)
    print(f"📐 素材方向: {orientation}（由原视频 {data.get('source', '(无)')} 探测）")

    print(f"🔍 待搜索素材（{len(scenes)} 个）:")
    for i, sc, term, kind in scenes:
        stype = sc.get('type')
        dur = scene_need_dur(sc) if stype == 'clip' else sc.get('duration', 3)
        print(f"  [{i}] 场景#{i+1:02d}  {stype:5s}  {kind}  '搜索:{term}'"
              f"  (~{dur:.0f}s  {'旁白' if sc.get('narration') else '画面'})")

    if not allow_search:
        # 本文件的全部工作就是下载 —— 关了搜索它就该是个空操作。
        # 刻意**不**把场景改写成文字卡：那会把这版方案永久改掉，而用户可能只是
        # 这一次不想联网。降级交给 render_remix.py 自己处理（它认同一个标志）。
        print(f"\n🔌 自动搜索已关闭：上面 {len(scenes)} 个 搜索: 场景不解析、不下载，"
              f"remix.yaml 保持原样")
        print("   渲染时它们会退到本地素材，顶不上才降级文字卡片；"
              "想彻底避开，请关着搜索重新生成方案（plan_remix.py）")
        return

    if plan_only:
        print("\n👉 可先编辑 remix.yaml 修改关键词，然后执行:")
        print(f"   python resolve_materials.py {work_dir}")
        return

    if not auto_yes:
        try:
            r = input(f"\n下载并改写 remix.yaml？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            r = ''
        if r not in ('y', 'yes'):
            print("已取消")
            return

    total = len(scenes)
    for n, (i, sc, term, kind) in enumerate(scenes, 1):
        print(f"\n[{n}/{total}] 场景#{i+1:02d} 搜索 '{term}'")
        if sc.get('type') == 'image':
            local = resolve_image(sc, i, work_dir, term, orientation)
        else:
            local = resolve_clip(sc, i, work_dir, term, orientation)
        if local:
            sc['source'] = local
        else:
            data['scenes'][i] = to_text_card(sc)
            print(f"  → 场景#{i+1:02d} 已降级为文字卡片")

    out = {'remix': data}
    with open(remix_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)
    print(f"\n✅ remix.yaml 已改写 → {remix_path}")


if __name__ == '__main__':
    main()
