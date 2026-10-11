#!/usr/bin/env python3
"""plan_remix.py — DeepSeek 二创编排，生成 remix.yaml

用法:
  python plan_remix.py <工作目录> [--bgm <音乐.mp3>] [--mode full|smart] [--no-search]

流程:
  1. 读取 analysis.json
  2. 调用 DeepSeek 生成 remix.yaml
  3. 准备渲染

工作目录需包含:
  analysis.json  # analyze_video.py 输出的分析报告
  素材/          # 用户上传的图片与短视频（可选，见 MANUAL_DIRS）
  图片/ 短片/    # 自动下载的素材落点（可选）
  _keyframes/    # 原视频关键帧（可选，可作图片素材）

场景 source 写 "搜索:关键词"（image→Pexels 照片 / clip→Pexels 视频）时，
由 resolve_materials.py 下载素材并改写 remix.yaml 后，再交给 render_remix.py。
"""
import sys, os, json, subprocess, yaml, requests   # subprocess 此前漏 import：
# 第 119 行用它探测短片时长，报错被裸 except 吞掉 → 时长提示从未进入提示词

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import llm as _llm         # 密钥不再硬编码，见 pipeline_config.py
# 素材目录与扩展名只有 video_make_ffmpeg 那一份 —— 这里原来写死 ['图片','短片']，
# 用户传进 素材/ 的东西从不进提示词，表现是「传了但片子里没有」。
from video_make_ffmpeg import IMG_EXTS, VIDEO_EXTS                # noqa: E402
from pipeline_config import MANUAL_DIRS                           # noqa: E402

LLM_API_URL, LLM_API_KEY, LLM_MODEL = _llm()


SYSTEM_PROMPT = """你是一个短视频二创导演。根据原视频分析报告和可用素材，设计全新的二创方案。

二创原则：
1. 保留精华，砍掉拖沓部分
2. 可以用新增图片/短片替换原片段
3. 旁白让内容更有价值
4. BGM 根据情绪自动匹配

## 解说模式：{mode_desc}

## 重要规则：original 场景的 start/end 必须对齐转录时间轴
不要切在句子中间。例如完整转录里有 [2.3s] 和 [6.9s] 的时间点，
那 original 的 start/end 就只能用这些时间点，不能用 5.8s 或 8.0s 这种非转录时间。
**切点优先选在句子完整结束处**（转录段 [X-Y] 的 Y 就是一句话的结尾），
避免从句子中间开始或结束，否则观众会听到半句话。

## 时间轴语义（重要）
- original：从原视频截取 [start, end] 的片段，使用原视频的绝对时间。
- image/clip/text：插入素材，**原视频时间轴暂停**，不消耗原视频时长。
- 多个 original 之间可以跳跃：跳过不保留的内容，后面的 original 仍用原视频的绝对时间。
- 示例：original [0-2.3] → image 4秒 → original [6.9-9]：原视频 2.3~6.9 秒的内容被跳过、不播放。

## 时长约束（duration 是参考值，实际时长由旁白朗读长度决定）
- image/clip/text 场景的 duration 只是**预估参考**：渲染时场景实际时长 = 旁白朗读长度 + 0.4 秒停顿，
  旁白读完才转场，不会截断或压缩旁白。所以**旁白文案长度直接决定画面时长**。
- 参考语速：中文约 4-5 字/秒。
  例如旁白 6 字 ≈ 画面 2 秒；12 字 ≈ 3 秒；20 字 ≈ 5 秒；27 字 ≈ 7 秒。
- 建议：image 场景旁白 12~20 字（画面 3~5 秒），太长会拖节奏；text 收尾卡片 8~12 字（2~3 秒）。
**字幕显示规则：所有 narration 文案控制在 15 字以内**（超长字幕在手机上显示不全），用逗号分隔长句时每段也需≤15字。

## clip 短片音频规则（audio 字段）
- **保留原声（audio: original）**：短片是有解说价值的素材（新闻播报、采访、解说类，
  文件名含 新闻/播报/采访/解说/片段 等字样时优先），此时 narration 填 null，让短片原声自然播放。
- **TTS 旁白（audio: tts）**：短片是纯画面/B-roll/无有效原声时，配 TTS 旁白（narration 必填）。
- 不确定时优先保留原声。
- **注意：自动搜索下载的短片（source 以 搜索: 开头）是空镜 B-roll，无原声无解说价值，audio 必须用 tts 且 narration 必填。**

## image 图片音频规则（audio 字段）
- **保留原视频背景音频（audio: original）**：图片时段继续播放原视频背景声音（氛围/原声解说延续），
  此时 narration 填 null（不配旁白）。
- **TTS 旁白（audio: tts，默认）**：图片时段静音原视频背景，只播旁白（narration 必填）。
- 默认用 tts（旁白），需要氛围声时用 original。

输出 strict YAML，格式必须严格遵循示例。"""


def build_prompt(analysis, assets_list, bgm_path, mode='smart', allow_search=True):
    """构建二创编排提示词

    `allow_search=False`（页面上的「自动搜索」关了）→ 提示词里禁掉 `搜索:`，
    改成「少排几场 / 多留原片段」。硬拦在 resolve_materials.py 和
    render_remix.py 里各有一道（那两个才是真正联网的地方）。
    """
    lines = []
    lines.append(f"原视频: {os.path.basename(analysis['source'])}")
    lines.append(f"时长: {analysis['duration']:.1f}s")
    lines.append(f"镜头数: {len(analysis['scenes'])}")

    # 完整转录
    lines.append(f"\n## 完整转录")
    for seg in analysis.get('transcript', []):
        lines.append(f"  [{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}")

    # 镜头列表
    lines.append(f"\n## 镜头列表")
    for sc in analysis['scenes']:
        vk = sc.get('vlm', {}).get('visual_key', '')
        tc = sc.get('vlm', {}).get('text_content', '')
        parts = [f"  [{sc['start']:.1f}s-{sc['end']:.1f}s]({sc['duration']:.1f}s)"]
        if vk:
            parts.append(f"画面:{vk}")
        if tc:
            parts.append(f"文字:{tc[:40]}")
        lines.append(' | '.join(parts))
        if sc.get('narration'):
            lines.append(f"    解说: {sc['narration'][:120]}")

    # 可用素材（含时长信息）
    lines.append(f"\n## 可用素材")
    if assets_list:
        for a in assets_list:
            lines.append(f"  {a}")
        # 时长探测：素材目录清单与 video_make_ffmpeg.MANUAL_DIRS 同一份 ——
        # 以前这里写死 ['短片','图片'] 不认 素材/，用户传的视频既不在清单里、
        # 也不探时长，LLM 自然从不引用它。
        for subdir in MANUAL_DIRS:
            d = os.path.join(os.path.dirname(analysis.get('source', '')), subdir)
            if not os.path.isdir(d):
                d = os.path.join(os.getcwd(), subdir)
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith(tuple(VIDEO_EXTS)):
                        fp = os.path.join(d, fn)
                        dur_s = None
                        try:
                            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                                'format=duration', '-of', 'csv=p=0', fp],
                                               capture_output=True, text=True)
                            dur_s = float(r.stdout.strip())
                        except:
                            pass
                        if dur_s:
                            lines.append(f"    ⏱️ {subdir}/{fn} 时长: {dur_s:.1f}s")
    else:
        lines.append(f"  无额外素材")

    # 原视频关键帧（可作 image 场景插图，画面与原视频一致，质量好）
    kf_dir = os.path.join(os.path.dirname(analysis.get('source', '')), '_keyframes')
    if os.path.isdir(kf_dir):
        kfs = sorted(f for f in os.listdir(kf_dir) if f.lower().endswith('.jpg'))[:8]
        if kfs:
            lines.append(f"\n## 原视频关键帧（可作图片素材）")
            for f in kfs:
                lines.append(f"  _keyframes/{f}")

    if bgm_path:
        lines.append(f"\nBGM: {bgm_path}")

    # 解说模式
    mode_desc = {
        'full': '【全部解说】每个场景都用新旁白替换原声，全面重新解读，不要保留任何原声',
        'smart': '【智能解说】由你判断：关键信息/转折点/亮点用新解说，过渡性或画面自说明处保留原声'
    }.get(mode, '【智能解说】由你判断：关键信息/转折点/亮点用新解说，过渡性或画面自说明处保留原声')
    lines.append(f"\n## 解说模式\n{mode_desc}")

    if allow_search:
        lines.append(f"""



## 素材获取规则（重要）
- 本地素材优先（素材/短片/关键帧）；当某场景需要特定画面但本地没有合适素材时，直接用自动搜索：
  - image 场景：source 写 `"搜索:关键词"` → 自动下载 Pexels 静态照片（风景/城市/物品特写等插图画面）
  - clip 场景：source 写 `"搜索:关键词"` → 自动下载 Pexels 视频空镜（B-roll，无原声，见 clip 音频规则）
- 关键词必须用**英文**（Pexels 只支持英文搜索，中文词匹配不到会返回热门视频，
  2026-08-20 实测多个不同中文词首候选撞同一烟花视频），2~4 个词、具体可拍摄，
  如 `"搜索:train interior"`、`"搜索:city night aerial"`、`"搜索:newborn nursery"`。
  根据该片段的旁白/转录/画面内容判断该搜什么，禁止抽象词（news、hot、debate、rules 之类）。
- 自动搜索素材是空镜/插图，不承载具体信息，关键信息必须写进旁白。
- 本地素材 + 搜索都覆盖不了的收尾、强调等场景，用 type: text 文字卡片。
- **不要输出"建议用户补充素材"之类的说明文字，素材不足时直接用 搜索: 解决。**
""")
    else:
        lines.append(f"""



## 素材获取规则（重要）：本次关闭了自动搜索
- **禁止出现 `搜索:`**。本次不联网找素材，写了也不会被下载 ——
  resolve_materials.py 和 render_remix.py 都不会发请求。
- 只能用上面「可用素材」清单里的本地素材和 `_keyframes/` 关键帧，source 照抄清单里的写法。
- **素材不够就少排几场**：宁可片子短、场次少，也不要拿不相干的画面凑数。
- 缺画面时**优先多保留原视频片段（original）**—— original 不消耗任何额外素材，
  而且比插一张不相干的图更贴近原意。
- 确实需要大字强调时用 type: text 文字卡片，但只留给关键数字/金句，不要用来顶缺素材的场次。
- **不要输出"建议用户补充素材"之类的说明文字。**
""")

    lines.append(f"""

## 收尾规则
最后一个场景结束后，添加一个 type: text 收尾卡片（duration 2-3秒，内容如"关注我，了解更多"或"下期再见"等）。
（这是设计好的片尾卡，不是「素材不够拿文字卡凑」，关了自动搜索也照写。）

## 输出格式
直接输出 YAML（{'素材不足用 搜索: 自动获取' if allow_search else '只能用本地素材、禁止写 搜索:'}，不输出素材建议文字）。
YAML 格式如下（字段必须完整）:

```yaml
remix:
  title: "二创标题"
  scenes:
    - type: original      # 原片段: 保留画面和原声
      start: 0.0
      end: 5.0
      narration: null     # null=保留原声
      bgm: null           # null=不插BGM

    - type: original      # 原片段: 替换旁白
      start: 5.0
      end: 12.0
      narration: "重新写的旁白文本"
      bgm: "calm"         # upbeat/calm/highlight/dramatic

    - type: image         # 插入图片
      source: "素材/xxx.jpg"   # 本地图（照抄素材清单里的写法）；自动搜索开着时可写 "搜索:关键词"
      duration: 3.0
      narration: "旁白（保留原视频背景音频时填 null）"
      audio: tts          # tts=旁白 / original=保留原视频背景音频（见 image 图片音频规则）
      bgm: "highlight"

    - type: clip          # 插入短片
      source: "素材/xxx.mp4"   # 本地短片（照抄素材清单）；自动搜索开着时可写 "搜索:关键词"
      duration: 3.0
      narration: "旁白（保留原声时填 null）"
      audio: tts          # tts=配音旁白 / original=保留短片原声（见 clip 短片音频规则）
      bgm: null

    - type: text          # 文字卡片
      duration: 3.0
      narration: "显示的文本"
      bgm: null
```""")

    return '\n'.join(lines)


def plan(work_dir):
    analysis_path = os.path.join(work_dir, 'analysis.json')
    if not os.path.exists(analysis_path):
        print(f"❌ 未找到: {analysis_path}")
        print(f"   先运行: python analyze_video.py <视频> {work_dir}")
        sys.exit(1)

    with open(analysis_path, encoding='utf-8') as f:
        analysis = json.load(f)

    # 扫描素材：目录与扩展名都走渲染模块那一份，别在这儿写窄了
    # （原来只认 6 种扩展名，`.mkv`/`.avi`/`.bmp` 传上去等于没传）
    assets = []
    for subdir in MANUAL_DIRS:
        d = os.path.join(work_dir, subdir)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(tuple(IMG_EXTS) + tuple(VIDEO_EXTS)):
                assets.append(f"  {subdir}/{fn}")

    bgm = None
    if '--bgm' in sys.argv:
        idx = sys.argv.index('--bgm')
        if idx + 1 < len(sys.argv) and os.path.exists(sys.argv[idx+1]):
            bgm = sys.argv[idx+1]

    mode = 'smart'
    if '--mode' in sys.argv:
        idx = sys.argv.index('--mode')
        if idx + 1 < len(sys.argv):
            mode = sys.argv[idx+1]

    # 页面上的「自动搜索」开关。这里只影响提示词 —— 真正联网的是
    # resolve_materials.py 和 render_remix.py，它们各有一道同样的闸门；
    # 三处必须认同一个标志，否则会出现「提示词说禁了、下游还是偷偷下载」。
    allow_search = '--no-search' not in sys.argv

    print(f"🧠 DeepSeek 编排二创方案...")
    if not allow_search:
        print("🔌 自动搜索已关闭：只排本地素材（缺画面优先多留原片段）")
    prompt = build_prompt(analysis, assets, bgm, mode, allow_search=allow_search)
    try:
        resp = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 8192,
            "reasoning_effort": "low",
        }, headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=120)

        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 提取 YAML
        suggestion = ''
        if '```yaml' in raw:
            suggestion = raw.split('```yaml')[0].strip()
            content = raw.split('```yaml', 1)[-1]
            content = content.rsplit('```', 1)[0]
        elif '```' in raw:
            suggestion = raw.split('```')[0].strip()
            content = raw.split('\n', 1)[-1] if raw.startswith('```') else raw
            content = content.rsplit('```', 1)[0]
        else:
            content = raw

        if suggestion and '无需' not in suggestion:
            print(f"  💡 建议: {suggestion[:200]}")

        content = content.strip()

        remix_path = os.path.join(work_dir, 'remix.yaml')
        with open(remix_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ 二创方案 → {remix_path}")

        try:
            parsed = yaml.safe_load(content)
            if isinstance(parsed, dict):
                scenes = parsed.get('remix', parsed).get('scenes', [])
                print(f"   场景数: {len(scenes)}")
        except:
            print(f"   ⚠️ YAML 解析警告，请检查格式")

        return remix_path

    except Exception as e:
        print(f"❌ 编排失败: {e}")
        return None


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    plan(sys.argv[1])
