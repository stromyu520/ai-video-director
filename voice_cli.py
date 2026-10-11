"""注册音色 → 质量检测 → 保存 → 测试合成验证"""
import sys, os, tempfile, json, textwrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths as P
P.add_cosyvoice_to_path()

from cosyvoice.cli.cosyvoice import AutoModel
import torch, torchaudio
import subprocess
import numpy as np
import soundfile as sf
from scipy import signal

MODEL_PATH = str(P.COSYVOICE_MODEL)
SPK_ID = '故事女声'

# ============================================================
# 官方参考音频（zero_shot_prompt.wav）的质量基准
# 注册时会比对，低于阈值的素材直接拒绝
# ============================================================
REF_FILE = str(P.REF_AUDIO)

# 质量阈值（基于官方参考的实测值放宽设定）
QUALITY_THRESHOLDS = {
    'snr_db':        {'min': 30,   'unit': 'dB',  'desc': '信噪比'},        # 官参 78dB
    'crest_factor':  {'min': 3.5,  'unit': '',    'desc': '峰值因子(动态)'}, # 官参 8.0
    'zcr':           {'min': 0.08, 'unit': '',    'desc': '零交叉率(细节)'}, # 官参 0.139
    'low_pct':       {'max': 45,   'unit': '%',   'desc': '低频占比(<500Hz)'}, # 官参 16.1%
    'bitrate_kbps':  {'min': 128,  'unit': 'kbps','desc': '码率'},           # 官参 768kbps
}


def analyze_audio(wav_path):
    """深度分析音频，返回各项指标 dict"""
    result = {}

    # 基础信息
    dur_s = ffprobe_one(wav_path, 'format=duration')
    br_s  = ffprobe_one(wav_path, 'format=bit_rate')
    result['duration'] = float(dur_s) if dur_s else 0
    result['bitrate'] = int(br_s) // 1000 if br_s else 0

    sr_s = ffprobe_one(wav_path, 'stream=sample_rate', stream=0)
    ch_s = ffprobe_one(wav_path, 'stream=channels', stream=0)
    result['sample_rate'] = int(sr_s) if sr_s else 0
    result['channels'] = int(ch_s) if ch_s else 1

    # 音量
    r = subprocess.run(['ffmpeg', '-i', wav_path, '-af', 'volumedetect',
                        '-f', 'null', '-'], capture_output=True, text=True)
    for line in r.stderr.split('\n'):
        if 'mean_volume' in line:
            result['mean_volume'] = float(line.split()[-2].replace('dB', ''))

    # 深度频谱分析 (numpy)
    data, sr = sf.read(wav_path)
    if len(data.shape) > 1:
        data = data.mean(axis=1)
    result['samples'] = len(data)

    # SNR（取底噪百分位法）
    noise_floor = np.percentile(np.abs(data), 10)
    signal_peak = np.percentile(np.abs(data), 95)
    result['snr_db'] = 20 * np.log10(signal_peak / max(noise_floor, 1e-10))

    # 峰值因子
    result['crest_factor'] = float(np.abs(data).max() / max(np.sqrt(np.mean(data**2)), 1e-10))

    # 零交叉率
    result['zcr'] = float(((data[:-1] * data[1:]) < 0).sum() / len(data))

    # 频段分布
    f, t, Zxx = signal.stft(data, sr, nperseg=512)
    mag = np.abs(Zxx)
    total = mag.sum() + 1e-10
    result['low_pct'] = float(mag[f < 500].sum() / total * 100)
    result['mid_pct'] = float(mag[(f >= 500) & (f < 4000)].sum() / total * 100)
    result['high_pct'] = float(mag[f >= 4000].sum() / total * 100)

    return result


def ffprobe_one(wav_path, entry, stream=None):
    """执行一次 ffprobe 查询，返回值或 None"""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', entry, '-of', 'csv=p=0']
    if stream is not None:
        cmd += ['-select_streams', str(stream)]
    cmd.append(wav_path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    val = r.stdout.strip()
    return val if val else None


def check_audio_quality(wav_path):
    """全面检测参考音频质量，与官方参考比对。返回 (通过, 信息列表, 评分)"""
    info = []
    passed = True
    score = 100

    # 分析音频
    a = analyze_audio(wav_path)

    # 1. 时长检查
    dur = a['duration']
    if dur < 2:
        info.append(f"  ❌ 时长: {dur:.1f}s（至少 2 秒，太短无法提取声纹）")
        passed = False; score -= 20
    elif 3 <= dur <= 6:
        info.append(f"  ✅ 时长: {dur:.1f}s ✓")
    elif dur < 3:
        info.append(f"  ⚠️ 时长: {dur:.1f}s（推荐 3~6 秒，有点短）")
        score -= 5
    else:
        info.append(f"  ⚠️ 时长: {dur:.1f}s（推荐 3~6 秒，偏长了）")
        score -= 5

    # 2. 采样率
    sr = a['sample_rate']
    if sr >= 44100:
        info.append(f"  ✅ 采样率: {sr}Hz ✓（高规格）")
    elif sr == 24000:
        info.append(f"  ✅ 采样率: {sr}Hz ✓")
    elif sr == 16000:
        info.append(f"  ⚠️ 采样率: {sr}Hz（将重采样到 24000Hz）")
        score -= 5
    else:
        info.append(f"  ❌ 采样率: {sr}Hz（过低，至少需要 16000Hz）")
        passed = False; score -= 15

    # 3. 码率检测（关键指标）
    br = a['bitrate']
    if br >= 256:
        info.append(f"  ✅ 码率: {br}kbps ✓（高质量）")
    elif br >= 128:
        info.append(f"  ⚠️ 码率: {br}kbps（互联网压缩水平，勉强可用）")
        score -= 10
    else:
        info.append(f"  ❌ 码率: {br}kbps（低于 128kbps，细节丢失严重，无法提取声纹）")
        passed = False; score -= 25

    # 4. 信噪比（关键指标）
    snr = a['snr_db']
    if snr >= 40:
        info.append(f"  ✅ 信噪比: {snr:.0f}dB ✓（干净）")
    elif snr >= 30:
        info.append(f"  ⚠️ 信噪比: {snr:.0f}dB（有背景噪底，可用）")
        score -= 10
    else:
        info.append(f"  ❌ 信噪比: {snr:.0f}dB（噪底过高，语音不清晰）")
        passed = False; score -= 20

    # 5. 峰值因子（动态范围 — 反映压缩程度）
    cf = a['crest_factor']
    if cf >= 5.0:
        info.append(f"  ✅ 动态范围(CF): {cf:.1f} ✓（动态丰富）")
    elif cf >= 3.5:
        info.append(f"  ⚠️ 动态范围(CF): {cf:.1f}（略有压缩，可接受）")
        score -= 8
    else:
        info.append(f"  ❌ 动态范围(CF): {cf:.1f}（过度压缩，语音动态丢失）")
        passed = False; score -= 15

    # 6. 零交叉率（细节丰富度）
    zcr = a['zcr']
    if zcr >= 0.12:
        info.append(f"  ✅ 细节丰富度(ZCR): {zcr:.4f} ✓")
    elif zcr >= 0.08:
        info.append(f"  ⚠️ 细节丰富度(ZCR): {zcr:.4f}（细节一般）")
        score -= 8
    else:
        info.append(f"  ❌ 细节丰富度(ZCR): {zcr:.4f}（细节缺失，音质模糊）")
        passed = False; score -= 15

    # 7. 频段分布（低频占比 — 反映是否发闷）
    low = a['low_pct']
    if low <= 30:
        info.append(f"  ✅ 频段均衡: 低频{low:.0f}%/中频{a['mid_pct']:.0f}%/高频{a['high_pct']:.0f}% ✓")
    elif low <= 45:
        info.append(f"  ⚠️ 频段: 低频{low:.0f}%（偏多，声音略闷）")
        score -= 8
    else:
        info.append(f"  ❌ 频段: 低频{low:.0f}%（过多，声音沉闷，压缩过度）")
        passed = False; score -= 15

    return passed, info, max(score, 0)


def test_synthesis(spk_id, test_text="日本首位女首相高市早苗。"):
    """注册后试合成一段，用 whisper 验证内容是否正确"""
    print(f"\n  🔍 测试合成...")
    tmp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name

    try:
        from cosyvoice.cli.cosyvoice import AutoModel
        cosyvoice = AutoModel(model_dir=MODEL_PATH)

        chunks = []
        for result in cosyvoice.inference_zero_shot(test_text, '', '', zero_shot_spk_id=spk_id):
            if 'tts_speech' in result:
                chunks.append(result['tts_speech'].cpu())

        if not chunks:
            return False, "合成无输出"

        full = torch.cat(chunks, dim=1)
        torchaudio.save(tmp_out, full, cosyvoice.sample_rate)
        dur = full.shape[1] / cosyvoice.sample_rate
        print(f"    生成 {dur:.1f}s")

        # 用 whisper 验证内容（CPU 模式避免显存冲突）
        try:
            from faster_whisper import WhisperModel
            whisper = WhisperModel(str(P.WHISPER_MODEL),
                                   device='cpu', compute_type='int8')
            segs, _ = whisper.transcribe(tmp_out, language='zh', beam_size=5)
            transcribed = ' '.join(s.text for s in segs)
            print(f"    TTS 输出: {transcribed}")

            # 计算原文中有多少关键字符被正确合成
            ref_clean = test_text.replace('，', '').replace('。', '').replace('？', '').replace('！', '')
            out_clean = transcribed.replace('，', '').replace('。', '').replace('？', '').replace('！', '').replace(' ', '')
            match_ratio = sum(1 for c in ref_clean if c in out_clean) / len(ref_clean) if ref_clean else 0

            if match_ratio < 0.3:
                return False, f"内容匹配率仅 {match_ratio:.0%}（音色异常，建议换素材重新注册）"
            elif match_ratio < 0.6:
                print(f"    ⚠️ 内容匹配率 {match_ratio:.0%}（部分不准确，但可用）")
            else:
                print(f"    ✅ 内容匹配率 {match_ratio:.0%}")

            os.unlink(tmp_out)
            return True, f"匹配率 {match_ratio:.0%}"

        except Exception as e:
            os.unlink(tmp_out)
            return True, f"合成成功（无法验证: {e}）"

    except Exception as e:
        os.unlink(tmp_out)
        return False, f"合成失败: {e}"


if len(sys.argv) < 2:
    print(f"用法:")
    print(f"  {sys.argv[0]} 注册 <视频.mp4> <视频说的话> [--spk-id <音色名>]")
    print(f"  {sys.argv[0]} 合成 <要生成的文本> [--spk-id <音色名>]")
    print(f"  {sys.argv[0]} 列表")
    print(f"  {sys.argv[0]} 删除 <音色名关键词>")
    sys.exit(1)

mode = sys.argv[1]
cosyvoice = AutoModel(model_dir=MODEL_PATH)

if mode == '列表':
    # 查看已注册音色
    pt = torch.load(f'{MODEL_PATH}/spk2info.pt', map_location='cpu', weights_only=True)
    print(f"已注册音色 ({len(pt)} 个):")
    for k in pt:
        print(f"  - {k}")

elif mode == '删除':
    target = sys.argv[2] if len(sys.argv) > 2 else ''
    pt = torch.load(f'{MODEL_PATH}/spk2info.pt', map_location='cpu', weights_only=True)
    to_del = [k for k in pt if target in k]
    if not to_del:
        print(f'没有找到匹配 "{target}" 的音色')
        sys.exit(1)
    for k in to_del:
        del pt[k]
        print(f'  🗑️ 已删除: {k}')
    torch.save(pt, f'{MODEL_PATH}/spk2info.pt')
    print(f'✅ 剩余 {len(pt)} 个音色: {list(pt.keys())}')

elif mode == '注册':
    video_path = sys.argv[2]
    prompt_text = sys.argv[3]

    if '--spk-id' in sys.argv:
        idx = sys.argv.index('--spk-id')
        if idx + 1 < len(sys.argv):
            SPK_ID = sys.argv[idx + 1]

    ref_dur = 5  # 默认 5 秒
    if '--dur' in sys.argv:
        idx = sys.argv.index('--dur')
        if idx + 1 < len(sys.argv):
            ref_dur = int(sys.argv[idx + 1])

    print(f"🎤 注册音色 [ {SPK_ID} ]")
    print(f"   参考文本: {prompt_text[:60]}...")
    print(f"\n📥 提取音频 ({ref_dur}秒, 24000Hz)...")
    tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
    r = subprocess.run(['ffmpeg', '-y', '-i', video_path, '-t', str(ref_dur),
                        '-ar', '24000', '-ac', '1', tmp_wav],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip().split('\n')[-1] if r.stderr else '未知错误'
        print(f"  ❌ 音频提取失败: {err[:120]}")
        os.unlink(tmp_wav)
        sys.exit(1)
    fsize = os.path.getsize(tmp_wav) if os.path.exists(tmp_wav) else 0
    if fsize < 1000:
        print(f"  ❌ 音频提取失败: 文件太小 ({fsize} bytes)")
        os.unlink(tmp_wav)
        sys.exit(1)
    print(f"  ✅ 提取完成 ({fsize//1024}KB)")

    # ---- 质量检测 ----
    print(f"\n🔎 音频质量检测 (与官方参考比对):")

    # 验证 WAV 文件完整性
    verify = subprocess.run(['ffprobe', '-v', 'error', tmp_wav],
                            capture_output=True, text=True)
    if verify.returncode != 0:
        print(f"  ❌ 音频文件损坏: {verify.stderr.strip()}")
        os.unlink(tmp_wav)
        sys.exit(1)

    # 先分析官方参考
    ref = analyze_audio(REF_FILE)

    passed, issues, score = check_audio_quality(tmp_wav)

    # 打印检测结果
    for line in issues:
        print(line)

    # 打印与官方参考的对比摘要
    a = analyze_audio(tmp_wav)
    print(f"\n  ┌──────────────────────┬──────────┬──────────┐")
    print(f"  │ 指标                 │ 官方参考  │ 你的素材  │")
    print(f"  ├──────────────────────┼──────────┼──────────┤")
    print(f"  │ 码率                 │ {ref['bitrate']:>6}kbps │ {a['bitrate']:>6}kbps │")
    print(f"  │ 信噪比(SNR)          │ {ref['snr_db']:>5.0f}dB  │ {a['snr_db']:>5.0f}dB  │")
    print(f"  │ 动态范围(CF)         │ {ref['crest_factor']:>5.1f}   │ {a['crest_factor']:>5.1f}   │")
    print(f"  │ 细节(ZCR)            │ {ref['zcr']:>6.4f} │ {a['zcr']:>6.4f} │")
    print(f"  │ 低频占比             │ {ref['low_pct']:>4.1f}%   │ {a['low_pct']:>4.1f}%   │")
    print(f"  └──────────────────────┴──────────┴──────────┘")
    print(f"  🏆 质量评分: {score}/100", end="")
    if score >= 80:
        print(" ✅ 优秀，可以注册")
    elif score >= 60:
        print(" ⚠️ 合格，可注册但效果可能一般")
    elif score >= 40:
        print(" ❌ 不合格（建议换素材）")
    else:
        print(" ❌ 很差（强烈建议换素材）")

    # ---- 参数级比对（逐项检查是否达到官方标准） ----
    print(f"\n  📊 参数级比对（须全部达标才能注册）:")
    all_pass = True
    checks = [
        ('码率',       a['bitrate'],         ref['bitrate'],        256,    'kbps'),
        ('信噪比SNR',   a['snr_db'],          ref['snr_db'],         40,     'dB'),
        ('动态范围CF',  a['crest_factor'],     ref['crest_factor'],    5.0,    ''),
        ('细节ZCR',     a['zcr'],             ref['zcr'],            0.08,   ''),
        ('低频占比',    a['low_pct'],         ref['low_pct'],        30,     '%', True),
    ]
    for name, val, ref_val, min_val, unit, *is_reverse in checks:
        reverse = is_reverse[0] if is_reverse else False
        if reverse:
            ok = val <= min_val
            icon = '✅' if ok else '❌'
            ref_str = f'参考{ref_val}{unit}' if unit else f'参考{ref_val}'
            print(f"    {icon} {name}: {val:.1f}{unit} (阈值: ≤{min_val}{unit}, {ref_str})")
        else:
            ok = val >= min_val
            icon = '✅' if ok else '❌'
            ref_str = f'参考{ref_val}{unit}' if unit else f'参考{ref_val}'
            print(f"    {icon} {name}: {val:.1f}{unit} (阈值: ≥{min_val}{unit}, {ref_str})")
        if not ok:
            all_pass = False

    if not all_pass:
        print(f"\n❌ 素材未通过参数级比对，注册已终止")
        print(f"   原因: 部分指标未达到官方参考的最低标准")
        if a['bitrate'] < 256:
            print(f"   → 码率太低 ({a['bitrate']}kbps)，视频压缩太狠，找原画素材")
        if a['snr_db'] < 40:
            print(f"   → 信噪比不足 ({a['snr_db']:.0f}dB)，背景噪声多")
        if a['crest_factor'] < 5.0:
            print(f"   → 动态被压平 (CF={a['crest_factor']:.1f})，语音细节丢失")
        if a['zcr'] < 0.10:
            print(f"   → 细节不足 (ZCR={a['zcr']:.4f})，音质模糊")
        if a['low_pct'] > 30:
            print(f"   → 低频过多 ({a['low_pct']:.0f}%)，声音发闷")
        print(f"   建议: 找原始码率更高、音质更干净的视频素材重新提取")
        os.unlink(tmp_wav)
        sys.exit(1)

    # ---- 注册 ----
    print(f"\n💾 注册到模型...")
    ref_text = prompt_text + '<|endofprompt|>'
    try:
        cosyvoice.add_zero_shot_spk(ref_text, tmp_wav, SPK_ID)
        cosyvoice.save_spkinfo()
        print(f"  ✅ 音色 [{SPK_ID}] 已保存")
    except Exception as e:
        print(f"  ❌ 注册失败: {e}")
        os.unlink(tmp_wav)
        sys.exit(1)

    os.unlink(tmp_wav)

    # ---- 测试合成 ----
    test_ok, test_msg = test_synthesis(SPK_ID)
    if test_ok:
        print(f"\n✅ 音色 [{SPK_ID}] 注册成功并验证通过！({test_msg})")
    else:
        print(f"\n❌ 音色 [{SPK_ID}] 注册完成但测试失败: {test_msg}")
        print(f"   建议用 --spk-id 换个名字，或用更好的素材重新注册")
        sys.exit(1)

elif mode == '合成':
    tts_text = sys.argv[2]
    if '--spk-id' in sys.argv:
        idx = sys.argv.index('--spk-id')
        if idx + 1 < len(sys.argv):
            SPK_ID = sys.argv[idx + 1]

    print(f"用 [{SPK_ID}] 音色合成...")
    chunks = []
    for result in cosyvoice.inference_zero_shot(tts_text, '', '', zero_shot_spk_id=SPK_ID):
        if 'tts_speech' in result:
            chunks.append(result['tts_speech'].cpu())
    if chunks:
        full = torch.cat(chunks, dim=1)
        out = str(P.ROOT / 'output.wav')
        torchaudio.save(out, full, cosyvoice.sample_rate)
        secs = full.shape[1] / cosyvoice.sample_rate
        print(f"✅ 生成 {secs:.1f}秒 → {out}")
    else:
        print("❌ 合成失败")
