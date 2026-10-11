#!/usr/bin/env python3
"""director_agent.py — 脚本智能体：标题 + 原文 → director.yaml

两种形态，同一份逻辑：
  CLI   : director_agent.py <项目目录> [--title T] [--text-file F]
                            [--dur 30] [--size 9:16] [--preset news|general]
                            [--no-search]   只用本地素材，不联网搜索
  import: generate(work_dir, title=..., ...) -> dict

页面（web/）把它当**脚本步骤**跑：子进程 + `-u` → 打印实时流式进 SSE，
进度靠下面的 emoji 标记被 web/progress.py 认出来。所以打印格式是契约，不是装饰。

**输入只有正文一处**：页面粘的原文（--text-file）或项目目录里已有的
正文内容*.txt。以前还支持给个网址去抓正文，已移除 —— 抓下来的正文混着
导航栏噪声，用户还得先判断「抓对了没」，不如自己粘一段干净的。

与 text_to_director.py 的关系：那个脚本要求项目目录里预先放好 正文内容*.txt，
且不校验、不重试，已无任何调用方。本文件原样搬来它的 SYSTEM_PROMPT
（news 预设逐字复用，避免两处提示词漂移）与「素材清单」扫描逻辑，加上两道：
输出校验、失败重试。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml                                                          # noqa: E402
import requests                                                      # noqa: E402
from pipeline_config import (llm as _llm, profile as _profile,        # noqa: E402
                             post_json)
from text_to_director import SYSTEM_PROMPT as NEWS_PROMPT             # noqa: E402
import image_brief                                                    # noqa: E402
import speech_rate                                                    # noqa: E402
# 扩展名与素材目录清单从渲染模块借 —— 这里以前各抄了一份，而且抄窄了
# （`.bmp`/`.tiff`/`.avi` 漏掉）：同一份清单在两处不一样时，表现是
# 「渲染认得、清单里没有」，也就是那张图永远排不进片子。
from video_make_ffmpeg import (IMG_EXTS as _IMG_SET,                  # noqa: E402
                               discover_manual_images,
                               discover_manual_videos)

# 那边是 set（用 `in` 判扩展名），str.endswith 只吃 tuple —— 转一次，
# 免得每个调用点都要记得这件事。
IMG_EXTS = tuple(_IMG_SET)

SIZES = {'9:16': [1080, 1920], '16:9': [1920, 1080]}

# 字数预算的换算率现在按**音色实测**（见 speech_rate.py），不再写死常数。
# 老代码用 4.5，且校验护栏 3.5~9.0（±2.5 倍）是在错误分母上标定的
# —— 那个区间实际什么都拦不住：声明 30s 出片 52.6s 也照样放行。
# 现在按实测率收窄到 ±RATE_TOL，写超了就重写，而不是默默出个超长片。
RATE_TOL = 0.15

SCENES_MIN, SCENES_MAX = 4, 10

# "搜索:train interior" / "搜索：城市夜景"（全角冒号也认）
_SEARCH_RE = re.compile(r'^\s*搜索\s*[:：]\s*(.+?)\s*$')
_HAS_DIGIT = re.compile(r'\d')


# ---------------------------------------------------------------------------
# 预设
#
# news 逐字复用 text_to_director 的提示词；general 是同一套结构规则的精简版。
# 两者共用「输出结构」三条硬规则（narration==spoken、英文搜索词、YAML 形状），
# 因为那是渲染器的硬契约，不是风格问题。
# ---------------------------------------------------------------------------

_STRUCTURE_RULES = """
## narration 与 spoken 必须逐字相同（最重要）
- **narration（字幕显示）和 spoken（TTS 朗读）必须是同一份文本，逐字完全相同**，禁止写两个版本
- 数字一律用阿拉伯数字书写（1.8T、7月16日、211匹、16.37万）——管线会自动把数字转成中文读音，字幕仍显示阿拉伯数字
- 文本保持口语播报风格 + 短句：每句 15~25 字，逗号/句号自然断句

## 画面（visuals）
- type: image — 用项目 素材/ 目录里的文件名（素材清单里有就照抄清单里的写法），或自动搜索照片 source: "搜索:关键词"
- type: video — source 写 "搜索:英文关键词" 自动搜索，或 "素材/文件名" 引用本地素材
- **搜索关键词必须是 2~4 个英文词**（Pexels 只支持英文，中文词会退回热门素材，
  等于素材全废）：如 "搜索:city night aerial"、"搜索:people subway phone"
- 禁止抽象词（news、hot、rules 这类匹配不到素材）

## 输出
只输出完整 YAML，不要任何解释文字：
project: {title: "...", duration: N, type: "..."}
voice: {spk_id: "...", express: true, speed: "中"}
optimize: false
scenes: [...]        # 每场景: id, narration, spoken, mood, speed, visuals(type, source)
bgm: {path: ""}
output: {subtitle: true, size: [1080, 1920]}   # 横屏 16:9 写 [1920, 1080]
"""

GENERAL_PROMPT = """你是短视频导演。给定一个主题或一段素材，设计一个完整的短视频方案。
""" + _STRUCTURE_RULES + """
## 场景设计
- 4~10 个场景，逻辑连贯：开头钩子 → 展开 → 转折/高光 → 收尾
- mood 可选: 悬念/严肃/平实/兴奋/高潮/收尾
- speed 可选: 快/中/慢
"""

PRESETS = {
    'news': ('新闻解说', NEWS_PROMPT),
    'general': ('通用口播', GENERAL_PROMPT),
}

DEFAULT_PRESET = 'news'


# ---------------------------------------------------------------------------
# 采集输入
# ---------------------------------------------------------------------------

def find_source_text(work_dir, explicit=''):
    """定位原文：--text-file 指定 > 目录下 正文内容*.txt > 目录下任意 .txt"""
    import glob
    if explicit:
        return explicit
    hits = glob.glob(os.path.join(work_dir, '正文内容*.txt')) + \
        glob.glob(os.path.join(work_dir, '*.txt'))
    return hits[0] if hits else None


def scan_materials(work_dir, briefs=None):
    """本地素材清单，提示 LLM 优先使用。

    原样搬自 text_to_director.py 的 main()，行文保持一致以便对照。

    `briefs` 是 image_brief 读懂图片后的结果。给与不给差别很大：
    不给的话清单**只有文件名**，模型只能从「前脸.jpg」这三个字猜里面是什么
    ——标致 508L 那版就是这么写出 source: 图片/主视觉.jpg 的，那个文件压根不存在。
    """
    materials = []
    # 读懂了的图片走带内容的清单；没读懂的（模型不可用/超时）退回只列文件名，
    # 至少别让它们从清单里凭空消失。
    done = {b['rel'] for b in (briefs or [])}
    materials += image_brief.inventory_lines(briefs or [])
    imgs = [r for r in discover_manual_images(work_dir) if r not in done]
    imgs += [f for f in sorted(os.listdir(work_dir))
             if f.lower().endswith(IMG_EXTS) and not f.startswith('.')]
    if imgs:
        materials.append('  本地图片（source **原样照抄**这些相对路径，别只写文件名）: '
                         + ', '.join(imgs))
    vids = [os.path.relpath(p, work_dir)
            for p in discover_manual_videos(work_dir)]
    if vids:
        materials.append(
            '  本地视频（可能是已生成成品的展示片段，video 场景 source 写 '
            '路径/文件名 显式引用并安排到合适的展示场景）: ' + ', '.join(vids))
    return materials


LAYOUT_RULES = """
## 素材排版（上面清单里每张图都已经"看过"了，按它的内容排）
- **source 必须从上面的清单里一字不差地选**，禁止自己拼文件名 ——
  凭空写一个不存在的图（`图片/主视觉.jpg` 这种）在渲染时只会静默降级成文字卡片。
- **讲什么就配什么图**：字幕/旁白说到哪一项，画面就该是印着那一项的那张图。
  这是本环节唯一的目标 —— 观众读到字幕的同时在画面上看到同样的字，才叫对上了。
- **图中文字是最可信的信息源**：图文卡上印的配置就是文案素材，
  照着它讲，别自己发挥、别写图里没有的参数。清单里写了「图中文字」的就优先用。
- **顺序按内容排成一条线**（例如 外观 → 智能 → 座舱 舒适），
  不要随机打乱、也不要按文件名字母序。每张图最多用一次。
- **用哪几张、舍哪几张由你定**：和片子主线无关、或内容与别的图重复的，
  直接不用 —— 硬凑一张不搭的图比少一张更伤观感。定完在解释里说一句舍了哪张、
  为什么（这句话给人看，不进 yaml）。
- 场景数可以多于图片数：多出来的场景用 `搜索:英文关键词` 补空镜。
  （**本次关了自动搜索时这条不适用**，见下面「只能用本地素材」一节。）
- **一个场景只配一张图**：`visuals` 是 `{type, source}` 两个键的对象，
  不是列表。想让某张多停一会儿，就把它单独占一个场景。
"""

# 关掉「自动搜索」时追加的规则（页面上的开关，见 web/pipelines.py）。
#
# 这一段跟 LAYOUT_RULES 里的「多出来的场景用 搜索: 补空镜」是**互相打架**的，
# 所以两条要么同进要么同出 —— 只在关了搜索时才追加本段，且本段明确说了
# 上一条不适用。校验器那边还有一道硬拦（任何 `搜索:` 都判错），
# 提示词写歪了也跑不出联网下载。
#
# 没有禁止 `type: text`：news 预设本来就要求用文字卡强调关键数字/金句
# （见 text_to_director.SYSTEM_PROMPT），一刀切禁掉会把那个设计一起砍没。
# 这里约束的是**别拿文字卡去顶缺素材的场次**。
NO_SEARCH_RULES = """
## 本次关闭了自动搜索：只能用本地素材
- **禁止出现 `搜索:`**。这一版不联网找素材，任何 `搜索:关键词` 都会被打回重写。
- visuals 的 source 只能从上面「项目可用素材」清单里**一字不差地照抄**；
  想让画面就是字幕本身，写 `type: text`。
- **素材不够就少写几个场景**：有几件素材就排几场（一件最多用一次）。
  宁可片子短、场次少，也不要用文字卡或不相干的画面去填时间
  —— 文字卡只留给真正需要大字强调的关键数字/金句，不用来顶缺素材的场次。
- 场次少了就把每场的旁白写长些，**全片旁白总量仍要落在上面给的预算区间里**
  （每句仍保持 15~25 字的短句节奏）。目标片长是用户在页面上选死的，不要改它。
"""


def build_prompt(title, article, materials, dur, total_chars=None, briefs=None,
                 allow_search=True):
    if total_chars is None:                    # 没传就按通用先验估一个
        total_chars = int(dur * speech_rate.DEFAULT_RATE)
    lines = [
        f'目标片长: {dur} 秒（全片总旁白约 {total_chars} 字，每场景旁白 15~30 字）',
        '',
        f'## 素材原文',
        f'标题: {title}',
        '',
        article,
        '',
        f'## 项目可用素材',
    ]
    if materials:
        lines += materials
    elif allow_search:
        lines += ['  （无本地素材，全部用 搜索:关键词 自动获取）']
    else:
        # generate() 里素材数为 0 就已经报错退出了，这里是兜底：真走到这，
        # 至少别给模型一句「全部用 搜索: 自动获取」的许可。
        lines += ['  （无本地素材）']
    # 只有在真读懂了一些图之后才加这套规则 —— 没读懂的图清单里只有文件名，
    # 这时要求模型「按内容排版」是强人所难，它只能继续猜。
    if any('error' not in b['r'] for b in (briefs or [])):
        lines.append(LAYOUT_RULES)
    if not allow_search:
        lines.append(NO_SEARCH_RULES)
    return '\n'.join(lines)


def local_material_count(work_dir):
    """本地素材**件数**（图片 + 视频），只用来判「有没有」，不评质量。

    口径必须和 scan_materials 交给 LLM 的那份清单一致 —— 不一致就会出现
    「清单里明明列着素材、却因为计数为 0 而拒绝生成」这种自相矛盾的失败。
    """
    n = len(discover_manual_images(work_dir))
    try:
        n += sum(1 for f in os.listdir(work_dir)
                 if not f.startswith('.') and f.lower().endswith(IMG_EXTS))
    except OSError:
        pass
    return n + len(discover_manual_videos(work_dir))


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def call_llm(system, user, *, max_tokens=8192, timeout=180):
    """发一次对话补全，返回正文文本。

    参数按 profile 分档 —— 不同服务商对 temperature / reasoning_effort
    的接受度不同，硬编码一套会「换个模型就 400」。
    profile **不是配置项**，由 pipeline_config.profile_for() 按 model/base_url
    推出来（判据就一条：是不是 k3 家的）。换模型不必再记得换档。

    思考模式按任务类型定：本文件做的是**编排**（场景拆分 + 文案创作），
    按 docs/智能剪辑项目文档.md 的实测结论用 effort=low。刻意不传 temperature：
    deepseek 走默认，k3 只允许 1，不传对两家都合法。
    """
    # required=True：没有 key 就打中文提示并以退出码 2 结束。
    # 2 这个码是契约 —— web 层把它翻译成「请先在页面上填 LLM key」。
    url, key, model = _llm(required=True)
    payload = {
        'model': model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'max_tokens': max_tokens,
    }
    prof = _profile()
    if prof in ('deepseek', 'openai'):
        # 编排任务：不关思考。实测 disabled 会「放飞」——乱拆场景、重复引用同一张图；
        # effort=low 比默认 high 稳（high 的思考 token 计入 max_tokens，会吃空 content）
        payload['reasoning_effort'] = 'low'
    # 走 pipeline_config.post_json 而不是 requests.post：本机 python 对
    # api.deepseek.com 的 TLS 1.3 握手会超时，那里带了一次 TLS 1.2 回退。
    resp = post_json(url, payload, timeout=timeout,
                     headers={'Authorization': f'Bearer {key}'})
    if resp.status_code != 200:
        raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:300]}')
    try:
        content = resp.json()['choices'][0]['message']['content']
    except Exception as e:                                       # noqa: BLE001
        raise RuntimeError(f'响应格式异常: {e}') from e
    if not content or not content.strip():
        raise RuntimeError('LLM 返回为空（多半是 max_tokens 被思考 token 吃光）')
    return content.strip()


def extract_yaml(raw):
    """从可能带 ``` 围栏的回复里取出 YAML 正文。"""
    if '```yaml' in raw:
        return raw.split('```yaml', 1)[-1].rsplit('```', 1)[0].strip()
    if '```' in raw:
        return raw.split('```', 1)[-1].rsplit('```', 1)[0].strip()
    return raw.strip()


# ---------------------------------------------------------------------------
# 校验
#
# 每条错误都带上场景序号，因为重试时只把错误列表发回给 LLM ——
# 「关键词不对」这种话没有指向性，模型只能瞎猜。
# ---------------------------------------------------------------------------

def _keyword_problem(term):
    term = (term or '').strip()
    if not term:
        return '关键词为空'
    if not term.isascii():
        return f'“{term}”含非 ASCII 字符 —— Pexels 只认英文，中文词会退回热门素材'
    words = term.split()
    if not (2 <= len(words) <= 4):
        return f'“{term}”是 {len(words)} 个词，应为 2~4 个英文词'
    return None


def validate_director(text, work_dir, dur, rate=None, spk_id=None,
                      allow_search=True, scenes_min=None):
    """返回 (errors, parsed)。errors 为空即通过，parsed 为解析后的 dict。

    只校验**会让渲染出错或静默降质**的东西，不做风格审查。

    `allow_search=False`（页面上的「自动搜索」关了）时任何 `搜索:` 都判错：
    这一版不联网，`搜索:` 到渲染那一步只会降级成文字卡，与其让它悄悄降级，
    不如现在打回去让模型重写成「少写一场」——用户明确要的是片子短一点。
    `scenes_min` 由 generate() 按本地素材数给（素材少时 3 场是合法的）。
    """
    errs = []
    try:
        parsed = yaml.safe_load(text)
    except Exception as e:                                       # noqa: BLE001
        return [f'不是合法 YAML: {e}'], None
    if not isinstance(parsed, dict):
        return [f'顶层不是映射（拿到 {type(parsed).__name__}）'], None

    proj = parsed.get('project')
    if not isinstance(proj, dict) or not str(proj.get('title') or '').strip():
        errs.append('project.title 缺失或为空')
    voice = parsed.get('voice')
    if not isinstance(voice, dict) or not str(voice.get('spk_id') or '').strip():
        errs.append('voice.spk_id 缺失或为空（渲染会直接 KeyError）')

    scenes = parsed.get('scenes')
    if not isinstance(scenes, list) or not scenes:
        return errs + ['scenes 缺失或为空'], None
    if scenes_min is None:
        scenes_min = SCENES_MIN
    if not (scenes_min <= len(scenes) <= SCENES_MAX):
        errs.append(f'场景数 {len(scenes)} 不在 {scenes_min}~{SCENES_MAX} 之间')

    seen_ids, total_chars = set(), 0
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            errs.append(f'场景[{i:02d}] 不是映射')
            continue
        sid = sc.get('id')
        if sid is None:
            errs.append(f'场景[{i:02d}] 缺 id')
        elif str(sid) in seen_ids:
            errs.append(f'场景[{i:02d}] id={sid} 与前面重复')
        else:
            seen_ids.add(str(sid))
        narration = sc.get('narration')
        if not isinstance(narration, str) or not narration.strip():
            errs.append(f'场景[{i:02d}] narration 为空')
        else:
            total_chars += len(narration.strip())
        spoken = sc.get('spoken')
        if isinstance(narration, str) and isinstance(spoken, str):
            a, b = narration.strip(), spoken.strip()
            # 两者不同**不是**缺陷 —— docs 里 spoken 就是「数字转中文的朗读版」，
            # 项目里的 标致508L 正是这么写的，渲染器两条路都吃。
            # 只有「两边都没有数字」时差异才无法用数字改写解释，那才是文案分叉。
            if a != b and not _HAS_DIGIT.search(a) and not _HAS_DIGIT.search(b):
                errs.append(f'场景[{i:02d}] narration 与 spoken 是两份不同的文案'
                            f'（无数字可解释的差异）')

        # visuals 必须是**一个 dict**（一个场景一份画面配置）。模型有时会写成
        # 列表（想给一个场景塞多张图），这里以前直接 .get() 就 AttributeError ——
        # 校验器自己崩掉，等于把「报错→让模型重写」这条重试路给掐了，
        # 用户看到的是整条生成失败，而不是「第 3 个场景画面写法不对」。
        vis = sc.get('visuals') or {}
        if not isinstance(vis, dict):
            errs.append(f'场景[{i:02d}] visuals 必须是对象（type/source 两个键），'
                        f'不能写成 {type(vis).__name__}；一个场景只配一份画面')
            continue
        vtype = str(vis.get('type') or '')
        src = str(vis.get('source') or '').strip()
        m = _SEARCH_RE.match(src)
        if m and not allow_search:
            # 关了自动搜索还写 搜索: —— 渲染时不会联网，只会降级成文字卡。
            # 当错误打回，让模型把这一场整个去掉（这是用户选的策略：宁可片子短）。
            errs.append(f'场景[{i:02d}] 写了 搜索:{m.group(1).strip()}，'
                        f'但本次关闭了自动搜索 —— 画面只能从本地素材清单里选；'
                        f'清单里没有合适的就把这一场整个删掉，不要用 搜索:')
        elif m:
            prob = _keyword_problem(m.group(1))
            if prob:
                errs.append(f'场景[{i:02d}] 搜索词有问题: {prob}')
        elif src and vtype != 'text':
            # 引用本地文件：渲染找不到会静默降级，所以这里当错误。
            # type: text 例外 —— 渲染器 detect_visual_kind 见到 type=='text'
            # 就直接返回 'text'，根本不去碰 source；那里的 source 是 LLM
            # 顺手写的大字内容，不是路径。查它会误报。
            if not os.path.exists(os.path.join(work_dir, src)):
                errs.append(f'场景[{i:02d}] 引用的本地素材不存在: {src}')

    # 字数预算按**这个音色**的实测语速算。老代码拿固定的 RATE_MIN/MAX 去卡，
    # 带宽 ±2.5 倍，等于没卡：声明 30s 的片出到 52.6s 也能过。
    # 现在收窄到 ±RATE_TOL —— 超了就打回重写，让成片真的接近用户要的秒数。
    if spk_id is None:      # 没指定就按文档里的音色算，别退化成通用常数
        spk_id = str(((parsed.get('voice') or {}).get('spk_id')) or '')
    if rate is None:
        rate = speech_rate.rate_for(spk_id)
    expected = int(round(float(dur) * rate)) if dur > 0 else 0
    if expected:
        lo, hi = int(expected * (1 - RATE_TOL)), int(expected * (1 + RATE_TOL))
        if not (lo <= total_chars <= hi):
            got = speech_rate.dur_for(spk_id or '', total_chars) if spk_id else total_chars / rate
            errs.append(
                f'总旁白 {total_chars} 字，目标 {dur}s 应按 {rate:.1f} 字/秒写 '
                f'{lo}~{hi} 字（当前约合 {got:.0f}s）。'
                f'请调整到目标区间{ "：删减冗余表达" if total_chars > hi else "：补充细节" }，'
                f'不要改变事实内容。')
    return errs, parsed


def apply_defaults(parsed, *, title, dur, size, spk_id='', express=None):
    """补齐渲染器必填项，并把页面上选的音色/尺寸盖上去。

    页面上选的音色必须赢过 LLM 写的那行 —— 用户是在配音区明确选过的。
    """
    parsed.setdefault('project', {})
    if not isinstance(parsed['project'], dict):
        parsed['project'] = {}
    parsed['project']['title'] = title or parsed['project'].get('title') or '未命名'
    parsed['project']['duration'] = dur
    parsed['project'].setdefault('type', '短视频')

    voice = parsed.get('voice')
    if not isinstance(voice, dict):
        voice = {}
    voice.setdefault('speed', '中')
    voice.setdefault('express', True)
    if spk_id:
        voice['spk_id'] = spk_id
    else:
        voice.setdefault('spk_id', '故事女声')
    if express is not None:
        voice['express'] = bool(express)
    parsed['voice'] = voice

    parsed['optimize'] = False                    # 见 config.toml：开启会静默换一版视频
    parsed.setdefault('bgm', {'path': ''})
    out = parsed.get('output')
    if not isinstance(out, dict):
        out = {}
    out.setdefault('subtitle', True)
    out['size'] = SIZES.get(size, SIZES['9:16'])
    parsed['output'] = out
    return parsed


def _one_line(s, width):
    """压掉换行并截断 —— 打印摘要用，别让一条日志占三行。"""
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:width] + ('…' if len(s) > width else '')


def summarize(parsed, path):
    scenes = parsed.get('scenes') or []
    total = sum(len(str(sc.get('narration') or '')) for sc in scenes)
    # 预估秒数按这个音色的实测语速算，别再写死常数 ——
    # 这里原来用的 CHARS_PER_SEC 随着常数块被换成 RATE_TOL 而消失，
    # 成了一个只在这一行被引用的 NameError，每次生成成功都会炸。
    spk = str(((parsed.get('voice') or {}).get('spk_id')) or '')
    return {
        'path': path,
        'title': (parsed.get('project') or {}).get('title', ''),
        'scenes': len(scenes),
        'chars': total,
        'eta_sec': speech_rate.dur_for(spk, total),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate(work_dir, *, title='', source_text='', dur=30,
             size='9:16', preset=DEFAULT_PRESET, spk_id='', express=None,
             max_retry=3, out_name='director.yaml', on_event=None, search=True):
    """生成 director.yaml，返回摘要 dict。

    on_event(kind, payload)：
      'log'      payload=str            一行日志
      'progress' payload=(frac, label)  0..1 + 人话

    `search=False`（页面上的「自动搜索」开关关了）→ 只排本地素材：
    禁 `搜索:`、场景数下限跟着素材数走、一个素材都没有就直接报错退出。
    """
    def log(line):
        print(line, flush=True)
        if on_event:
            on_event('log', line)

    def prog(frac, label):
        if on_event:
            on_event('progress', (frac, label))

    work_dir = os.path.abspath(work_dir)
    if not os.path.isdir(work_dir):
        raise RuntimeError(f'项目目录不存在: {work_dir}')
    if preset not in PRESETS:
        raise RuntimeError(f'未知预设: {preset}（可选 {"/".join(PRESETS)}）')
    preset_label, system_prompt = PRESETS[preset]

    # ---- 收集原文 ----
    article = (source_text or '').strip()
    if not article:
        src = find_source_text(work_dir)
        if not src:
            raise RuntimeError(
                '没有原文可编排：项目里没有 正文内容*.txt，页面上也没有粘正文')
        with open(src, encoding='utf-8') as f:
            article = f.read().strip()
        log(f'📰 原文: {src}（{len(article)} 字）')

    title = (title or '').strip() or os.path.basename(work_dir)
    # 关了搜索又一个素材都没有：死在调 LLM 之前。
    # 让模型「无米下锅」只会得到一份满屏文字卡的 yaml —— 那正是这个开关想避免的，
    # 还得白花一次调用加几轮重试。
    n_local = local_material_count(work_dir)
    if not search and not n_local:
        raise RuntimeError(
            '关闭了自动搜索，但项目里没有本地素材：请先在素材面板上传图片或短视频，'
            '或者把「自动搜索」打开。')
    # 先读懂图片再写稿 —— 顺序不能反。VLM 的输出是**写给 LLM 看的**素材说明，
    # 而渲染时那次 VLM 是事后校验；两者提示词不同，别互相顶替（见 image_brief.py）。
    briefs = []
    if image_brief.manual_images(work_dir):
        log('👁️ 先读懂项目里的图片（本地视觉模型，写稿要用）')
        briefs = image_brief.describe_project(work_dir, on_event=log)
        ok = sum(1 for b in briefs if 'error' not in b['r'])
        if ok:
            log(f'   读懂 {ok}/{len(briefs)} 张，已连同画面内容一起交给 LLM 排版')
    materials = scan_materials(work_dir, briefs=briefs)
    # 字幕预算按**这个音色**的实测语速算（speech_rate.py 会随每条成片自动校准）。
    # 片长由旁白念多久决定，所以「30 秒」只能翻译成「约多少字」这一个杠杆。
    rate = speech_rate.rate_for(spk_id)
    budget = speech_rate.chars_for(spk_id, dur)
    log(f'📝 生成导演文件（{preset_label} · {dur}s · {size}）')
    log(f'   🗣️ 按 {rate:.1f} 字/秒（{spk_id or "本地默认音色"}）算，'
        f'全片旁白预算约 {budget} 字')
    if materials:
        log(f'   本地素材 {len(materials)} 类，已交给 LLM 优先使用')
    if not search:
        log(f'🔌 自动搜索已关闭：只排本地素材（{n_local} 件）')
    prog(0.10, f'{preset_label} 编排')

    user = build_prompt(title, article, materials, dur, total_chars=budget,
                        briefs=briefs, allow_search=search)
    # 关了搜索时下限跟着素材数走：只有 3 张图就不该逼模型凑够 4 场
    # （凑出来的第 4 场只能是文字卡，那正是用户不要的）。
    s_min = SCENES_MIN if search else max(1, min(SCENES_MIN, n_local))
    errors = []
    parsed, content = None, ''

    for attempt in range(1, max(1, max_retry) + 1):
        if attempt > 1:
            log(f'🔁 第 {attempt} 次重试（上次有 {len(errors)} 处不合规）')
        # 重试时把上次的毛病拼回 user 消息。带场景序号，模型才能定点改。
        ask = user
        if errors:
            ask = (user + '\n\n## 上一次的输出有以下问题，请全部修掉后重新输出完整 YAML\n'
                   + '\n'.join(f'- {e}' for e in errors))
        raw = call_llm(system_prompt, ask)
        content = extract_yaml(raw)
        errors, parsed = validate_director(content, work_dir, dur,
                                           rate=rate, spk_id=spk_id,
                                           allow_search=search, scenes_min=s_min)
        if not errors:
            break
        for e in errors:
            log(f'   ⚠️ {e}')
        prog(0.10 + 0.5 * attempt / max(1, max_retry), f'校验未通过，重试 {attempt}')

    if errors:
        # 落盘但仍报出来：让用户能在页面上直接改 yaml，而不是拿到一个空白
        log(f'⚠️ 重试用尽仍有 {len(errors)} 处不合规，已写出供手工修正')

    parsed = apply_defaults(parsed or {}, title=title, dur=dur, size=size,
                            spk_id=spk_id, express=express)
    content = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
    out_path = os.path.join(work_dir, out_name)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)

    info = summarize(parsed, out_path)
    log(f'✅ 导演文件 → {out_path}')
    log(f'   场景数: {info["scenes"]}，总旁白 {info["chars"]} 字，'
        f'预计 {info["eta_sec"]:.0f}s')
    for i, sc in enumerate(parsed.get('scenes') or [], 1):
        # 折行会让摘要错行：text 场景的 source 是 LLM 写的大字文案，常带换行
        vsrc = _one_line(str((sc.get('visuals') or {}).get('source', '')), 32)
        log(f'   [{i:02d}] {sc.get("mood", "平实")} | '
            f'{_one_line(str(sc.get("narration", "")), 34)} | 画面: {vsrc}')
    prog(1.0, '导演文件已生成')
    info['errors'] = errors
    return info


def main():
    ap = argparse.ArgumentParser(description='脚本智能体：生成 director.yaml')
    ap.add_argument('work_dir', help='项目目录')
    ap.add_argument('--title', default='', help='标题（默认取目录名）')
    ap.add_argument('--text', default='', help='直接给正文')
    ap.add_argument('--text-file', default='', help='正文文件路径')
    ap.add_argument('--dur', type=int, default=30)
    ap.add_argument('--size', default='9:16', choices=sorted(SIZES))
    ap.add_argument('--preset', default=DEFAULT_PRESET, choices=sorted(PRESETS))
    ap.add_argument('--spk-id', default='', help='音色（页面选的会盖过 LLM 写的）')
    ap.add_argument('--express', dest='express', action='store_true', default=None)
    ap.add_argument('--no-express', dest='express', action='store_false')
    ap.add_argument('--max-retry', type=int, default=3)
    ap.add_argument('--out', default='director.yaml')
    ap.add_argument('--no-search', dest='no_search', action='store_true',
                    help='只用本地素材，不联网搜索（页面上的「自动搜索」开关）')
    args = ap.parse_args()

    # 正文来源二者互斥，命令行优先
    text = args.text
    if not text and args.text_file:
        with open(args.text_file, encoding='utf-8') as f:
            text = f.read()

    try:
        generate(args.work_dir, title=args.title, source_text=text,
                 dur=args.dur, size=args.size,
                 preset=args.preset, spk_id=args.spk_id, express=args.express,
                 max_retry=args.max_retry, out_name=args.out,
                 search=not args.no_search)
    except RuntimeError as e:
        print(f'❌ {e}', flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
