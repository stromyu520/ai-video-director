#!/usr/bin/env python3
"""tts_backends.py — 线上 TTS 后端：音色名前缀分发

**为什么是「音色名前缀」而不是「改管线」**：整条渲染链上，「用哪个 TTS」这件事
只体现为一个字符串 —— `spk_id`。video_make_ffmpeg.synth_scene 把它原样透传给
`tts_express.py --spk-id <值>`（连字符！`--spk_id` 是 argparse 错误），
_batch_tts 的 JSON 里也带它，render_remix 走 REMIX_SPK 环境变量。沿途没人校验它。
所以只要约定「带已知前缀的名字 = 线上」，本地/线上就是一个字符串的差别，
video_make_ffmpeg.py / render_remix.py 一行都不用改。

约定沿用 MoneyPrinterTurbo 的 `provider:payload` 风格（那边是 `is_X_voice()`）：

    故事女声                  → 本地 CosyVoice（无前缀 = 本地）
    edge:zh-CN-XiaoxiaoNeural → Edge TTS（免费、免 key）
    openai:alloy              → POST {base_url}/audio/speech，兼容任何同协议服务

**判别规则**：按第一个冒号切开，左边是已知 provider 才算线上。这样本地音色名里
就算将来带冒号也不会被误判成线上。

本模块**绝不 import torch**：服务器进程会 import 它（为了 /api/voices），
而本机 15GB 内存是满的。edge_tts 也只在函数内延迟导入。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import project_paths as P
CONFIG_PATH = ROOT / 'config.toml'

# 已知 provider → 是否线上。本地不在此表：没有前缀就是本地。
PROVIDERS = {
    'edge': '线上 Edge TTS（免费，无需 key）',
    'openai': 'OpenAI 兼容接口（/v1/audio/speech）',
}

# 线上合成的中间产物统一转成这个规格的 WAV。
# 24k 是对齐 CosyVoice（Fun-CosyVoice3）的输出，下游 xfade_join 里
# 还会再 aresample=44100，所以这里差一点也不会坏，但对齐了最省事。
WAV_RATE = 24000

# OpenAI 兼容接口内置音色（它没有列表 API，只能写死）
OPENAI_VOICES = [
    ('alloy', '中性 · 百搭'), ('echo', '男声 · 沉稳'),
    ('fable', '中性 · 叙事'), ('onyx', '男声 · 低沉'),
    ('nova', '女声 · 明亮'), ('shimmer', '女声 · 柔和'),
]

# edge_tts.list_voices() 走网络失败时的兜底：MoneyPrinterTurbo 的静态表。
# 注意目录是 MoneyPrinterTurbo-main —— 不带 -main 的路径在这台机器上不存在。
AZURE_VOICES_JSON = Path(P.AZURE_VOICES_JSON)

DEFAULT_EDGE_VOICE = 'zh-CN-XiaoxiaoNeural'


# ---------------------------------------------------------------------------
# 音色名解析
# ---------------------------------------------------------------------------

def split_voice(voice):
    """'edge:xxx' → ('edge', 'xxx')；'故事女声' → ('cosyvoice', '')。

    只认已知前缀：`A:B` 里 A 不在 PROVIDERS 里就整体当本地音色名，
    免得将来本地音色名带个冒号就被判成线上。
    """
    s = (voice or '').strip()
    if ':' in s:
        head, tail = s.split(':', 1)
        if head.strip().lower() in PROVIDERS:
            return head.strip().lower(), tail.strip()
    return 'cosyvoice', ''


def is_online_voice(voice):
    return split_voice(voice)[0] != 'cosyvoice'


def provider_label(voice):
    return PROVIDERS.get(split_voice(voice)[0], '本地 CosyVoice')


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _load_tts_cfg():
    """读 config.toml 的 [tts] 段。读失败当空 —— 缺配置要走各自的降级路径，
    不该让合成直接崩。**每次现读**：服务器改完配置不能要求重启。"""
    try:
        import tomllib
    except ModuleNotFoundError:                     # 系统 python3 是 3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:                 # pragma: no cover
            return {}
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
    except Exception:                               # noqa: BLE001
        return {}
    tts = data.get('tts')
    return tts if isinstance(tts, dict) else {}


def backend_cfg(provider):
    """[tts.backends.<provider>] 段 + 少量默认值。"""
    backends = _load_tts_cfg().get('backends') or {}
    cfg = backends.get(provider) or {}
    return dict(cfg) if isinstance(cfg, dict) else {}


def default_voice(provider):
    """页面上选中某个后端时默认挑哪个音色（返回带前缀的完整 id）。"""
    v = str(backend_cfg(provider).get('voice') or '').strip()
    if not v:
        return f'edge:{DEFAULT_EDGE_VOICE}' if provider == 'edge' else ''
    # 配置里可能已经写了前缀，也可能只写了裸音色名，两种都认
    return v if is_online_voice(v) else f'{provider}:{v}'


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------

def _edge_rate(speed):
    """'快'/'慢' → edge 的 ±百分比。其余一律不改语速。"""
    return {'快': '+20%', '慢': '-20%'}.get((speed or '').strip(), None)


def _quiet_logging():
    """edge_tts 的依赖会调 basicConfig(DEBUG)，把 `Using selector: EpollSelector`
    打到 stdout 上。管线的 stdout 是要给人看的进度流，这些噪音会混进去。"""
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    for name in ('aiohttp', 'asyncio', 'edge_tts'):
        logging.getLogger(name).setLevel(logging.WARNING)


def _ensure_parent(path):
    """edge_tts.save / open() 都不会替你建目录，而它们的报错是裸 errno 2。"""
    d = os.path.dirname(str(path))
    if d:
        os.makedirs(d, exist_ok=True)


def _to_wav(src, out_path):
    """任何音频 → 16bit PCM WAV。按内容嗅探格式，所以扩展名骗不了它。"""
    out_path = str(out_path)
    _ensure_parent(out_path)
    r = subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-ar', str(WAV_RATE), '-ac', '1',
         '-c:a', 'pcm_s16le', out_path],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        tail = ((r.stderr or '').strip().splitlines() or [''])[-1]
        raise RuntimeError(f'音频转 WAV 失败：{tail}')


def _synth_edge(payload, text, out_path, speed=None):
    import asyncio

    import edge_tts
    _quiet_logging()
    voice = payload or DEFAULT_EDGE_VOICE
    kwargs = {}
    rate = _edge_rate(speed)
    if rate:
        kwargs['rate'] = rate
    mp3 = str(out_path) + '.edge.mp3'
    _ensure_parent(mp3)
    try:
        asyncio.run(edge_tts.Communicate(text, voice, **kwargs).save(mp3))
        if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
            raise RuntimeError('edge-tts 没有写出音频（音色名或网络有问题？）')
        _to_wav(mp3, out_path)
    finally:
        if os.path.exists(mp3):
            os.remove(mp3)


def _synth_openai(payload, text, out_path, speed=None):
    import requests

    cfg = backend_cfg('openai')
    base = str(cfg.get('base_url') or '').strip().rstrip('/')
    key = str(cfg.get('api_key') or '').strip()
    # 这套后端已从页面撤掉（本机没有这样的服务），所以提示指向 config.toml 而不是
    # 页面上的某个框 —— 照旧说「设置 → 配音」会让人在弹窗里找一个不存在的输入框。
    if not base:
        raise RuntimeError('未配置 OpenAI 兼容 TTS 的 base_url'
                           '（改 config.toml 的 [tts.backends.openai]，页面上不再提供）')
    if not key:
        raise RuntimeError('未配置 OpenAI 兼容 TTS 的 api_key'
                           '（改 config.toml 的 [tts.backends.openai]，页面上不再提供）')
    # 用户可能给 https://host/v1，也可能直接给到 /audio/speech，两种都认
    endpoint = base if base.endswith('/audio/speech') else base + '/audio/speech'
    body = {'model': str(cfg.get('model') or 'tts-1'),
            'input': text,
            'voice': payload or 'alloy',
            'response_format': 'wav'}
    rate = {'快': 1.2, '慢': 0.85}.get((speed or '').strip())
    if rate:
        body['speed'] = rate
    r = requests.post(endpoint, json=body,
                      headers={'Authorization': f'Bearer {key}'},
                      timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code}: {(r.text or "")[:200]}')
    raw = str(out_path) + '.openai.raw'
    _ensure_parent(raw)
    with open(raw, 'wb') as f:
        f.write(r.content)
    try:
        _to_wav(raw, out_path)      # 有的服务忽略 response_format，一律过一遍
    finally:
        if os.path.exists(raw):
            os.remove(raw)


_DISPATCH = {'edge': _synth_edge, 'openai': _synth_openai}


def synthesize(voice, text, out_path, *, speed=None, express=False):
    """线上合成一段旁白到 out_path（WAV）。返回 {ok, secs, error}。

    express 在线上是**故意忽略**的：那是 CosyVoice 的表现力标记
    （LLM 插呼吸/情绪标记再合成），edge/openai 都吃不了这些标记文本，
    硬送过去会把标记原样念出来。所以线上直接合成原文。
    """
    provider, payload = split_voice(voice)
    fn = _DISPATCH.get(provider)
    if fn is None:
        return {'out': str(out_path), 'ok': False,
                'error': f'未知线上 TTS: {provider}'}
    try:
        fn(payload, text, out_path, speed)
        secs = _probe_secs(out_path)
        return {'out': str(out_path), 'ok': True, 'secs': round(secs, 2)}
    except Exception as e:                                  # noqa: BLE001
        return {'out': str(out_path), 'ok': False, 'error': str(e)}


def _probe_secs(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 命令行入口（给 tts_express.py 的线上分支用）
#
# 打印风格与 tts_express 本地路径保持一致，并且带上 PHASES_* 标记表认得的 emoji，
# 免得线上跑起来时页面进度条不动。
# ---------------------------------------------------------------------------

def cli_online(voice, text, out_path, speed=None):
    """单段线上合成。成功返回 True，失败打中文原因 + sys.exit(1)。"""
    import time
    provider, payload = split_voice(voice)
    print(f'🎤 线上合成（{provider}）：{payload or "（默认音色）"}')
    print(f'   文本 {len(text)} 字 → {out_path}', flush=True)
    t0 = time.time()
    r = synthesize(voice, text, out_path, speed=speed)
    if not r['ok']:
        print(f'❌ 线上合成失败: {r["error"]}', file=sys.stderr, flush=True)
        sys.exit(1)
    print(f'        ✅ {r["secs"]:.1f}s → {os.path.basename(str(out_path))}'
          f'（用时 {time.time() - t0:.1f}s）', flush=True)
    return True


def cli_online_batch(jobs):
    """--batch 的线上分支。出参契约与本地 run_batch 逐字相同：
    末行 `__BATCH_RESULT__[{out, ok, secs, error}, ...]`。"""
    import time
    print(f'🎤 批量合成：{len(jobs)} 段旁白（线上，不加载本地模型）', flush=True)
    t0 = time.time()
    results = []
    for i, j in enumerate(jobs, 1):
        out = j['out']
        print(f'   [{i}/{len(jobs)}] {str(j.get("text", ""))[:28]}...', flush=True)
        r = synthesize(j.get('spk_id'), j.get('text', ''), out,
                       speed=j.get('speed'))
        if r['ok']:
            print(f'        ✅ {r["secs"]:.1f}s → {os.path.basename(out)}',
                  flush=True)
        else:
            print(f'        ❌ 失败: {r["error"]}', flush=True)
        results.append(r)
    print(f'   批量完成：{sum(1 for r in results if r["ok"])}/{len(jobs)} 段，'
          f'用时 {time.time() - t0:.1f}s', flush=True)
    print('__BATCH_RESULT__' + json.dumps(results, ensure_ascii=False))
    return results


# ---------------------------------------------------------------------------
# 音色清单（给页面下拉用）
# ---------------------------------------------------------------------------

def _edge_voices_from_network(timeout=20):
    import asyncio

    import edge_tts
    _quiet_logging()

    async def go():
        return await asyncio.wait_for(edge_tts.list_voices(), timeout=timeout)

    voices = asyncio.run(go())
    out = []
    for v in voices or []:
        short = v.get('ShortName') or ''
        if not short:
            continue
        out.append({
            'id': f'edge:{short}',
            'name': short,
            'locale': v.get('Locale') or '',
            'gender': v.get('Gender') or '',
            'label': (v.get('FriendlyName') or short)
                     .replace('Microsoft ', '').replace(' Online', ''),
        })
    return out


def _edge_voices_from_static():
    """网络不可达时的兜底：MoneyPrinterTurbo 的静态 azure_voices.json。"""
    if not AZURE_VOICES_JSON.exists():
        return []
    try:
        data = json.loads(AZURE_VOICES_JSON.read_text(encoding='utf-8'))
    except Exception:                                       # noqa: BLE001
        return []
    out = []
    for v in data if isinstance(data, list) else []:
        if not isinstance(v, dict):
            continue
        short = v.get('ShortName') or v.get('short_name') or ''
        if not short:
            continue
        out.append({
            'id': f'edge:{short}',
            'name': short,
            'locale': v.get('Locale') or v.get('locale') or '',
            'gender': v.get('Gender') or v.get('gender') or '',
            'label': (v.get('FriendlyName') or short)
                     .replace('Microsoft ', '').replace(' Online', ''),
        })
    return out


def online_voices(provider=None, *, prefer_network=True):
    """线上音色清单。返回 [{id, name, locale, gender, label, provider}]。

    provider=None 时把所有启用的线上后端都列出来。
    """
    names = [provider] if provider else list(PROVIDERS)
    out = []
    for name in names:
        if name == 'edge':
            try:
                items = _edge_voices_from_network() if prefer_network else []
            except Exception:                               # noqa: BLE001
                items = []
            if not items:
                items = _edge_voices_from_static()
        elif name == 'openai':
            items = [{'id': f'openai:{vid}', 'name': vid, 'locale': '',
                      'gender': '', 'label': label}
                     for vid, label in OPENAI_VOICES]
        else:
            items = []
        for it in items:
            it['provider'] = name
        out += items
    return out


def local_voices():
    """本地 CosyVoice 音色名。

    必须在**子进程**里读：spk2info.pt 是 torch.save 出来的，
    反序列化要 torch，而服务器进程绝不能 import torch（内存已满）。
    这里只加载那个 59KB 的小文件，不建模型，几秒就退。
    """
    code = (
        'import json,torch,sys;'
        f'pt=torch.load(r"{ROOT}/models/Fun-CosyVoice3-0-5B-2512/spk2info.pt",'
        'map_location="cpu",weights_only=True);'
        'print(json.dumps([str(k) for k in pt.keys()]))'
    )
    # 解释器必须是 video-env（torch 在那儿）。sys.executable 在服务器进程里
    # 虽然也是它，但这个模块是库，被别的 python 调用时得仍然能跑。
    py = ROOT / 'video-env' / 'bin' / 'python'
    try:
        r = subprocess.run(
            [str(py if py.exists() else sys.executable), '-c', code],
            capture_output=True, text=True, timeout=180,
            cwd=str(ROOT))
        if r.returncode != 0:
            return []
        line = (r.stdout or '').strip().splitlines()[-1:] or ['']
        names = json.loads(line[0])
        return [n for n in names if isinstance(n, str)]
    except Exception:                                       # noqa: BLE001
        return []


if __name__ == '__main__':              # 自查：python tts_backends.py
    print('本地音色  ', local_voices())
    for name in PROVIDERS:
        cfg = backend_cfg(name)
        print(f'{name:8s}', '已配置' if cfg else '未配置', dict(cfg))
    try:
        vs = online_voices('edge')
        print(f'edge 音色 {len(vs)} 个，例：',
              ', '.join(v['id'] for v in vs[:4]))
    except Exception as e:                                  # noqa: BLE001
        print('edge 音色列表失败:', e)
