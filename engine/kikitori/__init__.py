"""Kikitori（听译）识别/翻译引擎：系统声音 → 断句 (Silero VAD) → 识别 (Whisper) → 翻译 (LLM)。

由 engine_server.py 驱动，和 WinUI 界面的通信协议见 docs/protocol.md。
"""
