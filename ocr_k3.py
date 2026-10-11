#!/usr/bin/env python3
"""ocr_k3.py — 用 k3（Kimi 多模态）识别图片文字，替代 tesseract 做字幕验证

tesseract 对视频帧大字/半透明底识别差（2026-08-20 实测），k3 视觉能力强得多。

用法:
  python ocr_k3.py <图片路径>                 # 默认：逐行输出图中文字
  python ocr_k3.py <图片路径> --prompt "..."  # 自定义指令（如 只输出字幕那两行）

key 读取链：环境变量 MOONSHOT_API_KEY → MPT config.toml moonshot_api_key
"""

import sys, os, base64, requests, tomllib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P

MPT_CONFIG = P.MPT_CONFIG
K3_URL     = 'https://api.moonshot.cn/v1/chat/completions'
K3_MODEL   = 'kimi-k3'

DEFAULT_PROMPT = ('识别图片中所有文字，逐行输出，不要解释、不要添加任何其他内容，'
                  '标点符号保留原样')


def get_k3_key():
    k = os.environ.get('MOONSHOT_API_KEY', '').strip()
    if k:
        return k
    try:
        with open(MPT_CONFIG, 'rb') as f:
            v = (tomllib.load(f).get('app') or {}).get('moonshot_api_key', '')
        return str(v).strip()
    except Exception:
        return ''


def ocr_image(img_path, prompt=DEFAULT_PROMPT):
    """k3 识别图片文字，返回识别文本；失败返回 None"""
    key = get_k3_key()
    if not key:
        return f"❌ 未找到 Moonshot key（env MOONSHOT_API_KEY / {MPT_CONFIG}）"
    if not os.path.exists(img_path):
        return f"❌ 文件不存在: {img_path}"
    ext = os.path.splitext(img_path)[1].lower()
    mime = 'image/png' if ext == '.png' else ('image/jpeg' if ext in ('.jpg', '.jpeg') else 'image/webp')
    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    try:
        # 注意：k3 只允许 temperature=1（传 0 会 400）；org RPM≈3，连续调用需间隔 20s+
        resp = requests.post(K3_URL, json={
            "model": K3_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": 1024,
        }, headers={"Authorization": f"Bearer {key}"}, timeout=180)
        if resp.status_code == 429:
            return "⏳ k3 限流 429（org RPM≈3），等 20s+ 再试"
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"❌ k3 调用失败: {e}"


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    img = sys.argv[1]
    prompt = DEFAULT_PROMPT
    if '--prompt' in sys.argv:
        prompt = sys.argv[sys.argv.index('--prompt') + 1]
    print(ocr_image(img, prompt))
