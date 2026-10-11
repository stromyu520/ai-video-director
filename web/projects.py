"""projects.py — 项目（= 一个工作目录）的枚举与创建

项目就是 `分类/项目名/` 这个目录本身，管线的一切输入输出都在里面。
本模块只读目录、不解析 yaml 内容（那是各步自己的事），
也绝不 import torch —— 服务器进程必须轻（本机 15G 内存已满）。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # 让下面能 import 仓库根目录的模块
    sys.path.insert(0, str(ROOT))

from .fsutil import (VIDEO_EXTS, IMAGE_EXTS, sanitize_name, safe_join,   # noqa: E402
                     probe_video)
import speech_rate                                                     # noqa: E402
# 用户素材目录（`素材/` + 存量 `图片/`）。从 pipeline_config 拿而不是从
# video_make_ffmpeg 拿：服务器进程不能 import 渲染模块，见该常量的 docstring。
from pipeline_config import MANUAL_DIRS                                # noqa: E402

# 新建项目时分类输入框里的候选值。**这不是白名单** —— 分类在页面上是
# 手输的，这里只决定下拉给哪几个建议；枚举以磁盘为准，见 discover_categories()。
# 顶层还有 docs/ models/ web/ 等十几个非项目目录，不能一律当分类列出来。
CATEGORIES = ('新闻', '军事', '报道', '宣传')

# 界面认得的产物/输入（相对项目目录）
ARTIFACTS = {
    'output.mp4': '成片',
    'final_remix.mp4': '二创成片',
    'director.yaml': '导演文件',
    'director_optimized.yaml': '优化后导演文件',
    'remix.yaml': '二创方案',
    'analysis.json': '分析报告',
    '新闻内容.txt': '原始文案',
}


# 判断「这个顶层目录像不像分类目录」用的项目标记物。任取其一即可。
# `素材` 必须在里面：新项目只传素材、还没出片时，它就是这个项目**唯一**的东西，
# 漏掉的话整个分类会从 discover_categories() 里消失（列表页上没有，也搜不到）。
_PROJECT_MARKS = frozenset(ARTIFACTS) | {'project.json', '素材', '图片', '短片', '音频'}


def _safe_segment(s):
    """单层目录名 —— 挡住 ../ 之类的路径穿越，也挡住 '.' / '..'。

    以前靠 `category not in CATEGORIES` 白名单兜底，现在改成磁盘发现，
    这层校验就必须显式写出来，否则 `ROOT / '../../etc'` 能通过 is_dir()。

    '.' 这一条是给 remove() 用的：`safe_join(ROOT/'新闻', '.')` 会解析成
    分类目录**本身**，is_dir() 为真 —— 不拦的话「删项目」能把整个分类端掉。
    """
    if not isinstance(s, str) or not s or s in ('.', '..'):
        return False
    if s.startswith('.') or '/' in s or '\\' in s:
        return False
    return os.sep not in s


def _looks_like_category(d):
    try:
        subs = [s for s in d.iterdir() if s.is_dir() and not s.name.startswith('.')]
    except OSError:
        return False
    return any(any((s / m).exists() for m in _PROJECT_MARKS) for s in subs)


def discover_categories():
    """默认分类 + 仓库里已经存在的分类目录。

    为什么要从磁盘发现：`CATEGORIES` 只是新建时的下拉选项，改一次默认值
    （比如从「新闻/其他/二创」换成「新闻/军事/报道/宣传」），原来建在
    `其他/` `二创/` 下的项目就会**整批从列表里消失** —— 数据没丢，
    但页面上看不见，和丢了没区别。

    判据是「该目录下至少有一个子目录长得像项目」，实测能把 docs/ models/
    web/ video-env/ 这些排除干净。
    """
    out = list(CATEGORIES)
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith('.') or d.name in out:
            continue
        if _looks_like_category(d):
            out.append(d.name)
    return tuple(out)


def _ok_as_category(d):
    """这个目录能不能当分类目录用。

    判据是「**空的**」或「已经像个分类目录」：
      · 空 —— 分类下最后一个项目被删掉后会留下空壳。它必须仍可读可写，
        否则删完最后一个项目，再打开那个分类的列表就变成 400 ——
        一个本该是「没东西」的事，报成了「你请求错了」。这条是实测踩出来的。
      · 像 —— 里面已经有项目，正常分类目录。
    被排除的是 docs/ models/ web/ video-env/ 这类**已经有别的 contents** 的
    非项目目录：往里建项目会把仓库结构搞乱，也会把它们的文件列成项目文件。
    """
    if not (d.is_dir() and any(d.iterdir())):
        return True                      # 空目录（或内容恰好为空）放行
    return _looks_like_category(d)


def _check_category_dir(category, *, allow_new=False):
    """校验并返回分类目录。

    `allow_new=True` 给新建项目用：分类在页面上是手输的，得允许磁盘上还没有
    的名字。读/删一律不许 —— 那样 `docs/x` 之类的路径就能被当成项目读出来。
    """
    if not _safe_segment(category):
        raise ValueError(f'分类名不合法: {category!r}')
    d = ROOT / category
    if d.exists() and not d.is_dir():
        raise ValueError(f'「{category}」是个文件，不能当分类名')
    if category in CATEGORIES:
        return d                         # 内置分类无条件放行（哪怕是空的）
    if not d.is_dir():
        if allow_new:
            return d                     # 新分类，create() 会把它 mkdir 出来
        raise ValueError(
            f'未知分类: {category}（可选 {"/".join(discover_categories())}）')
    if not _ok_as_category(d):
        raise ValueError(
            f'「{category}」不是分类目录（里面已经有别的东西了），换个名字')
    return d


def _category_dir(category):
    """读/写既有项目时的分类目录校验。"""
    return _check_category_dir(category)


def _category_dir_for_new(category):
    """新建项目时的分类目录 —— 比读路径多允许一种情况：**还不存在的名字**。"""
    return _check_category_dir(category, allow_new=True)


def _find_source_video(pdir):
    """项目里的原片：优先叫「原片」/「source」的，否则取最大的那个视频。

    必须排除产物名 —— 成片往往就是目录里最大的视频，不排除的话
    每个项目的「原片」都会显示成自己的 output.mp4。
    """
    skip = set(ARTIFACTS)
    vids = [p for p in pdir.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS
            and not p.name.startswith('.') and p.name not in skip]
    if not vids:
        return None
    named = [p for p in vids
             if any(k in p.stem for k in ('原片', '原视频', 'source', '素材'))]
    pool = named or vids
    return max(pool, key=lambda p: p.stat().st_size)


def _image_count(pdir):
    """列表卡片上那个「图片 N」。两个素材目录都数 —— 只数 `图片/` 的话，
    页面把上传的图都收进 `素材/` 之后，卡片会一直显示 0。"""
    return sum(1 for m in list_materials(pdir) if m['kind'] == 'image')


def summarize(category, name):
    """一个项目的概要。目录名本身就是项目名（可能是中文长句）。"""
    pdir = safe_join(_category_dir(category), name)
    return _summarize_dir(pdir, category, name)


def _summarize_dir(pdir, category, name):
    """概要的本体。create() 直接调它 —— 走 summarize 的话会再过一次
    `_category_dir` 的严格校验，而刚建的新分类那一刻还不满足「像个分类目录」，
    于是项目建成功了却报 400。"""
    if not pdir.is_dir():
        return None
    files = []
    for fn, label in ARTIFACTS.items():
        fp = pdir / fn
        if fp.is_file():
            st = fp.stat()
            files.append({
                'name': fn, 'label': label,
                'size': st.st_size,
                'mtime': st.st_mtime,
                'age': round(time.time() - st.st_mtime),
            })
    src = _find_source_video(pdir)
    out = next((f for f in files
                if f['name'] in ('output.mp4', 'final_remix.mp4')), None)
    return {
        'category': category,
        'name': name,
        'path': str(pdir),
        'artifacts': files,
        'images': _image_count(pdir),
        'source': src.name if src else None,
        'source_size': src.stat().st_size if src else 0,
        'output': out['name'] if out else None,
        'output_size': out['size'] if out else 0,
        'mtime': max((f['mtime'] for f in files), default=pdir.stat().st_mtime),
        # 建项目时选的类型，工作台据此展开对应的那一段（进去还能改）
        'kind': read_kind(pdir),
        # 「自动搜索」开关（默认开）。跟着概要一起回去，进项目就不用再单发一个请求。
        'search': read_search(pdir),
        # 前端据此决定默认走哪条管线：有二创分析的走 remix，否则 director
        'suggest': 'remix' if (pdir / 'analysis.json').exists() else 'director',
    }


def _output_video(pdir):
    for n in ('output.mp4', 'final_remix.mp4'):
        p = pdir / n
        if p.is_file():
            return p
    return None


def _declared_duration(pdir):
    """director.yaml 里声明的目标片长。remix.yaml 没有这个字段 —— 返回 None。"""
    fp = pdir / 'director.yaml'
    if not fp.is_file():
        return None
    try:
        import yaml
        doc = yaml.safe_load(fp.read_text(encoding='utf-8'))
    except Exception:                                        # noqa: BLE001
        return None
    if not isinstance(doc, dict):
        return None
    try:
        d = float((doc.get('project') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def duration_report(pdir):
    """声明片长 vs 实际片长 —— 页面上那条标红提示的数据来源。

    片长是旁白念多久决定的，`project.duration` 只是一个**意图**。两者对不上
    时用户看到的是「我填了 30 秒，怎么出来 43 秒」。这条差值以前只打进了
    渲染日志，而日志是滚动的 —— 片子都出来了，没人会回头翻。

    只在打开工作台时算一次，**不放进 summarize()**：列表页有十几个项目，
    一个个 ffprobe 过去会让首页明显变慢。
    """
    want = _declared_duration(pdir)
    out = _output_video(pdir)
    if want is None or out is None:
        return None
    actual = probe_video(out).get('duration') or 0
    if actual <= 0:
        return None
    off = (actual - want) / want
    return {
        'file': out.name,
        'want': round(want, 1),
        'actual': round(actual, 1),
        'off': round(off, 3),
        # 阈值与 director_agent.RATE_TOL 同一口径：校验器按 ±15% 要求 LLM 写稿，
        # 成片也按 ±15% 判「这次算不算按意图出片」。
        'off_budget': abs(off) > 0.15,
        'longer': off > 0,
    }


def list_files(pdir, limit=400):
    """项目目录的顶层条目。子目录（图片/短片/_keyframes）只报文件名与数量，
    不展开 —— 一个 30s 的片子能有几百个关键帧，全展开会把工作台卡死。"""
    out = []
    for entry in sorted(pdir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith('.'):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if entry.is_dir():
            n = sum(1 for f in entry.iterdir() if not f.name.startswith('.'))
            out.append({'name': entry.name, 'dir': True, 'count': n,
                        'size': 0, 'mtime': st.st_mtime})
        else:
            out.append({'name': entry.name, 'dir': False,
                        'ext': entry.suffix.lower().lstrip('.'),
                        'size': st.st_size, 'mtime': st.st_mtime})
    out.sort(key=lambda f: f['mtime'], reverse=True)
    return out[:limit]


def list_materials(pdir, limit=300):
    """素材目录里的文件，供工作台素材面板逐张展示。

    与 list_files 的分工：那个只报「图片/ · 12」这样的目录计数，面板要的是
    逐张缩略图，必须展开一层。展开一层是安全的 —— 关键帧在 `_keyframes/`，
    不在 MANUAL_DIRS 里，不会一次涌进几百条。

    `rel` 是给前端拼 `/media/...` 用的，也是 director.yaml 里 source 的写法，
    两边必须同形（`素材/前脸.jpg`），否则「这张图有没有被用上」就配不上对。
    """
    out = []
    for sub in MANUAL_DIRS:
        d = pdir / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir(), key=lambda p: p.name):
            if not f.is_file() or f.name.startswith('.'):
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS:
                kind = 'image'
            elif ext in VIDEO_EXTS:
                kind = 'video'
            else:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({'rel': f'{sub}/{f.name}', 'name': f.name, 'dir': sub,
                        'kind': kind, 'ext': ext.lstrip('.'),
                        'size': st.st_size, 'mtime': st.st_mtime})
    return out[:limit]


def count_materials(pdir):
    """可当画面的本地素材件数（素材目录 + 根目录图片），只判「有没有」。

    用来在**提交前**拦下「关了自动搜索、项目里又一个素材都没有」—— 让智能体
    跑到一半再报错，用户白等一轮。口径与 director_agent.local_material_count
    对齐（那边是真正干活的）：根目录的图片也算，别一边算一边不算，
    否则会出现「页面上答应了、智能体又拒绝」这种自相矛盾。
    """
    n = len(list_materials(pdir))
    try:
        n += sum(1 for f in pdir.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    except OSError:
        pass
    return n


def _scenes_director(doc):
    voice = str((doc.get('voice') or {}).get('spk_id') or '')
    items = []
    for i, sc in enumerate(doc.get('scenes') or []):
        if not isinstance(sc, dict):
            continue
        vis = sc.get('visuals') or {}
        narration = str(sc.get('narration') or '')
        items.append({
            'index': i,
            'kind': str(vis.get('type') or ''),
            'source': str(vis.get('source') or ''),
            'narration': narration,
            'spoken': str(sc.get('spoken') or ''),
            'mood': str(sc.get('mood') or ''),
            'speed': str(sc.get('speed') or ''),
            # director.yaml 不写时长，时长是渲染时按字数念出来的。
            # 换算率走 speech_rate（按音色实测），别再抄一个 4.5 进来 ——
            # 这个数以前有三份拷贝，每份都不一样。
            'secs': speech_rate.dur_for(voice, len(narration)),
        })
    return items


def _scenes_remix(doc):
    root = doc.get('remix') if isinstance(doc.get('remix'), dict) else doc
    items = []
    for i, sc in enumerate((root or {}).get('scenes') or []):
        if not isinstance(sc, dict):
            continue
        typ = str(sc.get('type') or '')
        if typ == 'original':
            secs = max(0.0, float(sc.get('end') or 0) - float(sc.get('start') or 0))
        else:
            secs = float(sc.get('duration') or 0)
        items.append({
            'index': i,
            'kind': typ,
            'source': str(sc.get('source') or ''),
            'narration': str(sc.get('narration') or ''),   # original 段是 None
            'mood': str(sc.get('bgm') or ''),
            'speed': '',
            'audio': str(sc.get('audio') or ''),
            'secs': round(secs, 1),
        })
    return items


def _src_exists(pdir, src):
    """场景引用的本地素材还在不在？`搜索:` 与空 source 返回 None（不适用）。

    面板要的就是这一个布尔量：渲染时引用了不存在的文件**不会报错**，
    只会静默降级成文字卡片，片子照常出、体积照常小 —— 不看这一列发现不了。
    这正是 director_agent.LAYOUT_RULES 里那个真实事故（source: 图片/主视觉.jpg）。
    """
    s = (src or '').strip()
    if not s or s.startswith('搜索:') or s.startswith('搜索：'):
        return None
    try:
        return (pdir / s).is_file() if not os.path.isabs(s) else os.path.isfile(s)
    except OSError:
        return False


def parse_scenes(pdir):
    """把两条管线的场景统一成前端只认的一种形状。

    形状不同：director 的画面在 `scenes[].visuals.source`，remix 在
    `scenes[].source`；remix 还有一类 `type: original`（保留原声、没有旁白）。
    解析失败一律返回 None —— yaml 是 LLM 写的，可能是半截的，
    不该让整个工作台打不开。
    """
    for fname, kind, fn in (('director.yaml', 'director', _scenes_director),
                            ('remix.yaml', 'remix', _scenes_remix)):
        fp = pdir / fname
        if not fp.is_file():
            continue
        try:
            import yaml
            doc = yaml.safe_load(fp.read_text(encoding='utf-8'))
        except Exception:                                    # noqa: BLE001
            return {'kind': kind, 'file': fname, 'items': [], 'error': '解析失败'}
        if not isinstance(doc, dict):
            return {'kind': kind, 'file': fname, 'items': [], 'error': '不是映射'}
        items = fn(doc)
        # exists 统一在这里补，不塞进两个 _scenes_* —— 那两处只管形状差异，
        # 「文件在不在」是判断，不是形状，混进去以后加一种管线就要抄一遍。
        for it in items:
            it['exists'] = _src_exists(pdir, it.get('source'))
        return {'kind': kind, 'file': fname, 'items': items}
    return None


def list_projects(category=None):
    cats = [category] if category else list(discover_categories())
    out = []
    for c in cats:
        d = _category_dir(c)
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_dir() or entry.name.startswith('.'):
                continue
            s = summarize(c, entry.name)
            if s:
                out.append(s)
    out.sort(key=lambda s: s['mtime'], reverse=True)
    return out


# 页面上的「视频类型」→ 管线 + 模式。三种类型只对应两条管线：
# 解说与二创是同一套编排（plan_remix.py），差别只有 --mode。
KINDS = {
    'director':    {'pipeline': 'director', 'mode': None,
                    'label': '新视频生成', 'hint': '标题/正文 → 智能编排 → 出片'},
    'commentary':  {'pipeline': 'remix', 'mode': 'smart',
                    'label': '视频解说', 'hint': '上传原片，保留部分原声，只补旁白'},
    'remix':       {'pipeline': 'remix', 'mode': 'full',
                    'label': '二次创作', 'hint': '上传原片，全程重配旁白与画面'},
}
KIND_FILE = 'project.json'


def _load_project_json(pdir):
    """读 project.json。缺文件、坏 JSON、不是对象 —— 一律当空 dict。

    这里存的都是「页面上的开关」这类可选偏好，不是权威数据：读不出来就各用
    各的默认值（kind 去猜目录内容、search 默认开），不该让页面打不开。
    """
    fp = pdir / KIND_FILE
    if not fp.is_file():
        return {}
    try:
        import json
        data = json.loads(fp.read_text(encoding='utf-8')) or {}
        return data if isinstance(data, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _save_project_json(pdir, data):
    import json
    (pdir / KIND_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def read_kind(pdir):
    """项目建时选的类型。读不出来就按目录内容猜 —— 老项目没有这个文件。"""
    k = _load_project_json(pdir).get('kind')
    if k in KINDS:
        return k
    if (pdir / 'director.yaml').is_file():
        return 'director'
    if (pdir / 'analysis.json').is_file():
        return 'remix'
    return 'director'


def write_kind(pdir, kind):
    if kind not in KINDS:
        raise ValueError(f'未知视频类型: {kind}（可选 {"/".join(KINDS)}）')
    import time as _t
    data = _load_project_json(pdir)
    data['kind'] = kind
    data.setdefault('created_at', _t.time())
    return _save_project_json(pdir, data)


def read_search(pdir):
    """页面上的「自动搜索」开关（关了＝只排本地素材，不联网）。

    默认开。老项目没有这个键，缺省必须等于今天的行为 —— 否则升级一次
    就悄悄改掉了所有人的出片方式，而且只在「素材不够」时才看得出来。
    """
    v = _load_project_json(pdir).get('search')
    return v if isinstance(v, bool) else True


def write_search(pdir, on):
    """写回开关。只动 search 一个键，kind/created_at 原样留着。"""
    data = _load_project_json(pdir)
    data['search'] = bool(on)
    return _save_project_json(pdir, data)


def create(category, name, kind=None):
    """新建项目目录。分类可以是磁盘上还没有的新名字（页面上手输的），
    已存在则直接返回（幂等）。"""
    clean = sanitize_name(name)
    if not clean:
        raise ValueError('项目名不合法')
    cdir = _category_dir_for_new(category)
    pdir = safe_join(cdir, clean)
    pdir.mkdir(parents=True, exist_ok=True)
    if kind:
        write_kind(pdir, kind)
    return _summarize_dir(pdir, category, clean)


def remove(category, name):
    """删项目：整个 `分类/项目名/` 目录，不可撤销。

    名字必须先过 _safe_segment —— 见它的注释，'.' 会把 pdir 解析成
    分类目录本身，那样删的就不是项目而是整个分类。
    """
    import shutil
    if not _safe_segment(name):
        raise ValueError(f'项目名不合法: {name!r}')
    pdir = safe_join(_category_dir(category), name)
    if not pdir.is_dir():
        raise FileNotFoundError(name)
    shutil.rmtree(pdir)
    return True


def project_dir(category, name):
    """校验并返回项目目录（所有路由都该用这个，别自己拼路径）。"""
    pdir = safe_join(_category_dir(category), name)
    if not pdir.is_dir():
        raise FileNotFoundError(f'项目不存在: {category}/{name}')
    return pdir


def iter_projects_dirs():
    """产出 (category, name, Path)，供任务恢复时定位。"""
    for c in discover_categories():
        d = _category_dir(c)
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir()):
            if entry.is_dir() and not entry.name.startswith('.'):
                yield c, entry.name, entry
