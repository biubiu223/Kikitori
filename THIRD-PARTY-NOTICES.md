# 第三方组件说明 / Third-Party Notices

Kikitori（听译）本身以 [GNU GPL v3](LICENSE) 发布，并附有下文的**附加许可**。
发布包（`Kikitori-*-win-x64.zip`）中包含以下第三方组件，它们各自遵循自己的许可证。

## 附加许可（GPL-3.0 第 7 节）/ Additional permission under GNU GPL version 3 section 7

> If you modify this Program, or any covered work, by linking or combining it with the
> NVIDIA CUDA libraries (including cuBLAS, cuDNN and NVRTC) or the Microsoft Windows App SDK
> runtime (or modified versions of those libraries), containing parts covered by the terms of
> their respective licenses, the licensors of this Program grant you additional permission to
> convey the resulting work.

中文说明：本程序运行时需要和 NVIDIA CUDA 运行库、Microsoft Windows App SDK 运行库一起工作，这些库不是开源的。上面这段附加许可明确允许把本程序和这些库组合使用、一起分发。（NVIDIA 运行库目前不随发布包分发，见文末。）

## 识别/翻译引擎（`engine\`，PyInstaller 打包的 Python 程序）

| 组件 | 版本 | 许可证 | 项目地址 |
|---|---|---|---|
| Python | 3.14 | PSF-2.0 | https://www.python.org |
| faster-whisper | 1.2.1 | MIT | https://github.com/SYSTRAN/faster-whisper |
| CTranslate2 | 4.8.2 | MIT | https://github.com/OpenNMT/CTranslate2 |
| Silero VAD 模型（随 faster-whisper 提供） | v6 | MIT | https://github.com/snakers4/silero-vad |
| ONNX Runtime | 1.30.0 | MIT | https://github.com/microsoft/onnxruntime |
| PyAV | 18.1.0 | BSD-3-Clause | https://github.com/PyAV-Org/PyAV |
| FFmpeg（随 PyAV 提供） | — | LGPL-2.1-or-later | https://ffmpeg.org |
| python-soxr / libsoxr | 1.1.0 | LGPL-2.1-or-later | https://github.com/dofuuz/python-soxr |
| PyAudioWPatch / PortAudio | 0.2.12.9 | Apache-2.0 / MIT | https://github.com/s0d3s/PyAudioWPatch |
| NumPy（含 OpenBLAS） | 2.5.3 | BSD-3-Clause 等 | https://numpy.org |
| httpx / httpcore | 0.28.1 / 1.0.9 | BSD-3-Clause | https://github.com/encode/httpx |
| huggingface_hub / hf-xet | 1.33.0 / 1.7.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| tokenizers | 0.23.2 | Apache-2.0 | https://github.com/huggingface/tokenizers |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | https://github.com/tqdm/tqdm |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| 其它纯 Python 依赖（anyio、h11、idna、filelock、fsspec、packaging、protobuf、flatbuffers、PyYAML、click、colorama、typing_extensions） | — | MIT / BSD / Apache-2.0 / PSF | 见各自项目 |
| PyInstaller 引导程序 | 6.22.3 | GPL-2.0-or-later，附允许以任意许可证分发打包程序的例外 | https://pyinstaller.org |

## 首次运行时下载的组件（不包含在发布包中）

| 组件 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| NVIDIA cuBLAS | 12.9 | NVIDIA 专有许可（CUDA Toolkit EULA） | PyPI `nvidia-cublas-cu12`（NVIDIA 官方发布） |
| NVIDIA cuDNN | 9.27 | NVIDIA 专有许可（cuDNN SLA） | PyPI `nvidia-cudnn-cu12`（NVIDIA 官方发布） |
| NVIDIA NVRTC | 12.9 | NVIDIA 专有许可（CUDA Toolkit EULA） | PyPI `nvidia-cuda-nvrtc-cu12`（NVIDIA 官方发布） |
| Whisper large-v3 模型（CTranslate2 格式） | — | MIT（OpenAI；格式由 SYSTRAN 转换） | HuggingFace `Systran/faster-whisper-large-v3` |

以上组件在第一次使用时由程序从官方来源或其镜像（清华 TUNA、阿里云 PyPI 镜像；hf-mirror.com）下载到本机，下载时校验文件哈希，使用时受各自许可证约束。

## 界面（`Kikitori.exe`，WinUI 3 / .NET）

| 组件 | 版本 | 许可证 | 项目地址 |
|---|---|---|---|
| .NET 运行时 | 8.0 | MIT | https://github.com/dotnet/runtime |
| Windows App SDK（含 WinUI 3） | 1.8 | Microsoft 软件许可条款（运行库可再分发） | https://github.com/microsoft/WindowsAppSDK |
| CommunityToolkit.Mvvm | 8.4.0 | MIT | https://github.com/CommunityToolkit/dotnet |
| CommunityToolkit.WinUI.Controls.SettingsControls | 8.2 | MIT | https://github.com/CommunityToolkit/Windows |
| WinUIEx | 2.9.3 | MIT | https://github.com/dotMorten/WinUIEx |
| H.NotifyIcon.WinUI | 2.3.2 | MIT | https://github.com/HavenDV/H.NotifyIcon |
| System.Security.Cryptography.ProtectedData | 8.0.0 | MIT | https://github.com/dotnet/runtime |

各组件的完整许可证文本请见对应项目地址。
