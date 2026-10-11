"""pipelines.py — 管线注册表：步骤、argv 构造、阶段权重

三种产出模式 = 两条管线 × 一个模式参数（见计划文档）：
  director  新短视频生成：director.yaml → video_make_ffmpeg.py → output.mp4
  remix     视频解说 / 二次创作：analyze → plan(mode) → 关键词闸门 → resolve → render
            差别只在 plan 的 --mode：smart=视频解说（保留部分原声）/ full=全解说

阶段权重表（phases）里每一对数字是【该步骤内部】的 0..1 区间，不是全片进度；
跨步骤的全局映射由 jobs._push_progress 按 StepSpec.weight 完成。

本模块只做「描述」，不执行任何东西 —— 不 import torch，不起进程。
"""
import asyncio
import os
import re
from pathlib import Path

from .fsutil import probe_video
from .models import StepSpec, PipelineSpec

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 阶段标记表
#
# 全部来自各脚本真实 print（2026-09-18 逐个 grep 核对），不是推测：
#   video_make_ffmpeg.py:1091/1171/78/1193/1250/1266/1288/1292/1306
#   analyze_video.py:28/31/64/84、plan_remix.py:243/280
#   resolve_materials.py:167/100/138/199、render_remix.py:93/119/150/209/237/618/714/738
#
# 键的顺序即匹配顺序：feed() 命中第一条就 break，所以粗粒度的阶段标记
# 必须排在细粒度之前，否则细的会把粗的盖掉。
# ---------------------------------------------------------------------------

# director 渲染（video_make_ffmpeg.py）
PHASES_DIRECTOR_RENDER = {
    r'📐\s*输出尺寸':            (0.01, 0.03),
    r'📷\s*Phase 1/2':           (0.03, 0.10),   # 图片预处理 + VLM
    r'🎤\s*Phase 2/2':           (0.10, 0.90),   # 语音合成 + 渲染（N 场景）
    r'⚡\s*批量合成':             (0.12, 0.34),   # 内部由 [i/N] 细化
    r'🎬\s*场景\s*\d+\s':        (0.34, 0.78),   # 逐场景 TTS 收尾，由「场景N」细化
    r'🎥\s*共\s*\d+\s*个镜头':    (0.80, 0.86),
    r'🧩\s*拼接':                (0.86, 0.90),
    r'💬\s*字幕':                (0.90, 0.94),
    r'🎵\s*BGM':                 (0.94, 0.97),
    r'✅\s*完成':                (0.97, 1.00),
}

# 二创分析（analyze_video.py）—— 最长的静默是 whisper 转写，中间一行都不打
PHASES_ANALYZE = {
    r'📹\s*时长':                (0.01, 0.06),
    r'📹\s*镜头检测':            (0.06, 0.26),
    r'🎤\s*音频转录':            (0.26, 0.80),
    r'🖼️\s*关键帧':              (0.80, 0.96),
}

# 二创编排（plan_remix.py）—— 一次 LLM 调用，中间无输出，全靠 creep
PHASES_PLAN = {
    r'🧠\s*DeepSeek':            (0.02, 0.30),
    r'✅\s*二创方案':            (0.96, 1.00),
}

# 素材解析（resolve_materials.py）—— 逐场景下载，由 [i/N] 细化
PHASES_RESOLVE = {
    r'📐\s*素材方向':            (0.01, 0.05),
    r'🔍\s*待搜索素材':          (0.05, 0.10),
    r'✅\s*照片|✅\s*视频':       (0.10, 0.95),
}

# 二创渲染（render_remix.py）—— 逐场景 TTS + 渲染，由「场景N」细化
PHASES_REMIX_RENDER = {
    r'🖼️\s*预处理图片':          (0.01, 0.04),
    r'🎬\s*预处理短片':          (0.04, 0.07),
    r'✅\s*素材预处理完成':       (0.07, 0.10),
    r'📐\s*视频尺寸':            (0.10, 0.12),
    r'🎬\s*二创渲染':            (0.12, 0.90),
    r'🧩\s*交叉溶解拼接':        (0.90, 0.95),
    r'🎵\s*BGM':                 (0.95, 0.97),
    r'✅\s*二创完成':            (0.97, 1.00),
}

# LLM 生成脚本（director_agent.py）—— 由 on_event 回调驱动时不用这张表，
# 这里是 CLI 子进程模式下的兜底（脚本会打 emoji 行）
PHASES_SCRIPT_GEN = {
    r'🧠|📝\s*生成':             (0.05, 0.60),
    r'✅\s*导演文件|✅\s*已生成':  (0.90, 1.00),
}


# ---------------------------------------------------------------------------
# argv 构造
#
# 入参是 Job，返回 argv（不含解释器，jobs 会补 [PYTHON, '-u']）。
# 环境变量（LLM_* / PEXELS_API_KEY）由 config.build_env() 注入，不走 argv ——
# key 绝不能出现在命令行上（ps 可见）。
# ---------------------------------------------------------------------------

def _work_dir(job):
    wd = job.work_dir
    if not wd:
        raise ValueError('任务缺少 work_dir')
    return wd


def _arg_path(job, rel):
    """把 job.params 里的相对路径钉到 work_dir 下。"""
    return os.path.join(_work_dir(job), rel)


def _no_search(argv, job):
    """页面上的「自动搜索」开关 → 追加 `--no-search`（在 argv 末尾追加并返回）。

    判据是**显式 False**，不是「非真值」：params 缺这个键时必须保持今天的
    行为（联网搜索）。老任务、直接从 API 提交的任务都不带这个键，
    非真值判断会把它们悄悄变成「不搜索」—— 和 --express/--no-express 同一条规矩。

    五个脚本各认一遍这个标志（生成/渲染/编排/下载），少一个就会出现
    「提示词说不搜了，下游还是偷偷下载」。
    """
    if job.params.get('search') is False:
        argv.append('--no-search')
    return argv


# 页面粘进来的正文落盘到这个名字。带「正文内容」前缀是刻意的：
# director_agent.find_source_text 优先 glob 正文内容*.txt，所以将来用户
# 自己往项目里丢的 正文内容.txt 会名正言顺地排在它前面。
SOURCE_TXT = '正文内容_页面输入.txt'


def build_generate(job):
    """本地 py 智能体（director_agent.py）：出 director.yaml。

    做成 script 步骤而不是 fn 步骤，是为了让编排期间那几行 emoji print
    以子进程 stdout 流出来 —— PHASES_SCRIPT_GEN 就是靠它们推进度的，
    同时取消时能随进程组一起 killpg 掉。

    正文走 --text-file 而不是 --text：几百字摆上 argv 会进 ps，
    且文件留在项目里，导出/复查时看得见「这份稿子是怎么来的」。
    """
    wd = _work_dir(job)
    argv = [str(ROOT / 'director_agent.py'), wd]

    src = os.path.join(wd, SOURCE_TXT)
    if job.params.get('source_text'):
        if not os.path.exists(src):
            raise ValueError(f'{SOURCE_TXT} 不在 {wd} 里（prepare 步骤本应先写出）')
        argv += ['--text-file', src]
    if job.params.get('title'):
        argv += ['--title', str(job.params['title'])]

    argv += ['--dur', str(int(job.params.get('duration') or 30))]
    argv += ['--size', str(job.params.get('size') or '9:16')]
    argv += ['--preset', str(job.params.get('preset') or 'news')]
    argv += ['--max-retry', str(int(job.params.get('max_retry') or 3))]

    # 页面上选的音色必须压过 LLM 自己在 yaml 里写的那个 ——
    # 这是「本地/线上 TTS」这条需求在生成侧的唯一落点。
    if job.params.get('spk_id'):
        argv += ['--spk-id', str(job.params['spk_id'])]
    express = job.params.get('express')
    if express is True:
        argv.append('--express')
    elif express is False:
        argv.append('--no-express')
    return _no_search(argv, job)


def build_director_render(job):
    yaml_path = job.params.get('director_yaml') or 'director.yaml'
    path = (yaml_path if os.path.isabs(yaml_path)
            else _arg_path(job, yaml_path))
    if not os.path.exists(path):
        raise ValueError(f'找不到导演文件: {path}')
    return _no_search([str(ROOT / 'video_make_ffmpeg.py'), path], job)


def build_analyze(job):
    src = _source_video(job)
    return [str(ROOT / 'analyze_video.py'), src, _work_dir(job)]


def build_plan(job):
    mode = job.params.get('mode', 'smart')
    if mode not in ('smart', 'full'):
        raise ValueError(f'未知解说模式: {mode}（只支持 smart | full）')
    argv = [str(ROOT / 'plan_remix.py'), _work_dir(job), '--mode', mode]
    bgm = job.params.get('bgm')
    if bgm and os.path.exists(bgm):
        argv += ['--bgm', bgm]
    return _no_search(argv, job)


def build_resolve(job):
    # 一律 -y：不带 -y 时它在 stdin=DEVNULL 上 input() → EOFError →
    # 打印「已取消」但退出码 0，任务会假装成功却什么都没做。
    return _no_search([str(ROOT / 'resolve_materials.py'), _work_dir(job), '-y'], job)


def build_remix_render(job):
    argv = [str(ROOT / 'render_remix.py'), _work_dir(job)]
    bgm = job.params.get('bgm')
    if bgm and os.path.exists(bgm):
        argv += ['--bgm', bgm]
    return _no_search(argv, job)


def _source_video(job):
    """二创的源视频：优先 params.source，否则在 work_dir 里找唯一的视频。"""
    src = job.params.get('source')
    if src and os.path.exists(src):
        return src
    wd = Path(_work_dir(job))
    vids = [p for p in sorted(wd.iterdir())
            if p.suffix.lower() in ('.mp4', '.mov', '.mkv', '.avi', '.webm')
            and not p.name.startswith('.')]
    if not vids:
        raise ValueError(f'{wd} 下找不到源视频（上传后再提交任务）')
    if len(vids) > 1:
        raise ValueError(f'{wd} 下有多个视频，请在 params.source 指定：'
                         + '、'.join(p.name for p in vids[:5]))
    return str(vids[0])


# ---------------------------------------------------------------------------
# fn 步骤（在服务器进程内跑；严禁 import torch / 严禁阻塞事件循环）
# ---------------------------------------------------------------------------

async def _step_prepare(job):
    """建目录 + 早期校验 + 把页面粘的正文落成项目里的文件。

    排在 generate **之前**：智能体要读的正文得先存在。yaml 的存在性检查
    放在这里天然放行 —— 勾了「智能编写脚本」就由下一步去产出它。
    """
    wd = Path(_work_dir(job))
    await asyncio.to_thread(lambda: wd.mkdir(parents=True, exist_ok=True))
    if job.pipeline == 'director':
        text = job.params.get('source_text')
        if text and job.params.get('write_script'):
            await asyncio.to_thread(
                (wd / SOURCE_TXT).write_text, str(text), 'utf-8')
        yaml_path = job.params.get('director_yaml') or 'director.yaml'
        p = Path(yaml_path if os.path.isabs(yaml_path)
                 else wd / yaml_path)
        if job.params.get('write_script') and not p.exists():
            return          # 交给 generate 步骤去产出
        if not p.exists():
            raise FileNotFoundError(
                f'{p} 不存在。「生成脚本并渲染」会先产出它；'
                f'也可以先把 director.yaml 放进项目目录，再点「只渲染现有脚本」。')


async def _step_remix_ingest(job):
    """二创：确认源视频就位，并把实测尺寸写进 params 供前端与 resolve 用。"""
    src = _source_video(job)
    job.params['source'] = src
    probe = await asyncio.to_thread(probe_video, src)
    job.params['video'] = probe


async def _step_keywords_gate(job, payload):
    """关键词确认闸门：把前端改过的搜索词写回 remix.yaml。

    payload: {"terms": {"<场景序号>": "new term"}, "skip": bool}
    场景序号是 remix.yaml 里 scenes 的 0 基下标，由前端从 /api/projects/{p}/remix
    拿到并原样回传。
    """
    terms = (payload or {}).get('terms') or {}
    if not terms:
        return
    await asyncio.to_thread(_apply_terms, Path(_work_dir(job)) / 'remix.yaml',
                            terms)


def _apply_terms(remix_path, terms):
    import yaml
    data = yaml.safe_load(remix_path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        return
    scenes = (data.get('remix') or data).get('scenes') or []
    changed = 0
    for idx_s, term in terms.items():
        try:
            idx = int(idx_s)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(scenes)):
            continue
        sc = scenes[idx]
        cur = str(sc.get('source') or '')
        if not cur.startswith('搜索:'):
            continue
        term = str(term).strip()
        if not term or term == cur[3:]:
            continue
        sc['source'] = f'搜索:{term}'
        changed += 1
    if changed:
        remix_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding='utf-8')


# ---------------------------------------------------------------------------
# 谓词：决定可选步骤跑不跑
# ---------------------------------------------------------------------------

def _need_generate(job):
    """点了「生成脚本并渲染」才调 LLM；「只渲染现有脚本」走已有 director.yaml。

    页面上没有这个开关了 —— 它现在由运行栏的两个按钮决定，
    但归到底还是这一个布尔量，所以谓词不动。

    注意 params 是 dict，前端传 JSON 布尔，别用字符串真值判断。
    """
    return bool(job.params.get('write_script'))


def _need_keyword_gate(job):
    """自动放行时不挂起。偏好存在 config.defaults.auto_confirm_keywords。

    关了自动搜索就没有关键词可确认（plan 步骤的提示词禁了 `搜索:`），
    直接放行 —— 否则会挂一个空弹窗，等人点一个「确认」才继续，
    而那个确认改不动任何东西。
    """
    if job.params.get('search') is False:
        return False
    if job.params.get('auto_confirm_keywords'):
        return False
    from . import config
    return not config.load()['defaults'].get('auto_confirm_keywords', False)


# ---------------------------------------------------------------------------
# 管线注册表
# ---------------------------------------------------------------------------

PIPELINES = {
    # 新短视频生成：手写或 LLM 生成 director.yaml → 渲染
    'director': PipelineSpec(
        key='director',
        label='新短视频生成',
        steps=[
            StepSpec(key='prepare', label='准备项目', kind='fn',
                     fn=_step_prepare, weight=0.2, hard_timeout=120.0),
            # 不设 optional：生成失败就没有 yaml 可渲染，那时把任务标成
            # 「完成」只是骗人（optional 分支会把 job 置 done 只留一条警告）。
            StepSpec(key='generate', label='生成导演文件', kind='script',
                     builder=build_generate, when=_need_generate,
                     weight=2.0,
                     idle_timeout=180.0,      # 单次 LLM 调用 HTTP 超时 120s
                     hard_timeout=900.0),
            StepSpec(key='render', label='渲染成片', kind='script',
                     builder=build_director_render, weight=8.0,
                     idle_timeout=300.0,      # 逐镜头渲染循环可能几分钟无输出
                     hard_timeout=3600.0,
                     produces=('output.mp4',)),
        ],
        phases={'render': PHASES_DIRECTOR_RENDER,
                'generate': PHASES_SCRIPT_GEN},
    ),

    # 视频解说（mode=smart）/ 二次创作（mode=full）：同一链条，只差 mode
    'remix': PipelineSpec(
        key='remix',
        label='视频解说 / 二次创作',
        steps=[
            StepSpec(key='ingest', label='接入原片', kind='fn',
                     fn=_step_remix_ingest, weight=0.5, hard_timeout=300.0),
            StepSpec(key='analyze', label='分析原片', kind='script',
                     builder=build_analyze, weight=5.0,
                     idle_timeout=600.0,      # whisper 转写 + 逐镜头关键帧
                     hard_timeout=1800.0,
                     produces=('analysis.json',)),
            StepSpec(key='plan', label='编排二创方案', kind='script',
                     builder=build_plan, weight=2.0,
                     idle_timeout=180.0,      # 单次 LLM 调用，HTTP 超时 120s
                     hard_timeout=600.0,
                     produces=('remix.yaml',)),
            StepSpec(key='keywords', label='确认搜索关键词', kind='gate',
                     fn=_step_keywords_gate, when=_need_keyword_gate,
                     weight=0.1),
            StepSpec(key='resolve', label='下载素材', kind='script',
                     builder=build_resolve, weight=4.0,
                     idle_timeout=240.0,
                     hard_timeout=1800.0),
            StepSpec(key='render', label='渲染成片', kind='script',
                     builder=build_remix_render, weight=8.0,
                     idle_timeout=300.0,
                     hard_timeout=3600.0,
                     produces=('final_remix.mp4',)),
        ],
        phases={'analyze': PHASES_ANALYZE,
                'plan': PHASES_PLAN,
                'resolve': PHASES_RESOLVE,
                'render': PHASES_REMIX_RENDER},
    ),
}


def validate_params(pipeline_key, params):
    """提交前校验，返回错误消息列表（空 = 通过）。"""
    errs = []
    if pipeline_key not in PIPELINES:
        return [f'未知管线: {pipeline_key}']
    p = params or {}
    if pipeline_key == 'remix':
        if not p.get('source') and not p.get('video_uploaded'):
            errs.append('二创管线需要先上传原片')
        mode = p.get('mode', 'smart')
        if mode not in ('smart', 'full'):
            errs.append(f'未知模式: {mode}')
    if pipeline_key == 'director':
        if not p.get('write_script') and not p.get('director_yaml_exists'):
            errs.append('项目里没有 director.yaml：'
                        '要么点「生成脚本并渲染」，要么先把文件放进项目目录')
        # 关着搜索又没有本地素材：智能体自己也会拒绝（director_agent.generate），
        # 但那时任务已经跑起来了。同一件事提到提交前说，用户少等一轮。
        # 二创管线**不设**这道闸：它可以只靠原片和关键帧成片，不插素材也能出。
        if p.get('search') is False and p.get('write_script') \
                and not p.get('local_material_count'):
            errs.append('关闭了自动搜索，但项目里还没有本地素材：'
                        '先在素材面板上传图片或短视频，或者把「自动搜索」打开')
    return errs


def output_path(job):
    """成片的绝对路径（渲染成功后在 work_dir 下）。"""
    wd = Path(job.work_dir)
    if job.pipeline == 'remix':
        return wd / 'final_remix.mp4'
    # video_make 允许 director.yaml 用 output.path 覆盖，但默认是 output.mp4
    return wd / 'output.mp4'


def preview_files(job):
    """前端可直接预览的产物（相对 work_dir 的路径）。"""
    wd = Path(job.work_dir)
    out = []
    for name in ('output.mp4', 'final_remix.mp4', 'director.yaml',
                 'director_optimized.yaml', 'remix.yaml', 'analysis.json'):
        if (wd / name).exists():
            out.append(name)
    return out
