"""project_paths.py — 全项目路径的唯一来源

改动前，本机目录（/home/wb/dev/test、vlm-env、MoneyPrinterTurbo ...）散落在
十几个脚本里，且都是绝对路径。项目一挪窝、或者别人 clone 到自己的目录，
要么跑不起来，要么得挨个文件改。

现在所有「项目之外的路径」都从这里取，每一项都可被环境变量覆盖：

    PROJECT_ROOT     项目根目录          默认 = 本文件所在目录
    COSYVOICE_DIR    CosyVoice 源码目录   默认 <根>/CosyVoice
    MODELS_DIR       模型总目录           默认 <根>/models
    BGM_DIR           BGM 成品输出目录    默认 <根>/BGM
    COSYVOICE_MODEL  音色克隆权重         默认 <MODELS_DIR>/Fun-CosyVoice3-0-5B-2512
    WHISPER_MODEL    转录权重             默认 <MODELS_DIR>/whisper-small
    DEMUCS_REPO      人声分离模型         默认 <MODELS_DIR>/demucs_repo
    REF_AUDIO        音色注册的官方参考音频 默认 <COSYVOICE_DIR>/asset/zero_shot_prompt.wav
    PIPELINE_PYTHON  跑管线的解释器        默认 = 当前解释器 sys.executable
    VLM_PYTHON       视觉模型的独立环境    默认 <根>/vlm-env/bin/python
    VLM_MODEL_DIR    视觉模型权重         默认 <MODELS_DIR>/Qwen3.5-2B
    MPT_CONFIG       MoneyPrinterTurbo 配置（可选，复用其 Pexels/NAS key）
    AZURE_VOICES_JSON  edge-tts 的静态音色表（可选兜底）

装到别的机器上，一般只需要保证「模型在本项目 models/ 下」或者设一次
MODELS_DIR，其余沿用默认值即可。见 README 的「安装」一节。

本文件刻意只 import os/sys/pathlib —— 每个管线脚本都会在顶部导入它，
绝不能把 torch 之类的重货拖进来（web 服务器进程同样会 import 本文件）。
"""
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get('PROJECT_ROOT') or Path(__file__).resolve().parent)


def _p(env, default):
    """环境变量优先，否则用默认值，统一返回 Path。"""
    return Path(os.environ.get(env) or default)


COSYVOICE_DIR = _p('COSYVOICE_DIR', ROOT / 'CosyVoice')
MATCHA_DIR    = COSYVOICE_DIR / 'third_party' / 'Matcha-TTS'
MODELS_DIR    = _p('MODELS_DIR', ROOT / 'models')
BGM_DIR       = _p('BGM_DIR', ROOT / 'BGM')

COSYVOICE_MODEL = _p('COSYVOICE_MODEL', MODELS_DIR / 'Fun-CosyVoice3-0-5B-2512')
WHISPER_MODEL   = _p('WHISPER_MODEL', MODELS_DIR / 'whisper-small')
DEMUCS_REPO     = _p('DEMUCS_REPO', MODELS_DIR / 'demucs_repo')
REF_AUDIO       = _p('REF_AUDIO', COSYVOICE_DIR / 'asset' / 'zero_shot_prompt.wav')

# 管线脚本与 TTS 后端一律用同一个解释器：默认就是正在跑当前脚本的这个，
# 所以「用哪个 python 启动」，子进程就跟着用哪个，不需要额外配置。
PYTHON = os.environ.get('PIPELINE_PYTHON') or sys.executable

# 视觉模型（Qwen3.5）与视频管线不共用一个环境 —— transformers 版本打架，
# 见 vlm_analyze.py 顶部。真要装到同一环境，把 VLM_PYTHON 指成同一个 python。
VLM_PYTHON    = str(_p('VLM_PYTHON', ROOT / 'vlm-env' / 'bin' / 'python'))
VLM_MODEL_DIR = str(_p('VLM_MODEL_DIR', MODELS_DIR / 'Qwen3.5-2B'))

# 以下两项是**可选**的外部复用：有就用，没有就降级（Pexels key 留空、
# edge-tts 联网取音色表），不影响主流程。
MPT_CONFIG        = str(_p('MPT_CONFIG', ROOT / 'MoneyPrinterTurbo' / 'config.toml'))
AZURE_VOICES_JSON = str(_p('AZURE_VOICES_JSON',
                           ROOT / 'MoneyPrinterTurbo-main' / 'app' / 'services'
                           / 'data' / 'azure_voices.json'))


def add_cosyvoice_to_path():
    """把 CosyVoice 及其 Matcha-TTS 子模块挂进 sys.path。

    必须在 `from cosyvoice...` 之前调用。CosyVoice 是独立仓库、自己带
    .git，不进本仓库，安装方式见 README。"""
    for p in (str(COSYVOICE_DIR), str(MATCHA_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
