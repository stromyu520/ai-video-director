# 🎬 AI Video Director · 智能视频剪辑

[English](README_EN.md) | **中文**

> 给一个**标题**或**网页链接**，AI 自动走完：联网搜索 → 写解说稿 → 定情绪节奏 → 语音克隆合成 → 配画面 → 配 BGM → 烧字幕 → 出片。

一个跑在**自己机器上**的短视频自动生成管线。核心是「AI 导演」模式：大模型扮演导演，把标题变成一份带情绪的**分镜脚本**（`director.yaml`），再由 ffmpeg 执行层逐场景渲染成片。

<br>

## 📖 目录

- [它能做什么](#它能做什么)
- [三条管线](#三条管线)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [Web 界面](#web-界面)
- [配置说明](#配置说明)
- [路径与可移植性](#路径与可移植性)
- [常见问题](#常见问题)
- [许可](#许可)

<br>

## 它能做什么

| 能力 | 说明 |
|------|------|
| 🎤 **声音克隆** | 3 秒参考音频克隆音色，注册前自动做 5 项音质检测（码率/信噪比/动态/细节/低频），不达标直接拒绝 |
| 😮💨 **智能表现力 TTS** | LLM 读懂文本语义，自动插入 `[breath]` `[sigh]` `[laughter]` 等标记，合成**带呼吸、叹气、笑声、情绪重音**的人声 |
| 🗣️ **多方言** | 广东话 / 四川话 / 东北话等 17 种方言控制 |
| 🎬 **AI 导演** | 大模型联网搜索 → 写稿 → 按情绪分段 → 为每段指定画面来源与 BGM |
| 🎞️ **二创重剪** | 对已有视频做镜头检测 + 转录 + 画面分析，重剪成新片（原片片段 + 新素材 + 新配音） |
| 🔍 **素材自动获取** | 说「搜索:城市夜景」就自动从 Pexels 下载配图/空镜，24 小时缓存复用 |
| 💬 **圆角字幕** | ASS 圆角半透明背景字幕，可直接烧录进画面 |
| 🧩 **转场** | 场景间交叉溶解（xfade），镜头时长按解说词节奏自动计算 |
| 🖥️ **Web 界面** | FastAPI 本地服务，浏览器里选管线、传素材、看进度、下载成片 |

<br>

## 三条管线

```
① 通用管线  标题/URL ──→ director.yaml ──→ video_make_ffmpeg.py ──→ 成品
                (AI 导演联网写稿)              (ffmpeg 逐场景渲染)

② 二创管线  原视频 ──→ analysis.json ──→ remix.yaml ──→ render_remix.py ──→ 成品
                (镜头检测+转录+画面理解)      (重剪编排)

③ 语音工具  文本/参考音频 ──→ tts_express.py / voice_cli.py ──→ 带情绪的人声
```

<br>

## 工作原理

```
用户（标题 / URL / 素材）
    ↓
┌─ AI 导演 ─────────────────────────────────┐
│  联网搜索 → 内容分析 → 写解说稿             │
│  定情绪节奏 → 配画面/BGM 方案               │
│  输出 director.yaml                        │
└───────────────────────────────────────────┘
    ↓ 用户确认
┌─ 渲染执行层 ──────────────────────────────┐
│  tts_express.py    → 带情感的语音            │
│  Pexels / 本地素材  → 画面                   │
│  场景逐段渲染       → xfade 转场拼接          │
│  ass_subtitle.py   → 圆角字幕烧录            │
│  bgm_prep.py       → BGM 混音                │
│  encoder.py        → 输出 MP4                │
└───────────────────────────────────────────┘
```

<br>

## 环境要求

| 项 | 要求 |
|----|------|
| **OS** | Linux（WSL2 可用）。已在 Ubuntu 22.04 + WSL2 实测 |
| **Python** | 3.11（3.10 需额外装 `tomli`） |
| **GPU** | NVIDIA 显卡，显存 ≥ 8GB（CosyVoice3 语音合成 + 转录）。纯 CPU 也能跑，但慢很多 |
| **CUDA** | 12.1（对应 torch 2.3.1+cu121） |
| **ffmpeg** | ≥ 4.4。**推荐 5.x 以上** —— 4.4 有个双视频流滤镜的已知 bug，本项目用「三步法」绕过了，详见 [常见问题](#常见问题) |
| **磁盘** | 模型约 15GB，加上素材缓存建议预留 30GB |

<br>

## 安装

### 1. 系统依赖

```bash
sudo apt update
sudo apt install -y ffmpeg fonts-wqy-zenhei tesseract-ocr tesseract-ocr-chi-sim
```

- `fonts-wqy-zenhei` —— 中文字幕字体（`ass_subtitle.py` 里写死了 `wqy-zenhei.ttc`，换字体要改 `FONT` 常量）
- `tesseract` —— 只用于旧的字幕验证流程，可留可去（现在推荐用多模态模型验证，见 `ocr_k3.py`）

### 2. 克隆本项目

```bash
git clone <本仓库地址> ai-video-director
cd ai-video-director
```

### 3. 克隆 CosyVoice（**必须**，单独一个仓库）

语音克隆的引擎是官方的 [FunAudioLLM/CosyVoice](https://github.com/FunAudioLLM/CosyVoice)，**不进本仓库**，需要自己 clone 到项目根目录下：

```bash
git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git
```

> ⚠️ 一定要带 `--recursive`，否则 `third_party/Matcha-TTS` 是空的，`import cosyvoice` 会失败。

### 4. 建虚拟环境装依赖

```bash
python3.11 -m venv video-env
source video-env/bin/activate

# 先装 CosyVoice 的（它锁定了 torch 等重型包的版本）
pip install -r CosyVoice/requirements.txt

# 再装本项目的
pip install -r requirements.txt
```

> 顺序不能反 —— 两个文件都钉了 torch/torchaudio 的版本，反着装会出现互相顶掉的情况。

### 5. 下载模型

放到 `models/` 下：

```bash
mkdir -p models

# ① 语音克隆权重（约 5GB，必须）
modelscope download --model FunAudioLLM/Fun-CosyVoice3-0.5B-2512 \
    --local_dir models/Fun-CosyVoice3-0-5B-2512

# ② 语音识别权重（约 500MB，必须：转录 + 音色注册时的质量校验都用它）
huggingface-cli download Systran/faster-whisper-small \
    --local-dir models/whisper-small

# ③ Demucs 人声分离（可选，只有 bgm_prep.py --separate 用得到）
#    首次运行时会自动下载到 ~/.cache，不用手动放
```

模型装到别处也行，用环境变量 `MODELS_DIR` / `COSYVOICE_MODEL` / `WHISPER_MODEL` 指过去，见 [路径与可移植性](#路径与可移植性)。

### 6. 配置密钥

编辑项目根目录的 `config.toml`（仓库里是**模板，不含任何真实密钥**）：

```toml
[llm]
base_url = "https://api.deepseek.com/v1/chat/completions"
api_key  = ""              # ← 填你的大模型 key
model    = "deepseek-v4-flash"

[pexels]
api_key = ""               # ← 填你的 Pexels key，或到 https://www.pexels.com/api/ 免费申请
```

- **LLM key 是必需的**（写稿、编排、表现力标记都靠它）。任何 OpenAI 兼容接口都行，不限于 DeepSeek —— 改 `base_url` + `model` 即可，请求参数档会自动跟着切。
- **Pexels key 可选**。不填的话「搜索:关键词」自动配图用不了，手动往 `素材/` 放图片视频那条路不受影响。
- 也可以用环境变量覆盖，不动文件：`LLM_API_KEY` / `LLM_API_URL` / `LLM_MODEL` / `PEXELS_API_KEY`。

> 🔒 **填完真实 key 后，别再提交上去**。最省事的做法是让 git 忽略你对这个文件的本地修改：
> ```bash
> git update-index --skip-worktree config.toml
> ```

### 7. 验证环境

```bash
python verify_env.py
```

全部通过会打印 `✅ 全部验证通过！`，包含 torch / CUDA / 显存 / CosyVoice 加载测试。

### 8.（可选）视觉模型环境

「图片理解」功能（读你上传的图片、给出画面描述喂给 LLM 写稿）跑在一个**独立环境**里 —— 它需要 transformers 5.x，而主环境被 CosyVoice 钉在 4.51.3，装一起会打架。所以项目用 `project_paths.VLM_PYTHON` 指过去、子进程调用。

不装完全不影响主流程，把 `config.toml` 里 `[vlm] enabled` 改成 `false` 即可。

想装的话：

```bash
python3.11 -m venv vlm-env
./vlm-env/bin/pip install -r vlm-requirements.txt
modelscope download --model Qwen/Qwen3.5-2B --local_dir models/Qwen3.5-2B
```

<br>

## 快速开始

### 先注册一个音色

管线默认用名为 `故事女声` 的音色。用任意一段 3 秒以上、干净的人声注册：

```bash
source video-env/bin/activate
python voice_cli.py 注册 参考音频.wav "参考音频里说的那句话" --spk-id 故事女声
python voice_cli.py 列表          # 查看已注册音色
```

注册时会自动做 5 项音质检测，不达标会**拒绝并说明原因**；通过后用 whisper 做一次测试合成，匹配率低于 30% 会自动回滚删除。

> 想快速试听不入库：`python tts_express.py "你好，欢迎收听今天的节目。" 参考音频.wav --express`

### 管线① 通用（标题 → 成片）

`director.yaml` 是 AI 导演的输出，也是渲染层的输入。长这样：

```yaml
project:
  title: "端到端测试"
  duration: 20

voice:
  spk_id: "故事女声"
  express: false
  speed: "中"

scenes:
  - id: 1
    narration: "这是一段图片场景的旁白测试，用来验证渲染管线是否正常工作"
    mood: "平实"
    visuals:
      type: "image"
      source: "641.webp"          # 本地图片

  - id: 2
    narration: "接下来是动态视频素材场景，素材来自 Pexels 图库的自动搜索下载"
    mood: "兴奋"
    visuals:
      source: "搜索:城市夜景"      # 自动去 Pexels 搜「城市夜景」并下载

bgm:
  path: ""          # 留空 = 不配 BGM
  volume: 0.25
  fade_in: 2
  fade_out: 3

output:
  path: ""
  subtitle: true    # 烧录字幕
```

手写一份，或者让 AI 导演生成，然后渲染：

```bash
python video_make_ffmpeg.py director.yaml
```

**手动放素材**：项目目录下建 `素材/`（图片和短视频同层，按扩展名区分），场景的 `visuals` 留空或多场景共用时，会自动按顺序轮转分配。

### 管线② 二创（原视频 → 重剪）

```bash
# 1. 分析原视频：镜头检测 + 转录 + 画面理解 → analysis.json
python analyze_video.py 原视频.mp4 工作目录/

# 2. 编排方案（可选：先看看素材缺口）
python check_materials.py 工作目录/
python plan_remix.py 工作目录/ --bgm 背景音乐.mp3     # → remix.yaml

# 2b. 若 remix.yaml 里有 "搜索:关键词"，解析并下载
python resolve_materials.py 工作目录/ --plan          # 先列出待下载关键词，供你确认/改词
python resolve_materials.py 工作目录/                 # 下载并改写 yaml 为本地路径

# 3. 渲染成片
python render_remix.py 工作目录/ --bgm 背景音乐.mp3   # → final_remix.mp4
```

`remix.yaml` 的场景类型：`original`（裁原片，可保留原声）/ `image`（图片，带 Ken Burns 慢推）/ `clip`（短片）/ `text`（文字卡）。详见 [`二创/README.md`](二创/README.md)。

### 管线③ 语音工具

```bash
# 智能表现力合成（LLM 自动插标记）
python tts_express.py "他沉默了很久，深深叹了口气，然后说：我认输。" 参考音频.wav --express

# 用已注册音色 + 方言
python tts_express.py "今天天气真好，我们去散步吧" --spk-id 故事女声 --express --lang 四川话

# 纯语音克隆 CLI
python voice_cli.py 合成 "你好，我是故事女声。"
```

无网络时 `--express` 会自动降级为启发式规则（按标点/关键词加标记），不会中断。

<br>

## Web 界面

```bash
source video-env/bin/activate
python -m web.server              # 或 uvicorn web.server:app
# → http://127.0.0.1:8000
```

浏览器里可以：选管线 → 填参数 → 上传素材 → 实时看进度（SSE）→ 预览下载成片；也能在页面上直接改 LLM / Pexels 配置。配置写回 `config.toml` 时会保留文件里的注释。

<br>

## 配置说明

`config.toml` 是**唯一**的配置文件。读取优先级一律是 **环境变量 > 本文件 > 中性默认值**。

| 段 | 键 | 说明 |
|----|----|------|
| `[llm]` | `base_url` / `api_key` / `model` | 任何 OpenAI 兼容接口。请求参数档（是否带 `reasoning_effort`）按 model 自动判断，见 `pipeline_config.profile_for()` |
| `[tts]` | `active` | 只决定网页默认选中哪栏，**不影响渲染**。渲染只认音色名前缀 |
| | `[tts.backends.*]` | `edge:` 前缀 = 线上 Edge（免费无需 key）；`openai:` 前缀 = 任何 OpenAI 兼容 `/audio/speech` 服务；无前缀 = 本地 CosyVoice |
| `[vlm]` | `enabled` / `model_dir` / `timeout` | 图片理解。触发条件是**手动上传的图片** —— 自动抽帧和 Pexels 素材都不喂（一张约 10s + 4.2GB 显存） |
| `[pexels]` | `api_key` | 素材站 key |
| `[defaults]` | `spk_id` / `size` / `express` / `speed` / `optimize` / `cuda` | 页面与导演的默认值 |
| `[runner]` | `max_upload_mb` | Web 上传大小上限 |

<br>

## 路径与可移植性

所有「项目之外的路径」都收口在 **`project_paths.py`** 一个文件里，每一项都能用环境变量覆盖。项目挪目录、换机器都不用改代码。

| 环境变量 | 默认值 | 用途 |
|---------|--------|------|
| `PROJECT_ROOT` | 本文件所在目录 | 项目根 |
| `COSYVOICE_DIR` | `<根>/CosyVoice` | CosyVoice 源码 |
| `MODELS_DIR` | `<根>/models` | 模型总目录 |
| `COSYVOICE_MODEL` | `<MODELS_DIR>/Fun-CosyVoice3-0-5B-2512` | 语音克隆权重 |
| `WHISPER_MODEL` | `<MODELS_DIR>/whisper-small` | 转录权重 |
| `DEMUCS_REPO` | `<MODELS_DIR>/demucs_repo` | 人声分离模型 |
| `REF_AUDIO` | `<COSYVOICE_DIR>/asset/zero_shot_prompt.wav` | 音色注册的官方参考音频 |
| `PIPELINE_PYTHON` | 当前解释器 | 跑管线的 Python |
| `VLM_PYTHON` | `<根>/vlm-env/bin/python` | 视觉模型的独立环境 |
| `VLM_MODEL_DIR` | `<根>/models/Qwen3.5-2B` | 视觉模型权重 |
| `MPT_CONFIG` | `<根>/MoneyPrinterTurbo/config.toml` | 可选：复用 MoneyPrinterTurbo 的 key |
| `BGM_DIR` | `<根>/BGM` | BGM 成品输出目录 |

例：模型装在 `/data/models` 下，一行环境变量搞定，不用动代码：

```bash
export MODELS_DIR=/data/models
python video_make_ffmpeg.py director.yaml
```

<br>

## 常见问题

<details>
<summary><b><code>import cosyvoice</code> 失败</b></summary>

八成是 clone CosyVoice 时没带 `--recursive`，`third_party/Matcha-TTS` 是空目录。补上：

```bash
cd CosyVoice && git submodule update --init --recursive
```
</details>

<details>
<summary><b>ffmpeg 版本与双流滤镜 bug</b></summary>

ffmpeg 4.4 在处理「两个视频流 + 滤镜」时有个已知 bug（本项目开发机就是 4.4）。项目里所有复杂拼接都走**三步法**绕开：先各自抽轨 → 再用滤镜处理 → 最后合并，避免单条命令里同时挂两个视频流。

如果你用的是 ffmpeg 5.x / 6.x，可以正常跑，无需额外处理。
</details>

<details>
<summary><b>显存不够（OOM）</b></summary>

- `config.toml` 里把 `[vlm] enabled` 设为 `false` —— 视觉模型一张图吃 4.2GB，是最大头
- 转录会自动从 `cuda/float16` 降级到 `cpu/int8`，慢但不会崩
- `[defaults] cuda = 0` 指定用哪块卡
</details>

<details>
<summary><b>字幕没有中文 / 显示成方块</b></summary>

缺中文字体。装 `fonts-wqy-zenhei`，或者改 `ass_subtitle.py` / `video_make_ffmpeg.py` 里的 `FONT` 常量指向你系统里已有的字体文件（`.ttf` / `.ttc`）。
</details>

<details>
<summary><b>音色注册总是被拒</b></summary>

注册前有 5 项音质硬门槛（码率 ≥128kbps、信噪比 ≥30dB、峰值因子 ≥3.5、零交叉率 ≥0.08、低频占比 ≤45%）。用**安静环境下录的、没有背景音乐的**人声，3~10 秒，参考文本必须和音频内容**逐字对应**。
</details>

<details>
<summary><b>合成的音频太短 / 效果差</b></summary>

合成文本建议 20 字以上；参考音频和参考文本不匹配会显著拉低克隆质量。CosyVoice 的 `<|endofprompt|>` 会自动加在参考文本末尾，不用手写。音频统一 24000Hz 单声道。
</details>

<details>
<summary><b>LLM 相关功能报「请先配置 key」</b></summary>

`config.toml` 的 `[llm] api_key` 是空的。填上，或用环境变量 `LLM_API_KEY` 临时给。
</details>

<br>

## 目录结构

```
ai-video-director/
├── project_paths.py        # 🧭 所有外部路径的唯一来源（环境变量可覆盖）
├── pipeline_config.py      # 🔑 所有 LLM/VLM 配置的唯一来源
├── config.toml             # ⚙️  配置文件（模板，不含真实密钥）
│
├── tts_express.py          # 🚀 智能表现力 TTS（核心）
├── tts_express_prompt.md   #    表现力标记定义 + 提示词模板
├── tts_backends.py         #    音色前缀分发（本地 CosyVoice / Edge / OpenAI 兼容）
├── voice_cli.py            #    音色注册 / 列表 / 删除 / 合成
│
├── video_make_ffmpeg.py    # 🎬 通用管线渲染引擎（ffmpeg，当前主力）
├── video_make.py           #    旧版 moviepy 引擎（保留作对照）
├── director_agent.py       # 🧠 AI 导演
├── text_to_director.py     #    标题/文本 → director.yaml
├── image_brief.py          #    写稿前读懂项目里的图片
├── vlm_analyze.py          #    视觉模型分析器（独立 vlm-env，子进程调用）
│
├── render_remix.py         # 🎬 二创管线渲染引擎
├── analyze_video.py        #    镜头检测 + 转录 + 画面分析
├── plan_remix.py           #    二创编排
├── check_materials.py      #    素材缺口评估
├── resolve_materials.py    #    自动搜索素材下载
│
├── pexels_materials.py     # 🎥 Pexels 素材搜索/缓存/下载
├── bgm_prep.py             # 🎵 BGM 预处理（提取/裁剪/人声分离/循环/标准化）
├── ass_subtitle.py         # 💬 SRT → ASS 圆角字幕
├── xfade_join.py           # 🧩 交叉溶解拼接（三步法）
├── encoder.py              #    编码参数
├── speech_rate.py          #    语速分析
├── ocr_k3.py               #    多模态 OCR（字幕验证用）
├── ocr_img.py              #    Tesseract OCR（旧）
├── verify_env.py           # ✅ 环境自检
│
├── web/                    # 🖥️  FastAPI Web 界面
│   ├── server.py           #    入口：python -m web.server
│   ├── config.py           #    读写 config.toml
│   ├── pipelines.py        #    管线注册与参数校验
│   ├── jobs.py             #    任务队列 + SSE 进度
│   ├── projects.py         #    项目发现与成片预览
│   └── static/             #    前端（原生 JS，无构建步骤）
│
├── prompts/                # 💡 提示词
├── docs/                   # 📚 详细项目文档
├── 二创/ 其他/              # 📁 各管线的说明
├── requirements.txt
├── vlm-requirements.txt
└── LICENSE
```

<br>

## 许可

[MIT](LICENSE)。本仓库**不含** CosyVoice 源码与任何模型权重，这些需自行安装并遵循其原始许可（CosyVoice 为 Apache 2.0）。

> ⚠️ **声音克隆请合法使用。** 不要克隆未获授权的声音，不要用于伪造他人言论、诈骗或任何违法用途。使用者需自行承担合规责任。
