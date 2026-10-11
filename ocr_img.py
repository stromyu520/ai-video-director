#!/usr/bin/env python3
"""ocr_img.py — 图片文字识别（中文），用于从官网截图中提取文字

用法:
  python ocr_img.py <图片路径>

支持: jpg, png, webp
依赖: tesseract-ocr + tesseract-ocr-chi-sim
"""

import sys, os, subprocess, tempfile, re
from PIL import Image

def ocr_image(img_path: str) -> str:
    """Tesseract OCR 识别图片中的文字"""
    if not os.path.exists(img_path):
        return f"文件不存在: {img_path}"
    
    # 压缩图片到合适大小（Tesseract 对大图处理慢）
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
    try:
        img = Image.open(img_path)
        w, h = img.size
        if w > 2000:
            ratio = 2000 / w
            img = img.resize((2000, int(h * ratio)), Image.LANCZOS)
        img.save(tmp)
        
        result = subprocess.run(
            ['tesseract', tmp, 'stdout', '-l', 'chi_sim'],
            capture_output=True, text=True, timeout=60
        )
        return result.stdout.strip()
    finally:
        try: os.unlink(tmp)
        except: pass

def ocr_batch(img_dir: str):
    """批量识别目录下所有图片"""
    exts = ('.jpg', '.jpeg', '.png', '.webp')
    for f in sorted(os.listdir(img_dir)):
        if f.lower().endswith(exts):
            path = os.path.join(img_dir, f)
            print(f"\n=== {f} ===")
            text = ocr_image(path)
            print(text[:500])
            if len(text) > 500:
                print(f"  ... ({len(text)}字符总长)")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <图片路径 或 目录>")
        sys.exit(1)
    
    path = sys.argv[1]
    if os.path.isdir(path):
        ocr_batch(path)
    else:
        print(ocr_image(path))
