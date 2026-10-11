# 第三方组件声明 / Third-Party Notices

本仓库（`ai-video-director`）自身代码以 [MIT](LICENSE) 授权。

**本仓库不包含**下面这些组件的代码或权重，它们由使用者自行安装，各自遵循其原始许可。

## 依赖的第三方组件

| 组件 | 许可 | 说明 |
|---|---|---|
| [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) (FunAudioLLM) | Apache License 2.0 | 声音克隆与语音合成 |
| Fun-CosyVoice3-0-5B-2512 权重 | 见模型仓库自带许可 | 需单独下载 |
| [Demucs](https://github.com/facebookresearch/demucs) (Meta Research) | MIT License | 人声/伴奏分离 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT License | 语音转写（ASR） |
| Qwen3.5-2B 权重 | 见模型仓库自带许可 | 视觉理解，需单独下载 |
| Edge TTS | 遵循 Microsoft 服务条款 | 在线 TTS，免费音色 |
| Pexels API | 遵循 [Pexels 服务条款](https://www.pexels.com/license/) | 自动素材检索 |

其余 Python 依赖见 `requirements.txt` 与 `vlm-requirements.txt`，各自遵循其原始许可。

## 内容授权

本仓库中的 `prompts/` 与 `docs/` 含中文提示词与工程笔记，随代码一并按 MIT 授权。

## ⚠️ 声音克隆合规警告

语音克隆是**双刃剑**。使用本项目的克隆功能时：

- 不要克隆未获授权的声音
- 不要用于伪造他人言论、冒充身份、诈骗或任何违法用途
- 合成音视频请按当地法律要求标注 AI 生成

**使用者需自行承担全部合规责任。** 项目作者不对滥用行为负责。
