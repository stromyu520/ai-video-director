#!/usr/bin/env python3
"""vlm_analyze.py — 本地视觉模型图片分析器（独立环境，供 video_make_ffmpeg.py 子进程调用）

**只在「手动上传的图片」上跑**（`图片/` 目录，见 video_make_ffmpeg.is_manual_image）。
一张图约 10s + 4.2GB 显存，别拿去喂自动抽的视频帧。

跑在 vlm-env 里（transformers 5.14.1，认 qwen3_5），**不是** video-env
（那边是 4.51.3，太老，加载不了）。也正因如此，调用方一律走子进程 ——
服务器进程绝不能 import torch，见 web/ 各模块的模块 docstring。

用法:
  vlm-env/bin/python vlm_analyze.py <图片路径> --narration "解说词" [--output-format json]

输出 (JSON):
{
  "visual_key":    "一句话画面关键信息",
  "match":         "yes|partial|no",       // 解说词是否匹配画面
  "match_reason":  "为什么匹配/不匹配",
  "suggest_subtitle": "修正后的字幕建议（如有）",
  "subject_pos":   "center|left|right|top|bottom|spread",  // 主体位置
  "composition_note": "构图建议（是否需要调整裁剪等）",
  "has_text":      true/false,             // 画面是否有重要文字
  "text_content":  "画面中的文字内容（如有）"
}
"""

import sys, os, json, argparse
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import vlm as _vlm_cfg          # 模型路径唯一来源


MAX_PIXELS = 768 * 768  # 超过此像素数则缩放（提速 3-5x，几乎不影响识别精度）

# 本地视觉模型路径。读取链：环境变量 VLM_MODEL_DIR > config.toml [vlm] model_dir
# > 默认值 —— 换模型/换机器只改配置，不用动代码。
MODEL_DIR = _vlm_cfg()['model_dir']


def _resize_for_vlm(image_path):
    """预缩放大图 → 临时 JPG，减少 VLM 视觉 token 数，加速推理"""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if w * h <= MAX_PIXELS:
        return image_path  # 图片够小，直接用原图

    # 按比例缩放到 MAX_PIXELS 以内
    ratio = (MAX_PIXELS / (w * h)) ** 0.5
    nw, nh = int(w * ratio), int(h * ratio)
    img = img.resize((nw, nh), Image.LANCZOS)

    tmp = image_path + ".vlm_tmp.jpg"
    img.save(tmp, quality=85)
    return tmp


# 「读懂」用的提示词 —— 和下面「校验」用的**不是一回事**。
#
# 校验模式（match）回答的是「这张图配这句词合不合适」，它是**事后**的、
# 而且答案依赖已经被写出来的那句词 —— 拿它去写稿，等于让模型先看答案再出题。
# 写稿前需要的是**纯理解**：这张图里到底有什么、上面印着什么字、适合讲什么。
# 尤其是「规格图文卡」这类素材，字（配置参数）才是正文该讲的东西，
# 而照片没有字，只能靠画面主体。`kind` 就是用来分开这两种情况的。
DESCRIBE_PROMPT = """仔细看这张图，输出 JSON（不要其他内容）：
{
  "visual_key": "一句话说清画面是什么（25字内）",
  "kind": "照片 或 配置图文卡 或 截图 或 图表 或 其他",
  "subjects": "画面主体与构图（15字内）",
  "has_text": true/false,
  "text_content": "画面里出现的文字，逐条照抄，用 | 分隔（没有就空字符串）",
  "suits": "这张图适合配什么内容的旁白（15字内）"
}

要点：
- 如果图上有文字，text_content 必须**逐字照抄**，不要概括、不要翻译、不要补全。
- suits 说的是「该图能撑起什么话题」，不是复述画面。
"""

MATCH_PROMPT = """分析这张图片，输出 JSON（不要其他内容）：
{{
  "visual_key": "一句话关键视觉信息（15字内）",
  "match": "yes 或 partial 或 no",
  "match_reason": "解说词与画面是否匹配？为什么？",
  "subject_pos": "主体在画面的位置：center|left|right|top|bottom|spread",
  "has_text": true/false,
  "text_content": "画面中的关键文字（没有就空字符串）"
}}

解说词参考：{narration}"""


def analyze(image_path, narration="", mode="match"):
    """Qwen3.5-2B 分析图片，返回结构化结果。

    mode='match'    —— 校验：这张图配这句解说词合不合适（要传 narration）
    mode='describe' —— 理解：这张图是什么、写着什么字、适合讲什么（写稿前用）
    """
    # 类名与权重必须配套：这台机器上的是 Qwen3.5-2B（config.json 里
    # architectures = Qwen3_5ForConditionalGeneration）。以前这里写的是
    # Qwen3VL* 那一套 —— 那是**另一个模型**的类，拿它加载 qwen3_5 的权重会
    # 报「Reinit due to size mismatch」（q_proj 形状 4096x2048 vs 2048x2048），
    # 因为 Qwen3.5 的注意力层和 Qwen3-VL 不是一回事。
    from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration
    import torch

    # 预缩放大图，加速推理
    input_path = _resize_for_vlm(image_path)

    processor = AutoProcessor.from_pretrained(MODEL_DIR)
    model = Qwen3_5ForConditionalGeneration.from_pretrained(
        MODEL_DIR,
        dtype=torch.bfloat16,
        device_map="auto",
        low_cpu_mem_usage=True,
    )

    if mode == 'describe':
        prompt = DESCRIBE_PROMPT
    else:
        prompt = MATCH_PROMPT.format(narration=narration[:200])

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "image": input_path},
            {"type": "text", "text": prompt},
        ]
    }]

    # enable_thinking=False：关掉思考段。**必须关**，实测开着时模型会先吐
    # `<think>\n\n</think>` 再吐 JSON，而这段思考要吃掉相当一部分 token 预算，
    # 256 个 token 常常在思考里就用光了 —— 结果是被截断的半截 JSON
    # （"visual_key": "车内尊贵 就到头了），解析必然失败。模板支持这个开关，
    # 见 Qwen3.5-2B/chat_template.jinja:149。
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    inputs = processor(text=[text], images=[input_path], padding=True, return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}

    # describe 模式要逐条照抄画面上的字（配置图文卡常有 5-7 条，一条二十几字），
    # 256 个 token 贴着上限跑 —— 一超就被截断，而截断的下场是 JSON 解析失败、
    # 静默降级成空字段。多给一点余量，代价只是最坏情况下多算几十个 token。
    with torch.no_grad():
        outputs = model.generate(
            **inputs, max_new_tokens=384 if mode == 'describe' else 256)

    result = processor.decode(outputs[0], skip_special_tokens=True)
    try:
        return _parse_result(result)
    finally:
        # 清理临时缩放图片（_resize_for_vlm 落的那张 .vlm_tmp.jpg）
        if input_path != image_path and os.path.exists(input_path):
            os.remove(input_path)


def _parse_result(result):
    """从模型输出里抠出那段 JSON。

    输出可能长这样（顺序和数量都不固定，所以一层层剥）：
        <|im_start|>assistant
        <think>…</think>
        ```json
        { … }
        ```
    以前只有一个 removeprefix("```json")，撞上 <think> 就直接解析失败，
    然后**静默降级**成 visual_key = 半截原文、match = "unknown" ——
    调用方看到的是「分析成功但字段是空的」，比报错还难查。
    """
    import re
    # 1) 去掉 <think>…</think>（可能跨多行，也可能只有空内容）
    result = re.sub(r'<think>.*?</think>', '', result, flags=re.S)
    # 2) 只看 assistant 之后的正文
    if 'assistant' in result:
        result = result.split('assistant')[-1]
    # 3) 直接抠第一个 { 到最后一个 } —— 比 removeprefix/removesuffix 稳，
    #    能一并吃掉 ```json 围栏、前后解释文字和残留的特殊 token
    lo, hi = result.find('{'), result.rfind('}')
    if lo != -1 and hi > lo:
        try:
            return json.loads(result[lo:hi + 1])
        except json.JSONDecodeError:
            pass
    result = result.strip()
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        # 解析失败，返回原始文本（保留原文前 50 字，便于在日志里看出模型到底说了啥）
        return {
            "visual_key": result[:50],
            "match": "unknown",
            "match_reason": f"JSON 解析失败: {result[:80]}",
            "subject_pos": "center",
            "has_text": False,
            "text_content": "",
            # describe 模式才有的字段，一并给出，免得调用方 KeyError
            "kind": "",
            "subjects": "",
            "suits": "",
        }


def main():
    parser = argparse.ArgumentParser(description="本地视觉模型（Qwen3.5-2B）图片分析")
    parser.add_argument("image_path", help="图片路径")
    parser.add_argument("--narration", default="", help="解说词文本（校验模式用）")
    parser.add_argument("--describe", action="store_true",
                        help="理解模式：说清图里有什么、写着什么字、适合讲什么（写稿前用）")
    parser.add_argument("--output-format", default="json", choices=["json", "text"])
    args = parser.parse_args()

    if not os.path.exists(args.image_path):
        print(json.dumps({"error": f"图片不存在: {args.image_path}"}))
        sys.exit(1)

    # 检查图片有效性
    try:
        with Image.open(args.image_path) as img:
            img.verify()
    except Exception as e:
        print(json.dumps({"error": f"图片无法读取: {e}"}))
        sys.exit(1)

    result = analyze(args.image_path, args.narration,
                     mode='describe' if args.describe else 'match')

    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.describe:
        print(f"画面: {result.get('visual_key', '?')}  [{result.get('kind', '?')}]")
        print(f"适合讲: {result.get('suits', '?')}")
        if result.get('has_text'):
            print(f"画面文字: {result.get('text_content', '')}")
    else:
        print(f"画面: {result.get('visual_key', '?')}")
        print(f"匹配: {result.get('match', '?')} — {result.get('match_reason', '')}")
        print(f"主体位置: {result.get('subject_pos', '?')}")
        if result.get('has_text'):
            print(f"画面文字: {result.get('text_content', '')}")


if __name__ == "__main__":
    main()
