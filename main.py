"""直播同声传译 —— 程序入口。

用法：
    python main.py                    正常启动（录制系统声音）
    python main.py --list-devices     列出可以录制的播放设备
    python main.py --file 测试.wav    用音频文件代替系统声音（测试用）
"""

import argparse
import ctypes
import os
import sys
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
APP_TITLE = "听译"


def setup_background_logging():
    """用 pythonw 启动时没有控制台，print 的内容写到 运行日志.txt（每次启动覆盖），方便排查问题。"""
    if sys.stdout is None or sys.stderr is None:
        f = open(os.path.join(HERE, "运行日志.txt"), "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


def message_box(text: str, error: bool = True):
    print(text)
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10 if error else 0x40)


def fatal(text: str):
    message_box(text, error=True)
    sys.exit(1)


_instance_mutex = None


def ensure_single_instance() -> bool:
    """防止重复打开（后台运行时看不到窗口，很容易双击两次）。"""
    global _instance_mutex
    if sys.platform != "win32":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex = k32.CreateMutexW(None, False, "Local\\LiveTranslateOverlay")
    return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS


def load_config(path: str) -> dict:
    # 仓库里只放 config.example.toml（config.toml 可能填了 API Key，不进 Git）。第一次运行时复制一份。
    example = os.path.join(os.path.dirname(path), "config.example.toml")
    if not os.path.exists(path) and os.path.exists(example):
        import shutil

        shutil.copyfile(example, path)
        print(f"已根据 config.example.toml 生成 {path}")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        fatal(f"找不到配置文件：{path}")
    except tomllib.TOMLDecodeError as e:
        fatal(f"配置文件 config.toml 格式有误：\n{e}\n\n常见原因：引号没配对、用了中文引号、少了等号。")


def list_devices():
    from livetrans.audio import list_loopback_devices

    devices, default = list_loopback_devices()
    print("\n可以录制的播放设备（在 config.toml 的 [audio] device 里填名字的一部分即可）：\n")
    for d in devices:
        mark = "  ← 当前默认" if default and d["index"] == default["index"] else ""
        name = d["name"].replace(" [Loopback]", "")
        print(f"  - {name}  ({int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} 声道){mark}")
    print()


def main():
    ap = argparse.ArgumentParser(description="直播同声传译")
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--file", help="用音频文件代替系统声音（测试用）")
    ap.add_argument("--speed", type=float, default=1.0, help="--file 的播放倍速")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    # 先确认没有在运行，再打开日志文件（否则会把正在运行那一份的日志清空）
    # （开发测试时可以设置环境变量 KIKITORI_NO_SINGLE_INSTANCE=1 跳过这个检查，不占用单实例锁）
    if os.environ.get("KIKITORI_NO_SINGLE_INSTANCE") != "1" and not ensure_single_instance():
        message_box("听译已经在运行了。\n\n要关闭的话：在字幕条上点右键 →「退出」。", error=False)
        return
    setup_background_logging()

    cfg = load_config(args.config)

    # 必须在 import faster_whisper 之前
    from livetrans.cuda_setup import add_cuda_dll_dirs

    add_cuda_dll_dirs()

    from livetrans.audio import FileSource, LoopbackCapture
    from livetrans.engine import Engine, OverlaySink
    from livetrans.overlay import Overlay

    log_dir = os.path.join(HERE, "字幕记录")
    overlay = Overlay(
        cfg.get("overlay", {}),
        os.path.join(HERE, "window_state.json"),
        log_dir,
        default_source=cfg.get("asr", {}).get("language", "ja"),
        default_target=cfg.get("translate", {}).get("target_language", "zh-Hans"),
    )

    if args.file:
        make_source = lambda q: FileSource(q, args.file, args.speed)  # noqa: E731
    else:
        device = cfg.get("audio", {}).get("device", "")
        make_source = lambda q: LoopbackCapture(q, device)  # noqa: E731

    engine = Engine(cfg, OverlaySink(overlay), make_source, log_dir,
                    source_lang=overlay.source_lang, target_lang=overlay.target_lang)
    overlay.on_languages = engine.set_languages
    overlay.on_pause = engine.set_paused
    overlay.on_reconnect = engine.reconnect
    overlay.on_quit = engine.stop

    engine.start()
    print("字幕窗口已打开：左键拖动移动，拖右下角 ↔ 调整宽度，右键打开菜单（暂停/字号/退出等）。")
    overlay.run()
    os._exit(0)   # 后台线程（显卡推理、网络请求）不等了，直接退出


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        message_box(f"程序出错退出了：\n{e}\n\n详细信息见 运行日志.txt")
        os._exit(1)
