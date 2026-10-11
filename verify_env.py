"""video-env 环境验证脚本
用法: video-env/bin/python verify_env.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P
P.add_cosyvoice_to_path()

import torch, torchaudio, subprocess

print("=" * 55)
print("  video-env 环境验证")
print("=" * 55)

print(f"\nPython:    {sys.version.split()[0]}")
print(f"torch:     {torch.__version__}")
print(f"CUDA:      {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU:       {torch.cuda.get_device_name(0)}")
    print(f"VRAM:      {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
    print(f"cuDNN:     {torch.backends.cudnn.version()}")

import faster_whisper;      print(f"faster-w:  {faster_whisper.__version__}")
import moviepy;              print(f"moviepy:   {moviepy.__version__}")
import scenedetect;          print(f"scenedet:  {scenedetect.__version__}")
import soundfile;            print(f"soundfile: {soundfile.__version__}")
import librosa;              print(f"librosa:   {librosa.__version__}")
import modelscope;           print(f"modelscope:{modelscope.__version__}")
import transformers;         print(f"transform: {transformers.__version__}")
import onnxruntime as ort;   print(f"onnxrt-gpu:{ort.__version__} CUDA={'CUDAExecutionProvider' in ort.get_available_providers()}")

# CosyVoice 加载测试
MODEL_PATH = str(P.COSYVOICE_MODEL)
from cosyvoice.cli.cosyvoice import AutoModel
cosyvoice = AutoModel(model_dir=MODEL_PATH)
print(f"CosyVoice: ✅ v3.0 loaded ({cosyvoice.sample_rate}Hz)")

r = subprocess.run(['nvidia-smi','--query-gpu=driver_version,memory.total,memory.free','--format=csv,noheader'],
                   capture_output=True, text=True, timeout=5)
print(f"nvidia:    {r.stdout.strip()}")
print(f"\n✅ 全部验证通过！")
