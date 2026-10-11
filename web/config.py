"""config.py — 读写项目根 config.toml，并构造子进程环境变量

设计要点：
- 唯一配置文件是项目根目录的 config.toml（与 pipeline_config.py 共用），
  不另起 web/config.json，避免两处配置打架。
- 写用 tomlkit，保留文件里既有的说明注释。
- 读取容忍缺段缺键（Web 首次启动时 [llm] 段可能还不存在）。
- 本模块绝不 import torch（本机内存已满，服务器进程必须轻）。
"""
import os
import stat
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 请求参数档的判据表只在 pipeline_config 里存一份 —— 页面显示的和管线实际用的
# 必须是同一个判断，否则「页面说 k3 档、管线其实带了 reasoning_effort」这种事
# 只会在 400 的时候才暴露。只借它的纯函数与常量，不触发读配置。
sys.path.insert(0, str(ROOT))
from pipeline_config import profile_for                       # noqa: E402
from project_paths import VLM_MODEL_DIR                      # noqa: E402
CONFIG_PATH = ROOT / 'config.toml'

DEFAULTS = {
    # [llm] 这一层的平铺键就是**当前生效**的 LLM 配置（pipeline_config.py 照旧读它，
    # 一行都不用改）。[llm.backends.<名>] 是**存起来的备用档**，页面上的
    # 「用这套」动作 = 把那一档的值拷进平铺键。不搞 active 指针是为了不动
    # pipeline_config 和用户现存的 config.toml —— 那把 key 是他唯一的 key。
    'llm': {
        'base_url': 'https://api.deepseek.com/v1/chat/completions',
        'api_key': '',
        'model': 'deepseek-v4-flash',
        # 没有 profile 这一项：请求参数档按 model/base_url 自动判断，
        # 见 pipeline_config.profile_for()。换模型不该还要记得换档。
    },
    'tts': {
        # active 只决定页面上默认选哪一栏，不影响渲染 ——
        # 渲染只看音色名有没有 edge:/openai: 前缀（见 tts_backends.py）
        'active': 'cosyvoice',
        'backends': {
            'edge': {'label': '线上 Edge（免费，无需 key）', 'enabled': True,
                     'voice': 'zh-CN-XiaoxiaoNeural'},
            'openai': {'label': 'OpenAI 兼容接口', 'enabled': False,
                       'base_url': '', 'api_key': '', 'model': 'tts-1',
                       'voice': 'alloy'},
        },
    },
    'vlm': {
        # 本地视觉模型（图片理解）。**触发条件是「手动上传的图片」**，
        # 见 video_make_ffmpeg.is_manual_image() —— 自动抽的视频帧、Pexels
        # 下回来的素材都不会喂给它（一张图约 10s + 4.2GB 显存）。
        'enabled': True,
        # 权重目录。换模型只改这一行，vlm_analyze.py 与 video_make_ffmpeg.py
        # 都从这里读（环境变量 VLM_MODEL_DIR 优先），不再各写一份路径。
        'model_dir': VLM_MODEL_DIR,
        'timeout': 120,               # 单张图上限秒数（含子进程加载模型约 12s）
    },
    'pexels': {'api_key': ''},
    'defaults': {
        'spk_id': '故事女声',
        'size': '9:16',
        'express': True,
        'optimize': False,            # 默认关：optimize 失败会静默换一版视频
        'auto_confirm_keywords': False,
        'cuda': 0,
    },
    'runner': {'max_upload_mb': 2048},
}

# 页面可以改、但**绝不可以**通过 GET 回显的键
SECRET_KEYS = ('api_key',)

_HEADER = """# 本地项目配置
# key 读取链：环境变量 PEXELS_API_KEY > 本文件 > MPT config.toml（可选，默认 <根>/MoneyPrinterTurbo/）
# LLM 段由 pipeline_config.py 读取（环境变量 LLM_* 优先）
"""


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    """返回完整配置（含默认值填充）。"""
    raw = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'rb') as f:
                raw = tomllib.load(f)
        except Exception:
            raw = {}
    return _deep_merge(DEFAULTS, raw)


def _set_nested(tbl, key, value):
    """把 value 写进 tbl[key]，value 是 dict 时**逐层下钻合并**而不是整块替换。

    整块替换会闯祸：`doc['tts']['backends'] = {'openai': {...}}` 会把
    `backends` 表里 edge 那一档一起抹掉 —— 而页面每次只提交它改过的那一档。
    """
    import tomlkit

    if not isinstance(value, dict):
        tbl[key] = value
        return
    if key not in tbl or not hasattr(tbl[key], 'keys'):
        tbl[key] = tomlkit.table()
    sub = tbl[key]
    for k, v in value.items():
        if v is None:
            continue
        if isinstance(v, dict):
            _set_nested(sub, k, v)
        else:
            sub[k] = v
    # 空的中间表（比如一条都没填的 [tts.backends]）不落盘
    if not len(sub):
        del tbl[key]


def save(patch):
    """把 patch 深合并进 config.toml。tomlkit 保留既有注释。

    空串表示「不改」而非「清空」—— 前端回显的是掩码，用户不碰就是空串，
    真按空串写下去会把已配好的 key 抹掉。要清空请显式传字符串 '-'.
    """
    import tomlkit

    if CONFIG_PATH.exists():
        doc = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    else:
        doc = tomlkit.document()
        for line in _HEADER.strip().splitlines():
            doc.add(tomlkit.comment(line[1:].strip() if line.startswith('#')
                                    else line))

    for section, values in (patch or {}).items():
        if not isinstance(values, dict):
            continue
        if section not in doc:
            doc[section] = tomlkit.table()
        tbl = doc[section]
        for k, v in (values or {}).items():
            if v is None:
                continue
            if k in SECRET_KEYS and v == '':
                continue                      # 空 = 不改，见 docstring
            if k in SECRET_KEYS and v == '-':
                tbl[k] = ''
                continue
            _set_nested(tbl, k, v)

    tmp = CONFIG_PATH.with_suffix('.toml.tmp')
    tmp.write_text(tomlkit.dumps(doc), encoding='utf-8')
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)   # 600
    except OSError:
        pass
    return load()


def masked(value, keep=4):
    """密钥掩码：sk-abc12345...z9 -> sk-***00z9（只露头 3 位与尾 4 位）"""
    if not value:
        return ''
    s = str(value)
    if len(s) <= keep + 3:
        return '*' * len(s)
    return f'{s[:3]}***{s[-keep:]}'


def _mask_view(d):
    """一个可能含密钥的 dict → 可回显的视图：api_key 只出掩码 + 是否已配。"""
    out = {k: v for k, v in (d or {}).items()
           if k not in SECRET_KEYS and not isinstance(v, dict)}
    key = (d or {}).get('api_key')
    out['api_key_masked'] = masked(key)
    out['api_key_set'] = bool(key)
    return out


def public_view(cfg=None):
    """给前端的配置视图：**密钥只给掩码**，并标注是否已配置。"""
    cfg = cfg or load()
    llm, px, tts = cfg['llm'], cfg['pexels'], cfg['tts']
    llm_view = _mask_view(llm)
    # [llm.backends.*] 是备用档，只回显掩码与非密钥字段
    llm_view['backends'] = {
        str(name): _mask_view(v)
        for name, v in (llm.get('backends') or {}).items()
        if isinstance(v, dict)}
    # profile 不再是配置项，而是**推导结果**：页面只读地显示出来，
    # 让人能确认「现在这回请求到底带不带 reasoning_effort」，而不是让人去选。
    # 页面显示的和管线实际用的走的是同一个函数，见模块头的注释。
    llm_view['profile'] = profile_for(llm.get('model'), llm.get('base_url'))
    return {
        'llm': llm_view,
        'tts': {
            'active': tts.get('active', 'cosyvoice'),
            'backends': {
                str(name): _mask_view(v)
                for name, v in (tts.get('backends') or {}).items()
                if isinstance(v, dict)},
        },
        'vlm': _mask_view(cfg['vlm']),
        'pexels': _mask_view(px),
        'defaults': cfg['defaults'],
        'runner': cfg['runner'],
        'config_path': str(CONFIG_PATH),
    }


def build_env(cfg=None, extra=None):
    """子进程环境：os.environ.copy() + 注入。

    env= 是「替换」而非「合并」，只用注入项构造会丢掉 PATH，
    导致管线内部所有裸名 ffmpeg 调用失败 —— 必须先 copy。
    """
    cfg = cfg or load()
    env = os.environ.copy()
    env.update({
        'LLM_API_URL': cfg['llm']['base_url'],
        'LLM_API_KEY': cfg['llm']['api_key'],
        'LLM_MODEL': cfg['llm']['model'],
        # 刻意**不注入** LLM_PROFILE：那个环境变量是「显式覆盖自动判断」的开关，
        # 一旦在这里钉死，pipeline_config 就会一直用旧值，
        # 换模型改成 k3 也还是带 reasoning_effort → 400。
        'PEXELS_API_KEY': cfg['pexels']['api_key'],
        'PYTHONUNBUFFERED': '1',          # 与 argv 的 -u 双保险
        'PYTHONIOENCODING': 'utf-8',      # C locale 下 emoji print 会炸
        'CUDA_VISIBLE_DEVICES': str(cfg['defaults'].get('cuda', 0)),
    })
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return {k: v for k, v in env.items() if v is not None}


def env_snapshot(cfg=None):
    """写进 job 的配置快照 —— 绝不含 api_key。"""
    cfg = cfg or load()
    return {
        'model': cfg['llm']['model'],
        'base_url': cfg['llm']['base_url'],
        # 快照里记的是**实际会用**的档，不是「配置里写的」—— 后者已经不存在了
        'profile': profile_for(cfg['llm'].get('model'), cfg['llm'].get('base_url')),
        'spk_id': cfg['defaults'].get('spk_id'),
        'pexels_set': bool(cfg['pexels']['api_key']),
    }


def self_check():
    """启动自检：确认注入后的环境里 ffmpeg 仍能被找到。"""
    env = build_env()
    problems = []
    if not shutil.which('ffmpeg', path=env.get('PATH')):
        problems.append('注入环境后找不到 ffmpeg（PATH 丢失？）')
    if not shutil.which('ffprobe', path=env.get('PATH')):
        problems.append('注入环境后找不到 ffprobe（PATH 丢失？）')
    return problems
