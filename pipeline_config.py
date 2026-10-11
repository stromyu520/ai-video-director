#!/usr/bin/env python3
"""pipeline_config.py — 全项目唯一的 LLM 配置来源

改动前，同一把明文密钥硬编码在 5 个脚本里（text_to_director / plan_remix /
check_materials / video_make_ffmpeg / tts_express）。换模型要改 5 处，
把项目目录打包发出去还会跟着源码泄漏密钥。

优先级：环境变量 > config.toml > （url/model 有中性默认，key 刻意无默认）

被 5 个脚本共用，一律在 import 时求值一次 —— 语义与改动前完全一致：
进程启动即冻结配置，运行中途改配置不影响已启动的进程。

用法（与改动前的三行写法等价，只是不再有明文密钥）：
    from pipeline_config import llm
    LLM_API_URL, LLM_API_KEY, LLM_MODEL = llm()

本文件刻意不 import 任何重资源：每个管线脚本都在顶部导入它。
"""
import os
import sys
import urllib.parse
from pathlib import Path

try:
    import tomllib                      # py3.11+（video-env 是 3.11）
except ModuleNotFoundError:             # 系统 python3 是 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:         # pragma: no cover
        tomllib = None

ROOT = Path(__file__).resolve().parent
import project_paths as _paths
CONFIG_PATH = ROOT / 'config.toml'

DEFAULT_URL = 'https://api.deepseek.com/v1/chat/completions'
DEFAULT_MODEL = 'deepseek-v4-flash'
DEFAULT_PROFILE = 'deepseek'
DEFAULT_VLM_DIR = str(_paths.VLM_MODEL_DIR)

# 用户素材的两个落点。`素材/` 是主目录（页面上传的图片和短视频都放这一层，
# 靠扩展名区分）；`图片/` 是存量项目与 Pexels 自动下载照片的落点，
# 见 video_make_ffmpeg.is_manual_image()。
#
# 放在这里而不是 video_make_ffmpeg.py，是因为 **web 服务进程也要用它**
# （素材面板要列这两个目录），而服务器绝不能 import video_make_ffmpeg ——
# 那条路会把整个渲染模块拉进内存，本机 15G 已满。本文件又已经是
# 「两边都 import 的唯一共享件」。
#
# 约定只有这一份拷贝：改目录名要改的是这里 + 各处已经拼好的相对路径
# （director.yaml / remix.yaml 里的 source），两件事都得做，缺一即断链。
MANUAL_DIRS = ('素材', '图片')

_cache = {}


def _section(name):
    """读 config.toml 的一个段。文件缺失/解析失败一律当空 —— 配置读不出来
    不该让管线直接崩，应当走各自的「无 key」降级路径。"""
    if name not in _cache:
        data = {}
        if tomllib is not None and CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'rb') as f:
                    data = tomllib.load(f).get(name) or {}
            except Exception:
                data = {}
        _cache[name] = data if isinstance(data, dict) else {}
    return _cache[name]


def llm(required=False):
    """返回 (api_url, api_key, model)。

    key 没有默认值：没配就是空串，由调用方决定降级还是报错。
    `required=True` 供离了 key 就没法工作的脚本使用（报中文提示 + 退出码 2，
    Web 层把这个码翻译成「请先填 LLM key」）。
    """
    cfg = _section('llm')
    url = os.environ.get('LLM_API_URL') or cfg.get('base_url') or DEFAULT_URL
    key = os.environ.get('LLM_API_KEY') or cfg.get('api_key') or ''
    model = os.environ.get('LLM_MODEL') or cfg.get('model') or DEFAULT_MODEL
    if required and not key:
        missing_key_exit()
    return url, key, model


# 「哪家的模型该吃哪套请求参数」—— 关键词命中即归入该档。
# 参数档本来就不是一个该让人**选**的东西：它是服务商的脾气，
# 而这件事在 model / base_url 里已经写着。多一个下拉框，就多一处
# 「换了模型但忘了换档 → 400」的机会。
PROFILE_RULES = (
    ('k3', ('k3', 'kimi', 'moonshot')),
)


def profile_for(model, base_url=''):
    """按模型名 / 接口地址判断该用哪套请求参数 —— 纯函数，不读文件。

    单独拎出来是给 web 层用的：它自己用 tomlkit 读配置，拿读到的值调这里，
    就不必在页面那边再抄一份关键词表（两份表迟早会不一致）。
    """
    haystack = f'{model or ""} {base_url or ""}'.lower()
    for name, keywords in PROFILE_RULES:
        if any(k in haystack for k in keywords):
            return name
    return DEFAULT_PROFILE


def profile():
    """请求参数档，**按配好的模型自动判断**（deepseek | k3）。

    两档的差别只有一条：发请求时带不带 reasoning_effort。
      · k3 / Moonshot —— 只接受 temperature=1，也不认 reasoning_effort，带了直接 400
      · 其余（DeepSeek 及任何 OpenAI 兼容接口）—— 带 effort=low

    所以判据就是「这个模型是不是 k3 家的」，命中关键词即可，不需要用户选。
    显式设 LLM_PROFILE 可以盖掉自动判断 —— 留给排查用，页面上不暴露。
    """
    env = os.environ.get('LLM_PROFILE')
    if env:
        return env
    cfg = _section('llm')
    model = os.environ.get('LLM_MODEL') or cfg.get('model') or DEFAULT_MODEL
    base_url = os.environ.get('LLM_API_URL') or cfg.get('base_url') or DEFAULT_URL
    return profile_for(model, base_url)


def defaults():
    """[defaults] 段：音色、尺寸、是否智能编排等界面/智能体默认值。"""
    return dict(_section('defaults'))


def vlm():
    """本地视觉模型（图片理解）的配置。

    目前只有 vlm_analyze.py 与 video_make_ffmpeg.py 用得上，但路径归这里管 ——
    「同一个路径在几个文件里各写一份」正是当初做 pipeline_config 要解决的问题，
    而 Qwen3-VL/Qwen3.5-2B 这次换模型恰好又踩了一遍（见 vlm_analyze.py 的注释）。
    """
    cfg = _section('vlm')
    try:
        timeout = int(cfg.get('timeout') or 120)
    except (TypeError, ValueError):
        timeout = 120
    return {
        'model_dir': (os.environ.get('VLM_MODEL_DIR')
                      or cfg.get('model_dir') or DEFAULT_VLM_DIR),
        'enabled': bool(cfg.get('enabled', True)),
        'timeout': timeout,
    }


def post_json(url, payload, *, timeout=180, headers=None):
    """POST 一个 JSON，**TLS 1.3 握手失败就退回 TLS 1.2 再试一次**。

    本机实测（2026-09-18）：python 3.11 + OpenSSL 3.5.5 对 api.deepseek.com
    （解析到腾讯 EdgeOne 的 119.188.220.215）做 TLS 1.3 握手会**超时**，
    而同一时刻：
      · curl 的 TLS 1.3 正常
      · python 强制 `maximum_version=TLSv1_2` 也正常
      · python 连 pypi/百度 都正常
    也就是说，既不是断网、也不是对面挂了，是这两家 TLS 1.3 的握手谈不拢。

    症状特别容易误判：`SSLError: UNEXPECTED_EOF_WHILE_READING` 或超时，
    看着像网络抖动，于是重试、换 key、查余额，全都不对症。所以这里把回退
    写死在发请求的地方 —— 上层（director_agent 等）不必知道这件事。

    只在失败后降级，不是一上来就砍到 1.2：能谈成 1.3 的时候不该降级。
    """
    import requests

    # 同一进程里记一笔：这个站已经证实谈不成 1.3，后面直接走 1.2。
    # 不然每次调用都要先白等一次握手超时（实测 10s 上下），
    # 而本文件被 import 时求值一次、进程里会发好几次请求（重试、批量）。
    host = urllib.parse.urlsplit(url).hostname or ''
    if host in _TLS12_HOSTS:
        return _post_tls12(url, payload, timeout=timeout, headers=headers)

    try:
        return requests.post(url, json=payload, timeout=timeout, headers=headers)
    except (requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as first:
        try:
            resp = _post_tls12(url, payload, timeout=timeout, headers=headers)
        except Exception:
            raise first          # 两次都失败 → 报**第一次**的错，那才是原始病因
        if host:
            _TLS12_HOSTS.add(host)
        return resp


_TLS12_HOSTS = set()


def _post_tls12(url, payload, *, timeout=180, headers=None):
    """把 TLS 上限压到 1.2 再发一次。见 post_json 的说明。"""
    import requests
    import ssl
    from requests.adapters import HTTPAdapter

    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2

    class _TLS12Adapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw['ssl_context'] = ctx
            return super().init_poolmanager(*a, **kw)

    sess = requests.Session()
    sess.mount('https://', _TLS12Adapter())
    return sess.post(url, json=payload, timeout=timeout, headers=headers)


def missing_key_exit():
    print('❌ 未配置 LLM API key，无法继续。任选其一：\n'
          f'   1. 在 {CONFIG_PATH} 里填 [llm] api_key = "..."\n'
          '   2. 设环境变量 LLM_API_KEY=...', file=sys.stderr)
    sys.exit(2)                          # 2 = 配置缺失，区别于 1 = 运行出错


if __name__ == '__main__':               # 自查用：python pipeline_config.py
    _url, _key, _model = llm()
    print(f'config    {CONFIG_PATH}  ({"存在" if CONFIG_PATH.exists() else "不存在"})')
    print(f'base_url  {_url}')
    print(f'model     {_model}')
    print(f'profile   {profile()}')
    print(f'api_key   {"已配置 " + _key[:3] + "***" + _key[-4:] if _key else "未配置"}')
    print(f'defaults  {defaults()}')
