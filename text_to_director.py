#!/usr/bin/env python3
"""text_to_director.py — 新闻原文 → LLM 提取核心文本 → director.yaml

用法:
  python text_to_director.py <项目目录> [--input 正文.txt] [--dur 30] [--out director.yaml]

流程:
  1. 读取项目目录下 正文内容*.txt（默认）或 --input 指定文件
  2. DeepSeek 提取核心内容，生成完整 director.yaml（含场景旁白/字幕/画面）
  3. 写出 director.yaml → 交给 video_make_ffmpeg.py 渲染（与手动编排完全同路）

与手写 director.yaml 的区别：旁白/字幕由 LLM 从原文提炼，而不是人工编写。
标致508 那种无原文的项目仍走原有 flow（手写或 optimize 编排）。
"""
import sys, os, glob, json, yaml, requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import llm as _llm         # 密钥不再硬编码，见 pipeline_config.py
# 素材目录清单只有一份实现，别在这里另抄一个 `图片/`/`素材/` 的拼接
# （见 video_make_ffmpeg.discover_manual_images 的说明）。
from video_make_ffmpeg import (IMG_EXTS as _IMG_SET,                   # noqa: E402
                               discover_manual_images,
                               discover_manual_videos)

LLM_API_URL, LLM_API_KEY, LLM_MODEL = _llm()

# 那边是 set，str.endswith 只吃 tuple
IMG_EXTS = tuple(_IMG_SET)


def find_source_text(work_dir, explicit=''):
    """定位原文：--input 指定 > 目录下 正文内容*.txt > 目录下任意 .txt"""
    if explicit and os.path.exists(explicit):
        return explicit
    if explicit:
        return explicit
    hits = glob.glob(os.path.join(work_dir, '正文内容*.txt')) + \
           glob.glob(os.path.join(work_dir, '*.txt'))
    if hits:
        return hits[0]
    return None


def build_prompt(title, article, materials, dur):
    n_scenes = max(4, round(dur / 5))
    total_chars = int(dur * 4.5)  # 中文语速约 4.5 字/秒
    per_scene = f"每场景旁白 15~30 字"
    lines = [
        f"目标片长: {dur} 秒（{n_scenes}~{n_scenes + 2} 个场景，全片总旁白约 {total_chars} 字，{per_scene}）",
        f"",
        f"## 新闻原文（第1段）",
        f"标题: {title}",
        f"",
        article,
        f"",
        f"## 项目可用素材",
    ]
    lines += materials or ['  （无本地素材，全部用 搜索:关键词 自动获取）']
    return '\n'.join(lines)


SYSTEM_PROMPT = """你是新闻短视频导演。给定一篇新闻原文，提取核心内容，设计一个短视频方案。

## 核心提取原则
- 保留新闻骨架：核心事实（谁/什么/为什么）、争议焦点、关键数据、最有力的引语
- 砍掉：背景铺垫的冗余细节、重复表述、过程性描写、政策条文罗列

## 场景设计
- 6~8 个场景，逻辑连贯：开头悬念钩子 → 事实展开 → 争议/反差 → 收尾观点
- 每场景字段: id, narration(字幕), spoken(TTS 念), mood, speed, visuals(type, source)
- mood 可选: 悬念/严肃/平实/兴奋/高潮/收尾
- speed 可选: 快/中/慢

## narration 与 spoken 必须逐字相同（最重要）
- **narration（字幕显示）和 spoken（TTS 朗读）必须是同一份文本，逐字完全相同**，禁止写两个版本
- 数字一律用阿拉伯数字书写（1.8T、7月16日、211匹、16.37万）——管线会自动把数字转成中文读音（一点八T、七月十六日、二百一十一匹），字幕仍显示阿拉伯数字
- 文本保持口语播报风格 + 短句：每句 15~25 字，逗号/句号自然断句（字幕两行内读完）
- 反例（错误）：narration"日本史上曾有八位女天皇" + spoken"历史中，日本曾经出现过八位女性天皇"——两版文本
- 正例（正确）：narration = spoken = "然而，却在首位女首相执政时期，修法堵死了女性天皇之路。"

## 画面（visuals）
- type: image — 用项目 图片/ 目录里的文件名，或自动搜索照片 source: "搜索:关键词"
- type: video — 空镜来源二选一：
  ① source 写 "素材/文件名" 显式引用本地素材（素材清单里列出的成品片段务必用上，安排到"效果展示"场景）；
  ② source 写 "搜索:英文关键词" 自动搜索（Pexels 只支持英文搜索，中文词匹配不到会返回热门视频）。
  关键词 2~4 个英文词、具体可拍摄（如 "搜索:train interior"、"搜索:city night aerial"、
  "搜索:people subway phone"）；禁止抽象词（news、hot、rules、debate 这类匹配不到素材）
- type: text — 强调卡片（关键数字/金句），旁白照常；**每 8 个场景最多 1 个**，
  且必须是真正需要大字强调的内容（关键数字/结论金句），普通叙事一律用 video/image 配 搜索: 素材
- 自动搜索失败时管线会自动降级，照常写 搜索: 关键词

## 输出
只输出完整 YAML，不要任何解释文字：
project: {title: "...", duration: N, type: "新闻"}
voice: {spk_id: "故事女声", express: true, speed: "中"}
optimize: false
scenes: [...]
bgm: {path: ""}
output: {subtitle: true, size: [1080, 1920]}   # size 指定输出尺寸，横屏 16:9 写 [1920, 1080]"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    work_dir = sys.argv[1]
    src_path = find_source_text(work_dir)
    if not src_path:
        print(f"❌ 未找到原文：{work_dir}/正文内容*.txt，可用 --input 指定")
        sys.exit(1)

    out_path = 'director.yaml'
    if '--out' in sys.argv:
        out_path = sys.argv[sys.argv.index('--out') + 1]
    dur = 30
    if '--dur' in sys.argv:
        dur = int(sys.argv[sys.argv.index('--dur') + 1])

    with open(src_path, encoding='utf-8') as f:
        article = f.read().strip()
    title = os.path.basename(work_dir)

    # 素材清单：本地图片/视频 → 提示 LLM 优先使用。
    # 目录扫描走 video_make_ffmpeg 的那一份（director_agent.py 也用同一份），
    # 本文件已无调用方，但不能在目录约定上跟别人分叉 —— 分叉了也看不出来，
    # 表现只是「这张图从没出现在片子里」。
    materials = []
    imgs = discover_manual_images(work_dir)
    imgs += [f for f in sorted(os.listdir(work_dir))
             if f.lower().endswith(IMG_EXTS) and not f.startswith('.')]
    if imgs:
        materials.append(f"  本地图片（source 可直接写 素材/文件名 或根目录文件名）: {', '.join(imgs)}")
    vids = [os.path.relpath(p, work_dir) for p in discover_manual_videos(work_dir)]
    if vids:
        materials.append(f"  本地视频（可能是已生成成品的展示片段，video 场景 source 写 路径/文件名 显式引用并安排到合适的展示场景；也可留空自动分配）: {', '.join(vids)}")

    print(f"📰 原文: {src_path} ({len(article)} 字)")
    print(f"🧠 DeepSeek 提取核心文本并编排...")
    prompt = build_prompt(title, article, materials, dur)
    try:
        resp = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8192,
            # 不传 temperature：deepseek 默认可用；k3 只允许 temperature=1（传其他值 400）
            "reasoning_effort": "low",
        }, headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=180)
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ LLM 调用失败: {e}")
        sys.exit(1)

    # 提取 YAML
    if '```yaml' in raw:
        content = raw.split('```yaml', 1)[-1].rsplit('```', 1)[0]
    elif '```' in raw:
        content = raw.split('```', 1)[-1].rsplit('```', 1)[0]
    else:
        content = raw
    content = content.strip()

    # 校验
    try:
        parsed = yaml.safe_load(content)
        scenes = parsed.get('scenes', [])
        if not isinstance(parsed, dict) or 'project' not in parsed:
            raise ValueError('缺 project 字段')
        if not scenes:
            raise ValueError('scenes 为空')
        if 'voice' not in parsed:
            parsed['voice'] = {'spk_id': '故事女声', 'express': True, 'speed': '中'}
        parsed.setdefault('optimize', False)
        parsed.setdefault('bgm', {'path': ''})
        parsed.setdefault('output', {'subtitle': True, 'size': [1080, 1920]})
        if parsed.get('project') is None:
            parsed['project'] = {'title': title, 'duration': dur, 'type': '新闻'}
        parsed['project']['duration'] = dur
        content = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
    except Exception as e:
        print(f"❌ LLM 输出不是合法 YAML: {e}")
        print(content[:500])
        sys.exit(1)

    out_full = os.path.join(work_dir, out_path)
    with open(out_full, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✅ 导演方案 → {out_full}")

    total = sum(len(sc.get('narration', '')) for sc in scenes)
    print(f"   场景数: {len(scenes)}，总旁白 {total} 字，预计 {total / 4.5:.0f}s")
    for i, sc in enumerate(scenes, 1):
        vsrc = (sc.get('visuals') or {}).get('source', '')
        print(f"   [{i:02d}] {sc.get('mood','平实')} | {sc.get('narration','')[:34]} | 画面: {vsrc[:30]}")
    return out_full


if __name__ == '__main__':
    main()
