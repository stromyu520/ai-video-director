#!/usr/bin/env python3
"""check_materials.py — 分析报告出来后，问大模型缺什么素材

用法:
  python check_materials.py <工作目录>

流程:
  1. 读取 analysis.json
  2. 问 DeepSeek 评估需要什么素材
  3. 输出建议 → 你补充 → 再跑 plan_remix.py
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import llm as _llm         # 密钥不再硬编码，见 pipeline_config.py
# 与渲染/编排共用同一份素材目录与扩展名清单，理由见 video_make_ffmpeg.MANUAL_DIRS
from video_make_ffmpeg import IMG_EXTS, VIDEO_EXTS                 # noqa: E402
from pipeline_config import MANUAL_DIRS                            # noqa: E402

LLM_API_URL, LLM_API_KEY, LLM_MODEL = _llm()


def check(work_dir):
    analysis_path = os.path.join(work_dir, 'analysis.json')
    if not os.path.exists(analysis_path):
        print(f"❌ 未找到分析报告: {analysis_path}")
        print(f"   先运行: python analyze_video.py <视频> {work_dir}")
        sys.exit(1)

    with open(analysis_path, encoding='utf-8') as f:
        analysis = json.load(f)

    # 扫描已有素材（目录与扩展名走共用清单 —— 写窄了就会建议你补一份
    # 你其实已经传过的素材）
    existing = []
    for subdir in MANUAL_DIRS:
        d = os.path.join(work_dir, subdir)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(tuple(IMG_EXTS) + tuple(VIDEO_EXTS)):
                    existing.append(f"{subdir}/{fn}")

    # 构建评估提示
    scene_summary = []
    for sc in analysis.get('scenes', []):
        vk = sc.get('vlm', {}).get('visual_key', '')
        tc = sc.get('vlm', {}).get('text_content', '')
        parts = [f"  [{sc['start']:.1f}s-{sc['end']:.1f}s]({sc['duration']:.1f}s)"]
        if vk: parts.append(f"画面:{vk}")
        if tc: parts.append(f"文字:{tc[:40]}")
        scene_summary.append(' | '.join(parts))
        if sc.get('narration'):
            scene_summary.append(f"    解说:{sc['narration'][:100]}")

    transcript = '\n'.join(
        f"  [{s['start']:.1f}s-{s['end']:.1f}s] {s['text']}"
        for s in analysis.get('transcript', [])
    )

    prompt = f"""你是一个短视频二创的素材评估专家。

## 原视频信息
- 文件: {os.path.basename(analysis['source'])}
- 时长: {analysis['duration']:.1f}s
- 镜头数: {len(analysis['scenes'])}

## 当前已有素材
{'  '.join(existing) if existing else '（无）'}

## 视频转录
{transcript[:800]}

## 镜头分析
{chr(10).join(scene_summary[:15])}

## 任务

分析这个视频要做成二创短视频，需要什么额外素材？

请按以下格式输出：

### 素材评估结果
[需要补充素材 / 无需额外素材]

### 需要的素材清单（如果需要）
列出具体需要的素材类型和用途：
1. 图片类：什么内容？用在哪里？
2. 短片类：什么内容？用在哪里？
3. BGM：什么风格？
4. 其他建议

### 二创思路简述
打算怎么做这个二创？（1-2句话）
"""

    print(f"\n🧠 正在评估素材需求...")
    try:
        import requests
        resp = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是二创视频的素材评估专家。分析视频后判断需要什么素材。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 2048,
            "thinking": {"type": "disabled"},
        }, headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=120)

        result = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"\n{'='*50}")
        print(f"📋 素材评估报告")
        print(f"{'='*50}")
        print(result)
        print(f"{'='*50}")

        # 判断是否需要素材
        needs = '无需' not in result.split('素材评估结果')[1].split('\n')[0] if '素材评估结果' in result else True
        if '无需额外素材' in result or '不需要' in result[:50]:
            print(f"\n✅ 素材充足，可以继续编排")
            print(f"   → 运行: python plan_remix.py {work_dir}")
        else:
            print(f"\n📌 请补充以上素材后重新运行评估")
            print(f"   素材放 {work_dir}/素材/ 目录下（图片和短视频都放这里）")
            print(f"   然后运行: python plan_remix.py {work_dir}")

    except Exception as e:
        print(f"❌ 评估失败: {e}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    check(sys.argv[1])
