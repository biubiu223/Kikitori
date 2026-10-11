# 界面 ⇄ 引擎 通信协议（v1）

本文档是 WinUI 3 界面（C#）和 Python 识别/翻译引擎之间的**唯一约定**。两边各自开发，只要遵守这里的格式就能对接。

## 1. 进程与文件布局

发布后的目录：

```
Kikitori\
  Kikitori.exe                 ← WinUI 3 界面（C#，自包含，免安装）
  engine\
    KikitoriEngine.exe         ← Python 引擎（PyInstaller onedir 打包）
    _internal\...               ← Python 运行时、CUDA DLL、模型资源等
```

- 用户数据目录：`%LOCALAPPDATA%\Kikitori\`
  - `settings.json`：界面保存的全部设置（界面自己管理）
  - `logs\engine.log`：引擎的 stderr 输出（由界面写入）
  - `字幕记录\`：引擎写入的字幕记录（路径通过 config.log_dir 传给引擎）
- Whisper 模型：继续使用 HuggingFace 默认缓存（`%USERPROFILE%\.cache\huggingface`），本机已有 large-v3。

**开发时**引擎可以不打包，在仓库根目录用 `.venv\Scripts\python.exe engine_server.py` 启动，协议完全相同。
界面查找引擎的顺序：
1. 环境变量 `KIKITORI_ENGINE`（完整命令行，例如 `"D:\...\python.exe" "D:\...\engine_server.py"`）
2. `<界面exe所在目录>\engine\KikitoriEngine.exe`

## 2. 传输方式

- 界面启动引擎子进程：**不显示窗口**（CreateNoWindow），重定向 stdin / stdout / stderr。
- **stdin**：界面 → 引擎 的命令，**stdout**：引擎 → 界面 的事件。
- 格式：**JSON Lines**，UTF-8（无 BOM），每行一个 JSON 对象，以 `\n` 结尾。
  - 界面（C#）写 stdin 时必须显式用 `new UTF8Encoding(false)`（`ProcessStartInfo.StandardInputEncoding`），读 stdout/stderr 用 UTF-8。
  - 引擎读每一行时要先去掉行首的 `﻿`（BOM）再解析——实测某些写入方会在第一行前带 BOM，导致第一条命令解析失败。
- **stdout 只能输出协议 JSON**。引擎里所有日志、print、第三方库输出（tqdm 进度条等）一律走 **stderr**。
- **stderr**：人类可读日志，界面原样追加写入 `logs\engine.log`，并保留最后 50 行用于出错提示。
- 引擎读到 stdin **EOF**（界面退出/崩溃）时必须在 2 秒内自行退出，防止残留进程。
- 界面还应把引擎进程放进 Job Object（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE），界面崩溃时引擎一并被系统结束。

## 3. 配置对象 `config`

`start` 和 `test_translation` 命令携带。字段与原 `config.toml` 一一对应：

```json
{
  "translate": {
    "provider": "deepseek",            // "deepseek" | "ollama" | "openai_compatible" | "off"
    "target_language": "zh-Hans",      // "zh-Hans" | "zh-Hant" | "en" | "ja" | "ko"
    "context_lines": 4,
    "deepseek": {"api_key": "sk-...", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "temperature": 1.3},
    "ollama": {"base_url": "http://localhost:11434", "model": "qwen3:8b", "temperature": 0.3},
    "openai_compatible": {"base_url": "", "api_key": "", "model": "", "temperature": 0.7}
  },
  "asr": {
    "model": "large-v3",               // 或 "large-v3-turbo" / "kotoba-tech/kotoba-whisper-v2.0-faster"
    "device": "cuda",                  // "cuda" | "cpu"
    "compute_type": "float16",         // "float16" | "int8_float16" | "int8"
    "language": "ja",                  // "ja"|"en"|"ko"|"zh"|"ru"|"fr"|"es"|"de"|"auto"
    "beam_size": 5,
    "show_partial": true,
    "partial_interval": 1.0
  },
  "vad": {"threshold": 0.5, "min_silence_ms": 500, "max_segment_s": 8.0, "min_speech_ms": 250},
  "audio": {"device": "", "file": ""}, // device 为空=默认播放设备；file 非空=测试用，从音频文件读取（按真实速度播放）
  "glossary": {"兎田ぺこら": "兔田佩克拉"},
  "log_dir": "C:\\Users\\...\\AppData\\Local\\Kikitori\\字幕记录"
}
```

缺少的字段一律用上面的默认值。

## 4. 命令（界面 → 引擎，stdin）

| 命令 | 字段 | 说明 |
|---|---|---|
| `start` | `config` | 按配置开始监听。若 ASR 模型参数（model/device/compute_type）和已加载的不同则重新加载模型，否则复用。正在运行时收到 `start` = 用新配置重启（停止录音→应用配置→重新开始）。 |
| `stop` | — | 停止录音（模型保留在显存里，下次 start 秒开）。 |
| `pause` | `value: bool` | 暂停/继续识别（录音不停，只是丢弃音频）。 |
| `set_languages` | `source`, `target` | 运行中切换说话语言/目标语言，对下一句生效。 |
| `reconnect_audio` | — | 重新连接音频设备（用户换了耳机/音箱后）。 |
| `list_devices` | — | 返回 `devices` 事件。 |
| `test_translation` | `config` | 用给定的翻译配置翻一句示例，返回 `translation_test` 事件。不影响正在运行的识别。 |
| `shutdown` | — | 停止一切并退出（退出码 0）。 |

示例：`{"cmd":"set_languages","source":"en","target":"zh-Hans"}`

收到无法识别的命令或字段错误时，引擎回一个 `notice`（level=error），不要崩溃。

## 5. 事件（引擎 → 界面，stdout）

所有事件都有 `"event"` 字段。

### 5.1 生命周期 / 状态

```json
{"event":"hello","version":"1.0.0","protocol":1}
```
进程启动、导入完成后立刻发送（在加载模型之前）。界面收到前不要发命令以外的假设；收到 hello 时清空字幕列表（引擎重启后 id 会从 1 开始）。

```json
{"event":"status","state":"loading_model","message":"正在加载语音识别模型…","level":"info"}
```
`state` 取值：
- `idle`：已启动、尚未 start
- `downloading_model`：首次下载模型（可带 `"progress": 0.0~1.0`）
- `loading_model`、`warming_up`
- `listening`：正在监听（message 里写设备名和语言，例如“正在监听：扬声器 (Realtek(R) Audio) | 日语 → 简体中文 | DeepSeek · deepseek-flash”）
- `paused`、`stopped`
- `error`：致命错误，例如模型加载失败、无法录音（message 写原因）

```json
{"event":"notice","message":"API Key 无效（401）","level":"error"}
```
临时提示（翻译失败、语言切换成功、翻译配置有问题等）。`level`：`info` | `warn` | `error`。

```json
{"event":"fatal","message":"..."}
```
引擎即将因无法恢复的错误退出前发送。

### 5.2 设备与音量

```json
{"event":"devices","devices":[{"name":"扬声器 (Realtek(R) Audio)","is_default":true},{"name":"耳机 (USB Audio Device)","is_default":false}]}
```

```json
{"event":"level","rms":0.0312}
```
监听中每 200ms 最多发一次（16k 单声道音频的 RMS，0~1）。超过 0.5 秒没有音频数据时发一次 `rms: 0`。用于界面显示“有没有听到声音”。

### 5.3 字幕

每句话有一个整数 `id`（同一个引擎进程内单调递增，start/stop 不重置）。

```json
{"event":"partial","id":12,"text":"今日はね、えっと"}
```
说话过程中的原文草稿（会多次刷新同一个 id）。

```json
{"event":"final","id":12,"text":"今日はね、えっと、新しいゲームやっていこうかなと思ってて","lang":"ja","mode":"translating","ts":1760000000.123}
```
一句话识别完成。`ts` 为这句开始说的时间（Unix 秒）。`mode`：
- `translating`：译文稍后由 `translation` 事件给出（界面先显示原文 + “…”）
- `no_translation`：翻译已关闭或配置有误，只显示原文
- `same_language`：说的就是目标语言，不翻译；界面把 `text` 当作**译文（大字）**显示，不显示原文行

```json
{"event":"translation","id":12,"text":"今天呢，呃，我打算来玩个新游戏","ok":true,"elapsed":0.62}
```
翻译失败时：`{"event":"translation","id":12,"text":"","ok":false,"error":"翻译请求失败：..."}`（界面显示“（翻译失败）”）。

```json
{"event":"remove","id":12}
```
这句最终被判定为噪音/空白，界面删掉对应的草稿。

### 5.4 其它

```json
{"event":"translation_test","ok":true,"message":"连接正常","input":"こんにちは、今日もよろしくね！","output":"大家好，今天也请多关照！","elapsed":0.58}
```
`ok=false` 时 `message` 写给用户看的原因（Key 无效、连不上、模型不存在等）。

## 5.5 实现细节约定（以引擎实际行为为准）

- **音量**：暂停时录音没停，`level` 仍然发真实音量。没在放声音时 WASAPI 不送数据，引擎每段静音只发一次 `rms: 0`。
- **重发当前状态**：`set_languages` 之后、`reconnect_audio` 之后、`pause` 切换时，会按**真实状态**再发一次 status：暂停中发 `status paused`，否则发 `status listening`。两者的 message 都带最新的设备/语言/翻译信息（例如“已暂停：扬声器 (…) | 日语 → 简体中文 | DeepSeek · deepseek-flash”）。还没开始监听时不发。
- **translation**：失败时也带 `elapsed`。`stop` 之后，已经发出 `final` 的句子仍可能补发 `translation`，界面照常更新即可。
- **remove**：只对发过 `partial` 的 id 发送。`stop` 时还没出结果的草稿会补发 `remove`。
- **start**：不带 config 时全部用默认值；字段无效时发 `notice(warn)` 并改用默认值。start 会清除暂停状态。
- **pause**：还没开始监听时收到 `pause`，直接忽略，不回事件。
- **设备**：指定的播放设备找不到时，改用默认设备并发 `notice(warn)`。正在录音时 `list_devices` 可能返回旧列表（PortAudio 在有录音流时不刷新），`reconnect_audio` 或重新 `start` 后会刷新。系统默认播放设备变了不会被自动发现，需要界面发 `reconnect_audio`。
- **恢复**：上一次 `start` 因为录不了音而失败（`status error`）后，`reconnect_audio` 会按上次的配置重新 start。

## 6. 典型时序

```
界面启动引擎进程
引擎 → hello
引擎 → status idle
界面 → list_devices          引擎 → devices
界面 → start {config}
引擎 → status loading_model → status warming_up → status listening
引擎 → level … partial(1) … partial(1) … final(1, translating) … translation(1) …
用户在字幕条右键切换语言：界面 → set_languages   引擎 → notice "已切换：英语 → 简体中文"
用户退出：界面 → shutdown，等待 3 秒，未退出则强制结束进程
```
