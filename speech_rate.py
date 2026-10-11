#!/usr/bin/env python3
"""speech_rate.py — 「多少字 = 多少秒」的唯一真相来源

为什么需要它：**片长不是由 project.duration 决定的，是由旁白念多久决定的。**
LLM 写多少字 → TTS 念多久 → 成片多长。所以「目标 30 秒」这个需求，只能翻译成
「旁白约 N 字」，而这个换算率必须按音色实测，不能拍脑袋。

实测（本项目已出片的目录，旁白字数 ÷ 成片**实际**秒数）：
    CosyVoice「故事女声」        4.47 ~ 4.78 字/秒（4 条片，很稳）
    Edge「zh-CN-XiaoxiaoNeural」 4.06 字/秒
同一个字数预算换音色就差 ~15%，所以按音色分别记，不共用一个常数。

历史坑（别再犯）：老代码把这个比值算成「字数 ÷ **声明**时长」
（168/30 = 5.6、235/30 = 7.83），据此得出 4.87~7.83 并设置校验护栏 3.5~9.0。
**分母用错了** —— 声明 30s 的片实际出了 52.6s，而 52.6 那个数根本没进计算。
结果是这个 ±2.5 倍的护栏什么都拦不住，片子比要求长 75% 也照样放行。

存的位置：web/cache/speech_rate.json。每跑成一条片就自动记一次，
下次生成脚本时用最新值算字数预算 —— 越用越准。
"""
import json
import os
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(ROOT, 'web', 'cache', 'speech_rate.json')

# 还没攒够实测数据时的先验值（单位：字/秒），按后端分。
# 缺省给 4.5 —— 它是老代码沿用的值，作为「完全未知」的兜底还算合理。
PRIOR = {
    'cosyvoice': 4.6,     # 实测 4.47~4.78，取中位
    'edge': 4.1,          # 实测 4.06
    'openai': 4.5,        # 没实测过，先用通用值
}
DEFAULT_RATE = 4.5

# 样本太小不采信：一条 5 字的短片除以总时长会得出离谱的比率
MIN_CHARS = 60

_lock = threading.Lock()


def provider_of(voice):
    """音色 → 后端名。'edge:xxx' → 'edge'；'故事女声' → 'cosyvoice'。

    规则与 tts_backends.split_voice 一致：只认已知前缀，
    认不出就整体当本地音色名（免得本地音色名带个冒号就被判成线上）。
    """
    from tts_backends import PROVIDERS
    if isinstance(voice, str) and ':' in voice:
        head = voice.split(':', 1)[0]
        if head in PROVIDERS:
            return head
    return 'cosyvoice'


def _read():
    try:
        with open(STORE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _write(d):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STORE)          # 原子替换：并发读不会看到半截文件


def rate_for(voice):
    """这个音色的实测字/秒。没实测过就回落后端先验。"""
    rec = (_read().get('voices') or {}).get(str(voice)) or {}
    if rec.get('chars', 0) >= MIN_CHARS and rec.get('secs', 0) > 0:
        return float(rec['rate'])
    return PRIOR.get(provider_of(voice), DEFAULT_RATE)


def chars_for(voice, dur):
    """要出 `dur` 秒，旁白该写多少字。提示词里给 LLM 的就是这个数。"""
    dur = float(dur or 0)
    if dur <= 0:
        return 0
    return max(1, int(round(dur * rate_for(voice))))


def dur_for(voice, chars):
    """这些字大概会念多少秒（页面上做预估用）。"""
    return round(int(chars or 0) / rate_for(voice), 1)


def record(voice, chars, secs):
    """跑完一条片，把实测结果记下来。

    按**字数加权**累加，而不是简单平均：一条 20 字的短片和一条 300 字的
    长片不该有同等话语权，否则刚跑的小样会把已经稳定的长片结论冲掉。
    """
    voice = str(voice or '')
    chars, secs = int(chars or 0), float(secs or 0)
    if not voice or chars < MIN_CHARS or secs <= 0:
        return None
    rate = chars / secs
    # 离谱值直接丢：正常中文口播不可能 1 字/秒 或 20 字/秒
    if not (2.0 <= rate <= 12.0):
        return None
    with _lock:
        d = _read()
        voices = d.setdefault('voices', {})
        old = voices.get(voice) or {}
        oc, os_ = int(old.get('chars') or 0), float(old.get('secs') or 0)
        nc, ns = oc + chars, os_ + secs
        voices[voice] = {
            'rate': round(nc / ns, 3),
            'chars': nc,
            'secs': round(ns, 1),
            'samples': int(old.get('samples') or 0) + 1,
            'last': round(rate, 3),          # 最近一次，便于发现音色行为漂移
        }
        d['updated'] = __import__('time').strftime('%Y-%m-%d %H:%M:%S')
        _write(d)
    return dict(voices[voice], voice=voice)
