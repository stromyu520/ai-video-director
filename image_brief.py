#!/usr/bin/env python3
"""image_brief.py — 写稿**之前**先把项目里的图片读懂，再去交给 LLM 排版。

和 video_make_ffmpeg 里那段 VLM 是**两件事**，别合：

  这里（写稿前）：这张图**是什么** —— 画面、上面的字、适合讲什么。
                  产出喂给 LLM 当素材清单，它才知道哪张图该配哪段旁白、
                  几段之间怎么排。
  那里（渲染时）：这句词**配这张图合不合适** —— 一个事后校验，
                  不匹配就往旁白里补一句视觉关键信息。

为什么必须前置：校验模式的提示词是「解说词参考：……」开头，答案依赖
**已经写好的那句词**。拿它去指导写稿，等于先看答案再出题。真实教训 ——
标致 508L 项目里 director.yaml 写着 source: 图片/主视觉.jpg 与 图片/外观.png，
**两个文件都不存在**（实际只有 前脸/侧面/内饰.jpg）。原来的清单只把文件名
念给 LLM 听，模型只能从名字猜内容，猜错了也没人拦得住。

成本：一张图约 10~20s（模型加载 11.5s 只付一次）。所以**结果落盘缓存**，
按 (mtime, size) 判断新鲜度，重跑同一项目不再付费。缓存文件名刻意用 .json ——
`discover_images()` 只认图片扩展名，不会被当成素材卷进去。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import project_paths as P                                      # noqa: E402
from pipeline_config import vlm as _vlm_cfg                    # noqa: E402
# 闸门、扩展名与目录清单从渲染模块借，不另抄一份：两处判断必须永远一致，
# 否则「写稿时算进去了、渲染时又不认」这种偏差只会以「图没用上」的形式出现。
from video_make_ffmpeg import (MANUAL_DIRS,                    # noqa: E402
                               discover_manual_images, is_manual_image)

# 必须在 vlm-env 里跑：video-env 的 transformers 是 4.51.3，加载不了 Qwen3.5
# 的权重（连类都 import 不到）。见 vlm_analyze.py 顶部的说明。
VLM_PYTHON = P.VLM_PYTHON
VLM_SCRIPT = os.path.join(ROOT, 'vlm_analyze.py')

CACHE_NAME = '图片理解.json'


def manual_images(work_dir):
    """项目里用户素材目录（`素材/`、`图片/`）下的图片，按 rel 排序，返回 [(abs, rel)]。

    目录清单与闸门都走 `video_make_ffmpeg` 那一份，不在这里另立一套 ——
    「写稿时算进去了、渲染时又不认」这种偏差只会以「图没用上」的形式出现，
    不报错、不降级、什么都没少，只是画面和字幕对不上。
    """
    return [(os.path.join(work_dir, rel), rel)
            for rel in discover_manual_images(work_dir)
            if is_manual_image(rel)]


def _cache_path(work_dir):
    return os.path.join(work_dir, CACHE_NAME)


def _load_cache(work_dir):
    p = _cache_path(work_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        # 缓存坏了不该让写稿失败 —— 当没有就是了（大不了重跑一次 VLM）
        return {}


def _save_cache(work_dir, data):
    try:
        with open(_cache_path(work_dir), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f'   ⚠️ 图片理解缓存写不进去（不影响本次）：{e}')


def _fresh(entry, abs_path):
    """缓存还算数吗 —— mtime + size 任一变了就重跑（图被换掉了）。"""
    if not isinstance(entry, dict):
        return False
    try:
        st = os.stat(abs_path)
    except OSError:
        return False
    return (entry.get('mtime') == int(st.st_mtime)
            and entry.get('size') == st.st_size
            and isinstance(entry.get('r'), dict))


def describe_one(abs_path, timeout=180):
    """跑一次理解模式，返回 dict；失败返回 {'error': ...}。"""
    try:
        r = subprocess.run(
            [VLM_PYTHON, VLM_SCRIPT, abs_path, '--describe'],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': f'超时（>{timeout}s）'}
    if r.returncode != 0:
        return {'error': (r.stderr or '').strip().splitlines()[-1:] and
                (r.stderr or '').strip().splitlines()[-1] or '未知错误'}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {'error': f'输出不是 JSON: {(r.stdout or "")[:80]}'}


def describe_project(work_dir, *, force=False, timeout=None, on_event=None):
    """把 `manual_images()` 那些图都理解一遍，返回 [{rel, abs, r}]（含 error 表示这张失败）。

    on_event(text) 用来把进度打到调用方的日志里 —— 一张图十几秒，
    没有进度输出的话用户会以为卡死了。
    """
    cfg = _vlm_cfg()
    if not cfg['enabled']:
        return []
    timeout = timeout or cfg['timeout']
    imgs = manual_images(work_dir)
    if not imgs:
        return []

    cache = _load_cache(work_dir)
    out, dirty = [], False
    for i, (abs_p, rel) in enumerate(imgs, 1):
        entry = cache.get(rel)
        if not force and _fresh(entry, abs_p):
            if on_event:
                on_event(f'   👁️ 图片理解（缓存命中）{rel}')
            out.append({'rel': rel, 'abs': abs_p, 'r': entry['r']})
            continue
        if on_event:
            on_event(f'   👁️ 图片理解 {i}/{len(imgs)}：{rel}（约十几秒）')
        r = describe_one(abs_p, timeout=timeout)
        if 'error' in r:
            if on_event:
                on_event(f'      ⚠️ {rel} 没读懂：{r["error"]}')
        else:
            st = os.stat(abs_p)
            cache[rel] = {'mtime': int(st.st_mtime), 'size': st.st_size, 'r': r}
            dirty = True
        out.append({'rel': rel, 'abs': abs_p, 'r': r})

    if dirty:
        _save_cache(work_dir, cache)
    return out


def inventory_lines(briefs):
    """把理解结果排成给 LLM 看的清单。

    每张图一行，带上「图里有什么」与「图上的字」。字要**原样给**：
    规格图文卡上印的就是配置参数，旁白该讲的就是它，让模型自己概括等于
    丢掉唯一可信的信息源（而且概括错了没人能发现）。
    """
    if not briefs:
        return []
    lines = ['  本地图片（source 必须从下面这些里选，文件名要一字不差）：']
    for b in briefs:
        r = b['r']
        if 'error' in r:
            # 读不懂也要列出来 —— 图是真的、能用，只是内容未知
            lines.append(f'    · {b["rel"]}（没读懂内容，按文件名和上下文安排）')
            continue
        bits = []
        if r.get('visual_key'):
            bits.append(f'画面：{r["visual_key"]}')
        if r.get('kind'):
            bits.append(f'类型：{r["kind"]}')
        if r.get('suits'):
            bits.append(f'适合讲：{r["suits"]}')
        if r.get('has_text') and r.get('text_content'):
            txt = str(r['text_content']).replace('\n', ' ').strip()
            # 太长的正文截断会丢配置项，但全给又会顶爆提示词预算 —— 240 字够放
            # 五六条配置项，超出部分对"该配哪段旁白"这个判断也没增益了。
            if len(txt) > 240:
                txt = txt[:240] + '…'
            bits.append(f'图中文字：{txt}')
        lines.append(f'    · {b["rel"]} —— ' + ' | '.join(bits))
    return lines


if __name__ == '__main__':                       # 自查用
    wd = sys.argv[1] if len(sys.argv) > 1 else '.'
    bs = describe_project(wd, force='--force' in sys.argv,
                          on_event=lambda s: print(s, flush=True))
    print()
    for ln in inventory_lines(bs):
        print(ln)
