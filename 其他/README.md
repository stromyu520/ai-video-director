# 🎬 其他智能剪辑（通用管线）

## 用途

你给我**标题**或**网页链接**，我（Hermes Agent）去搜索内容、写解说稿、找配图/配视频，生成 `director.yaml`，你确认后跑 `video_make_ffmpeg.py` 出成品。

## 流程

```
你: 标题 / URL
  ↓ 我
├── 联网搜索 → 获取内容
├── 分析重点 → 定节奏/情绪
├── 配素材 → 二选一：
│   ├── "搜索:关键词"（Pexels 自动下载视频，如 搜索:城市夜景航拍）
│   └── 手动图片/视频 → 放进项目目录（图片任意位置，视频放 素材/ 子目录）
├── 写解说稿 → 按场景分段
└── 生成 director.yaml
  ↓ 你确认
video_make_ffmpeg.py → 成品
```

## 用法

```bash
python video_make_ffmpeg.py <director.yaml>
```

## 素材来源（三选一，按优先级）

| 方式 | director.yaml 写法 | 说明 |
|------|-------------------|------|
| Pexels 自动下载 | `visuals: {source: "搜索:城市夜景"}` | 自动搜索下载高清视频，24h 缓存复用 |
| 手动素材目录 | `visuals: {type: "video"}`（或 source 留空） | 自动发现 `素材/` 目录视频，按场景顺序分配；多镜头自动切片不重复 |
| 精确路径 | `visuals: {source: "素材/xxx.mp4"}` | 本地图片/视频直用 |

## 产出物

```
其他/xxx项目名/
├── director.yaml        ← 导演方案
├── director_optimized.yaml  ← optimize=true 时 VLM+LLM 重排后的方案
├── 素材/                ← 手动视频素材（可选）
├── 图片/                ← 配图（可选）
└── output.mp4           ← 成品视频
```

## 详细文档

见 `docs/智能剪辑项目文档.md`（组件详解、director.yaml 完整参考、LLM 配置、常见问题）。
