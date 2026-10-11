# 🎬 二创短视频（智能剪辑）

对原始视频做二次创作：**原视频裁剪片段 + 新素材 + 字幕 + 新声音 → 重新生成新视频**。

## 流程

```bash
# 1. 分析原视频（镜头检测 + 转录 + VLM 画面分析）
video-env python analyze_video.py <视频.mp4> [输出目录]     # → analysis.json

# 2. 编排方案（可选：检查素材缺口）
video-env python check_materials.py <工作目录>              # → 素材缺口评估
video-env python plan_remix.py <工作目录> [--bgm 音乐]      # → remix.yaml

# 2b. 解析自动搜索素材（仅当 remix.yaml 含 "搜索:" 时）
video-env python resolve_materials.py <工作目录> --plan      # → 列出待下载关键词，供确认/改词
video-env python resolve_materials.py <工作目录>             # → 下载到 图片//短片/ 并改写 remix.yaml

# 3. 渲染成片
video-env python render_remix.py <工作目录> [--bgm 音乐]    # → final_remix.mp4
```

## remix.yaml 场景类型

| 类型 | 含义 | 素材来源 |
|------|------|---------|
| `original` | 裁剪原视频片段（可非线性跳跃，`audio: original` 保留原声） | 原视频时间轴 |
| `image` | 插入图片（Ken Burns 慢推） | 本地 `图片/` 目录 / `"搜索:关键词"`（Pexels 照片）/ `_keyframes/` 原视频关键帧 |
| `clip` | 插入短片 | `"搜索:关键词"`（Pexels 视频）/ `短片/`、`素材/` 目录自动发现 / 本地路径 |
| `text` | 收尾文字卡片 | — |

## 素材获取规则

- 编排时 LLM 判断：本地素材不足 → 场景写 `source: "搜索:关键词"`（image→照片，clip→视频空镜），关键词由 LLM 按该片段旁白/转录/画面内容生成
- `resolve_materials.py` 下载并改写 yaml 为本地路径；失败降级：image→原视频关键帧→文字卡，clip→文字卡
- `搜索:关键词`（Pexels 下载，24h 缓存复用）→ `短片/`、`素材/` 目录自动发现（按场景顺序轮转）→ yaml 精确路径

## 技术要点

- **字幕**：非 original 场景烧录 ASS 圆角半透明背景字幕（60px，与通用管线同款，`ass_subtitle.py`）
- **转场**：全片段 xfade 0.16s 交叉溶解（三步法，绕 ffmpeg 4.4 双流 bug）
- **旁白**：`tts_express.py`（CosyVoice 克隆音色，可用 `REMIX_SPK` 环境变量指定音色，默认"故事女声"）
- **时长**：场景时长由旁白朗读决定（不压缩），文案超长自动截短重合成

## 依赖

- `tts_express.py` — 语音合成
- `render_remix.py` — 渲染引擎
- `pexels_materials.py` — Pexels 素材（`搜索:`前缀时）
- `ass_subtitle.py` — ASS 字幕

全部位于项目根目录，需在 `video-env` 虚拟环境里运行（`source video-env/bin/activate`）。
