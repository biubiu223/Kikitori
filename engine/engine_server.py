"""Kikitori（听译）无界面引擎进程：通过 stdin / stdout 的 JSON Lines 和 WinUI 界面通信。

协议见 docs/protocol.md（和界面的唯一约定）。启动方式：
    开发时（在仓库根目录）：.venv\\Scripts\\python.exe engine\\engine_server.py
    打包后：engine\\KikitoriEngine.exe（PyInstaller onedir，见 engine/packaging/）

stdout 只能写协议 JSON：启动第一件事就把文件描述符 1、Win32 标准输出句柄和 sys.stdout 都改指向 stderr，
协议改用私有的句柄写。这样 print、tqdm、huggingface_hub、C 扩展里的 printf 都不会污染协议通道。
"""

import os
import sys


def _take_over_stdout() -> int | None:
    """把真正的 stdout 留给协议专用，其它所有输出都改到 stderr。返回协议用的文件描述符。"""
    try:
        proto_fd = os.dup(1)
    except OSError:
        proto_fd = None
    try:
        os.dup2(2, 1)                 # C 扩展里 printf 到 stdout 的也改去 stderr
    except OSError:
        try:                          # 没有 stderr：丢掉
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.close(null)
        except OSError:
            pass
    if sys.platform == "win32":
        try:                          # 直接用 GetStdHandle(STD_OUTPUT_HANDLE) 写的库也改去 stderr
            import ctypes
            import msvcrt

            ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(1))
        except Exception:
            pass
    err = sys.stderr
    if err is not None:
        try:
            err.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
    else:
        err = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = err
    sys.stdout = err
    return proto_fd


_PROTO_FD = _take_over_stdout()

# 第三方库的进度条/统计：进度条本来就会去 stderr，但满屏的 \r 进度条写进日志文件没法看
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# 模型下载不用 Xet 协议（镜像站不一定支持；走普通 HTTP 时才能在进度回调里中止下载）
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import json  # noqa: E402
import math  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from datetime import datetime  # noqa: E402

VERSION = "1.1.0"
PROTOCOL = 1


def log(*args):
    print("[server]", *args, file=sys.stderr, flush=True)


def _hard_exit(code: int):
    """立即退出：不等后台线程（显卡推理、网络请求），也避开 CTranslate2 在解释器退出时偶尔崩溃的问题。"""
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


# ======================================================================
#  协议输出
# ======================================================================
def _sanitize(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if hasattr(obj, "item"):          # numpy 标量
        try:
            return _sanitize(obj.item())
        except Exception:
            pass
    return str(obj)


class Protocol:
    """往 stdout 写协议事件：一行一个 JSON，加锁，每行立即写出。"""

    def __init__(self, fd: int | None):
        self.fd = fd
        self._lock = threading.Lock()

    def emit(self, event: str, **fields):
        obj = {"event": event, **fields}
        try:
            line = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            line = json.dumps(_sanitize(obj), ensure_ascii=False, allow_nan=False)
        data = (line + "\n").encode("utf-8", errors="replace")
        if self.fd is None:
            return
        with self._lock:
            try:
                view = memoryview(data)
                while view:
                    n = os.write(self.fd, view)
                    view = view[n:]
            except OSError as e:
                # 界面那头已经把管道关了：没人听了，直接退出
                log("协议输出管道已关闭，退出：", e)
                _hard_exit(0)


# ======================================================================
#  配置：缺的字段用默认值（docs/protocol.md 第 3 节），类型不对的用默认值并提示
# ======================================================================
def default_log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Kikitori", "字幕记录")


def default_config() -> dict:
    return {
        "translate": {
            "provider": "deepseek",
            "target_language": "zh-Hans",
            "context_lines": 4,
            "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                         "temperature": 1.3},
            "ollama": {"base_url": "http://localhost:11434", "model": "qwen3:8b", "temperature": 0.3},
            "openai_compatible": {"base_url": "", "api_key": "", "model": "", "temperature": 0.7},
        },
        "asr": {
            "model": "large-v3",
            "device": "cuda",
            "compute_type": "float16",
            "language": "ja",
            "beam_size": 5,
            "show_partial": True,
            "partial_interval": 1.0,
        },
        "vad": {"threshold": 0.5, "min_silence_ms": 500, "max_segment_s": 8.0, "min_speech_ms": 250},
        "audio": {"device": "", "file": ""},
        "download": {"source": "auto"},
        "glossary": {},
        "log_dir": default_log_dir(),
    }


def _coerce(default, value):
    """按默认值的类型转换；转不了抛 ValueError。"""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.strip().lower() in ("true", "1", "yes")
        raise ValueError
    if isinstance(default, int):
        if isinstance(value, bool):
            raise ValueError
        f = float(value)
        if not math.isfinite(f):
            raise ValueError
        return int(round(f))
    if isinstance(default, float):
        if isinstance(value, bool):
            raise ValueError
        f = float(value)
        if not math.isfinite(f):
            raise ValueError
        return f
    if isinstance(default, str):
        if isinstance(value, (dict, list)):
            raise ValueError
        return str(value)
    return value


def _merge(default, value, path: str, warnings: list):
    if isinstance(default, dict):
        if value is None:
            value = {}
        elif not isinstance(value, dict):
            warnings.append(f"设置项 {path} 格式不对，已使用默认值")
            value = {}
        out = {}
        for k, d in default.items():
            out[k] = _merge(d, value.get(k), f"{path}.{k}" if path else k, warnings)
        for k, v in value.items():       # 默认值里没有的字段原样保留（例如 deepseek.thinking）
            if k not in out:
                out[k] = v
        return out
    if value is None:
        return default
    try:
        return _coerce(default, value)
    except (TypeError, ValueError):
        warnings.append(f"设置项 {path} 的值「{str(value)[:40]}」无效，已使用默认值 {default}")
        return default


def normalize_config(raw: dict) -> tuple[dict, list[str]]:
    from kikitori.languages import normalize_source, normalize_target

    warnings: list[str] = []
    cfg = _merge(default_config(), raw or {}, "", warnings)

    # 专有名词表：任意 键 → 值
    g = cfg.get("glossary")
    if not isinstance(g, dict):
        warnings.append("设置项 glossary 格式不对，已忽略")
        g = {}
    cfg["glossary"] = {str(k).strip(): str(v).strip() for k, v in g.items() if str(k).strip()}

    a, t, v = cfg["asr"], cfg["translate"], cfg["vad"]
    lang = a["language"].strip().lower()
    a["language"] = normalize_source(lang)
    if a["language"] != lang:
        warnings.append(f"不支持的说话语言「{lang}」，已改用日语")
    tgt = t["target_language"].strip()
    t["target_language"] = normalize_target(tgt)
    if t["target_language"].lower() != tgt.lower():
        warnings.append(f"不支持的翻译目标语言「{tgt}」，已改用简体中文")
    t["provider"] = t["provider"].strip().lower() or "off"
    a["model"] = a["model"].strip() or "large-v3"
    a["device"] = a["device"].strip().lower() or "cuda"
    if a["device"] not in ("cuda", "cpu", "auto"):
        warnings.append(f"识别设备「{a['device']}」无效，已改用 cuda")
        a["device"] = "cuda"
    a["compute_type"] = a["compute_type"].strip() or "float16"
    d = cfg["download"]
    src = d["source"].strip().lower()
    d["source"] = src if src in ("auto", "official", "china") else "auto"
    if d["source"] != src:
        warnings.append(f"下载源「{src}」无效，已改用自动选择")
    a["beam_size"] = min(10, max(1, a["beam_size"]))
    a["partial_interval"] = min(10.0, max(0.3, a["partial_interval"]))
    t["context_lines"] = min(20, max(0, t["context_lines"]))
    v["threshold"] = min(0.99, max(0.05, v["threshold"]))
    v["min_silence_ms"] = max(50, v["min_silence_ms"])
    v["max_segment_s"] = max(1.0, v["max_segment_s"])
    v["min_speech_ms"] = max(0, v["min_speech_ms"])
    cfg["log_dir"] = cfg["log_dir"].strip() or default_log_dir()
    return cfg, warnings


# ======================================================================
#  服务端
# ======================================================================
TEST_SAMPLES = {
    "ja": "こんにちは、今日もよろしくね！",
    "en": "Hi everyone, thanks for coming to the stream today!",
}


def _engine_mod():
    import kikitori.engine as m

    return m


class _GatedSink:
    """加载模型时用：这次 start 已经被新的 start/stop 取代后，不再往外报状态（免得 stopped 之后又冒出 loading）。

    检查和发送在同一把锁里做（stop 也在这把锁里改代数），保证不会出现“先 stopped、后 loading”的顺序。
    """

    def __init__(self, sink, alive, lock):
        self._sink = sink
        self._alive = alive
        self._lock = lock

    def status(self, *a, **kw):
        with self._lock:
            if self._alive():
                self._sink.status(*a, **kw)

    def notice(self, *a, **kw):
        with self._lock:
            if self._alive():
                self._sink.notice(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._sink, name)


def make_protocol_sink(proto: Protocol):
    from kikitori.engine import EventSink

    class ProtocolSink(EventSink):
        def __init__(self):
            self.state = "idle"

        def partial(self, seg_id, text):
            proto.emit("partial", id=int(seg_id), text=text)

        def final(self, seg_id, text, lang, mode, ts):
            proto.emit("final", id=int(seg_id), text=text, lang=lang or "", mode=mode, ts=round(float(ts), 3))

        def translation(self, seg_id, text, ok, error=None, elapsed=None):
            f = {"id": int(seg_id), "text": text or "", "ok": bool(ok)}
            if not ok:
                f["error"] = error or "翻译失败"
            if elapsed is not None:
                f["elapsed"] = round(float(elapsed), 2)
            proto.emit("translation", **f)

        def remove(self, seg_id):
            proto.emit("remove", id=int(seg_id))

        def status(self, state, message, level="info", **extra):
            self.state = state
            f = {"state": state, "message": message, "level": level}
            if extra.get("progress") is not None:
                f["progress"] = round(min(1.0, max(0.0, float(extra["progress"]))), 3)
            log(f"状态 {state}：{message}" + (f" {f['progress']:.0%}" if "progress" in f else ""))
            proto.emit("status", **f)

        def notice(self, message, level="info"):
            log(f"提示（{level}）：{message}")
            proto.emit("notice", message=message, level=level)

        def level(self, rms):
            proto.emit("level", rms=round(float(rms), 4))

    return ProtocolSink()


class Server:
    def __init__(self, proto: Protocol):
        self.proto = proto
        self.sink = make_protocol_sink(proto)
        self._lock = threading.RLock()
        self._boot_lock = threading.Lock()   # 同一时间只有一个 start 在加载模型/开始录音
        self._gen = 0                        # 每次 start/stop 加一；旧的 start 发现自己过期了就放弃
        self.session = None                  # 当前的 Engine
        self._stopped = None                 # 最近一次 stop 掉的 Engine（下次 start 前等它的线程退出）
        self.asr = None                      # 已加载的 Whisper 模型（stop 后保留，下次 start 秒开）
        self.asr_key = None                  # (model, device, compute_type)
        self.last_cfg: dict | None = None
        self._id_lock = threading.Lock()
        self._next_id = 1                    # 句子编号：整个进程内单调递增，start/stop 不重置
        self._log_paths: dict[str, str] = {}
        self._exiting = False
        self._installer = None               # 正在下载显卡运行库的 RuntimeInstaller（stop 时中止）

    # ------------------------------------------------------------------
    def next_id(self) -> int:
        with self._id_lock:
            sid = self._next_id
            self._next_id += 1
            return sid

    def log_path(self, log_dir: str) -> str:
        # 同一个进程、同一个目录只用一个记录文件（多次 start/stop 都写进去）
        with self._lock:
            p = self._log_paths.get(log_dir)
            if p is None:
                p = os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".txt")
                self._log_paths[log_dir] = p
            return p

    def error(self, message: str):
        self.sink.notice(message, "error")

    @staticmethod
    def spawn(fn, *args, name: str):
        def run():
            try:
                fn(*args)
            except Exception as e:
                traceback.print_exc()
                try:
                    Server._current.sink.notice(f"引擎内部错误：{e}", "error")
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True, name=name).start()

    _current = None

    # ------------------------------------------------------------------
    def handle(self, msg):
        if not isinstance(msg, dict):
            self.error("命令格式不对：每行应该是一个 JSON 对象")
            return
        cmd = msg.get("cmd")
        handler = {
            "start": self.cmd_start,
            "stop": self.cmd_stop,
            "pause": self.cmd_pause,
            "set_languages": self.cmd_set_languages,
            "reconnect_audio": self.cmd_reconnect_audio,
            "list_devices": self.cmd_list_devices,
            "test_translation": self.cmd_test_translation,
            "shutdown": self.cmd_shutdown,
        }.get(cmd) if isinstance(cmd, str) else None
        if handler is None:
            self.error(f"未知命令：{cmd}")
            return
        log("收到命令：", cmd)
        handler(msg)

    # ---------------- start / stop ----------------
    def cmd_start(self, msg):
        raw = msg.get("config")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            self.error("start 命令的 config 必须是 JSON 对象")
            return
        cfg, warnings = normalize_config(raw)
        for w in warnings[:5]:
            self.sink.notice(w, "warn")
        with self._lock:
            self._gen += 1
            gen = self._gen
            old, self.session = self.session, None
            if old is None:                    # 刚 stop 过的会话，线程可能还没完全退出
                old, self._stopped = self._stopped, None
            self.last_cfg = cfg
        self._cancel_download()     # 正在下载运行库的话先中止（新的 start 会接着下）
        if old is not None:
            log("用新配置重启：先停止当前的监听")
            old.stop()
        self.spawn(self._boot, gen, cfg, old, name=f"boot-{gen}")

    def _alive(self, gen: int) -> bool:
        return gen == self._gen and not self._exiting

    def _boot(self, gen: int, cfg: dict, old=None):
        eng = _engine_mod()
        from kikitori.audio import FileSource, LoopbackCapture

        if old is not None:
            old.join(3.0)    # 等旧会话的线程退出：不和新会话抢录音设备，换模型时旧模型也能及时释放
            old = None
        with self._boot_lock:
            if not self._alive(gen):
                return
            gated = _GatedSink(self.sink, lambda: self._alive(gen), self._lock)
            try:
                import_asr_libs()          # 等后台预加载做完（不能两个线程同时导入）
            except Exception as e:
                traceback.print_exc()
                gated.status("error", f"语音识别组件加载失败：{e}", "error")
                return
            acfg = cfg["asr"]
            if acfg["device"] == "auto":
                from kikitori.cuda_runtime import has_nvidia_driver

                acfg["device"] = "cuda" if has_nvidia_driver() else "cpu"
                log(f"识别设备 auto → {acfg['device']}")
            key = (acfg["model"], acfg["device"], acfg["compute_type"])
            src_lang = acfg["language"]
            tgt_lang = cfg["translate"]["target_language"]
            hotwords = " ".join(cfg["glossary"].keys()) or None

            if self.asr is None or self.asr_key != key:
                # 显卡模式：先确认显卡运行库就绪（没有就下载）。CPU 模式不需要。
                if acfg["device"] == "cuda" and not self._ensure_cuda_runtime(gen, cfg, gated):
                    return
                if self.asr is not None:
                    log(f"模型参数变了 {self.asr_key} → {key}，重新加载")
                    self.asr, self.asr_key = None, None
                    import gc

                    gc.collect()   # 先把旧模型从显存里放掉
                try:
                    asr = eng.load_transcriber(acfg, src_lang, hotwords, gated,
                                               source=cfg["download"]["source"],
                                               cancelled=lambda: not self._alive(gen))
                except Exception as e:
                    if not self._alive(gen):
                        log(f"加载/下载模型时被中止：{type(e).__name__}")
                        return
                    traceback.print_exc()
                    gated.status("error", eng.model_error_message(e, acfg), "error")
                    return
                self.asr, self.asr_key = asr, key
            else:
                log("模型参数没变，复用已加载的模型")
            if not self._alive(gen):
                return

            afile = cfg["audio"]["file"].strip()
            if afile:
                path = os.path.abspath(afile)
                make_source = lambda q: FileSource(q, path)  # noqa: E731
            else:
                device = cfg["audio"]["device"]
                make_source = lambda q: LoopbackCapture(q, device, fallback_to_default=True)  # noqa: E731

            session = eng.Engine(
                cfg, self.sink, make_source,
                transcriber=self.asr, log_path=self.log_path(cfg["log_dir"]),
                source_lang=src_lang, target_lang=tgt_lang, id_gen=self.next_id,
            )
            with self._lock:
                if not self._alive(gen):
                    return
                self.session = session
            session.boot()

    def _ensure_cuda_runtime(self, gen: int, cfg: dict, gated) -> bool:
        """显卡模式用之前：确认 NVIDIA 运行库能用，没有就下载（docs/protocol.md 第 7.1 节）。失败时已经发了 status error。"""
        from kikitori import cuda_runtime, cuda_setup

        if not cuda_runtime.has_nvidia_driver():
            gated.status("error", "没有检测到 NVIDIA 显卡（或驱动），请在 设置 → 语音识别 里把设备改成 CPU", "error")
            return False
        kind, dirs = cuda_setup.find_cuda_dirs()
        if not kind:
            src_mode = cfg["download"]["source"]
            log(f"没有找到显卡运行库，开始下载到 {cuda_setup.runtime_dir()}（下载源：{src_mode}）")
            t0 = time.time()

            last = [time.monotonic()]

            def emit(message, progress):
                # 协议：下载期间 downloading_runtime 最多每 500ms 一次
                wait = 0.5 - (time.monotonic() - last[0])
                if wait > 0:
                    time.sleep(wait)
                last[0] = time.monotonic()
                gated.status("downloading_runtime", message, "info", progress=progress)

            def progress(done, total, source):
                emit(f"正在下载显卡运行库（只需一次）：{done / 2**20:.0f} / {total / 2**20:.0f} MB · {source}",
                     round(min(0.999, done / total), 3) if total else 0.0)

            gated.status("downloading_runtime", "正在准备下载显卡运行库（只需一次）…", "info", progress=0.0)
            inst = cuda_runtime.RuntimeInstaller(src_mode, progress)
            with self._lock:
                if not self._alive(gen):
                    inst.close()
                    return False
                self._installer = inst
            try:
                used = inst.run()
            except cuda_runtime.Cancelled:
                log("显卡运行库下载已中止（已下载的部分保留，下次接着下）")
                return False
            except cuda_runtime.InstallError as e:
                log("显卡运行库安装失败：", e)
                gated.status("error", str(e), "error")
                return False
            finally:
                with self._lock:
                    self._installer = None
                inst.close()
            log(f"显卡运行库下载完成（{time.time() - t0:.0f}s，{used}）")
            emit(f"显卡运行库已就绪 · {used}", 1.0)
            kind, dirs = cuda_setup.find_cuda_dirs()
            if not kind:
                gated.status("error", "显卡运行库安装后检查不通过，请重试，或者改用 CPU", "error")
                return False
        cuda_setup.add_cuda_dll_dirs()
        err = cuda_setup.preload_cuda_dlls()
        if err:
            gated.status("error", f"显卡运行库加载失败：{err}。请重新启动程序后重试，或者改用 CPU", "error")
            return False
        log(f"显卡运行库：{kind} {dirs[0]}{' 等' if len(dirs) > 1 else ''}")
        return True

    def _cancel_download(self):
        inst = self._installer
        if inst is not None:
            log("中止正在进行的显卡运行库下载")
            inst.cancel()

    def cmd_stop(self, msg=None):
        with self._lock:
            self._gen += 1
            s, self.session = self.session, None
            if s is not None:
                self._stopped = s
        self._cancel_download()
        if s is not None:
            s.stop()
        self.sink.status("stopped", "已停止")

    # ---------------- 运行中的控制 ----------------
    def cmd_pause(self, msg):
        v = msg.get("value")
        if not isinstance(v, bool):
            self.error("pause 命令需要 value: true 或 false")
            return
        with self._lock:
            s = self.session
        if s is None:
            log("还没有开始监听，忽略 pause")
            return
        if s.set_paused(v):
            s.emit_state()          # paused 或 listening（按设置后的真实状态）

    def cmd_set_languages(self, msg):
        from kikitori.languages import (SOURCE_LANGS, TARGET_LANGS, source_menu_name,
                                         target_menu_name)

        src, tgt = msg.get("source"), msg.get("target")
        if src is None and tgt is None:
            self.error("set_languages 命令需要 source 或 target")
            return
        if src is not None:
            codes = {c for c, _, _ in SOURCE_LANGS}
            if not isinstance(src, str) or src.strip().lower() not in codes:
                self.error(f"不支持的说话语言：{src}")
                return
            src = src.strip().lower()
        if tgt is not None:
            codes = {c.lower(): c for c, _, _ in TARGET_LANGS}
            if not isinstance(tgt, str) or tgt.strip().lower() not in codes:
                self.error(f"不支持的翻译目标语言：{tgt}")
                return
            tgt = codes[tgt.strip().lower()]

        with self._lock:
            s = self.session
            cfg = self.last_cfg
            if cfg is not None:      # 记下来：“重新连接”重启时也用新语言
                if src:
                    cfg["asr"]["language"] = src
                if tgt:
                    cfg["translate"]["target_language"] = tgt
        if s is None:
            cur_src = src or (cfg["asr"]["language"] if cfg else "ja")
            cur_tgt = tgt or (cfg["translate"]["target_language"] if cfg else "zh-Hans")
            self.sink.notice(f"已切换：{source_menu_name(cur_src)} → {target_menu_name(cur_tgt)}", "info")
            return
        s.set_languages(src or s.src_lang, tgt or s.tgt_lang)
        if s is self.session:
            s.emit_state()          # 重发真实状态：暂停中发 paused，否则 listening（message 带新语言）

    def cmd_reconnect_audio(self, msg):
        with self._lock:
            s, cfg, state = self.session, self.last_cfg, self.sink.state
        if s is not None and s.running:
            def run():
                if s.reconnect() and s is self.session:
                    s.emit_state()  # 重发真实状态：暂停中发 paused，否则 listening（message 带新设备）

            self.spawn(run, name="reconnect")
        elif state == "error" and cfg is not None:
            # 上次没启动成功（比如没有可录音的设备）：用上次的配置重新开始
            log("上次启动失败，按上次的配置重新开始")
            self.cmd_start({"config": cfg})
        else:
            self.sink.notice("还没有开始监听", "warn")

    # ---------------- 其它 ----------------
    def cmd_list_devices(self, msg):
        def run():
            from kikitori.audio import list_loopback_devices

            try:
                devs, default = list_loopback_devices()
            except Exception as e:
                traceback.print_exc()
                self.proto.emit("devices", devices=[])
                self.error(f"无法列出播放设备：{e}")
                return
            out = []
            for d in devs:
                out.append({
                    "name": d["name"].replace(" [Loopback]", ""),
                    "is_default": bool(default) and d.get("index") == default.get("index"),
                })
            self.proto.emit("devices", devices=out)

        self.spawn(run, name="list-devices")

    def cmd_test_translation(self, msg):
        raw = msg.get("config")
        if not isinstance(raw, dict):
            self.error("test_translation 命令需要 config（JSON 对象）")
            return
        self.spawn(self._test_translation, raw, name="test-translation")

    def _test_translation(self, raw: dict):
        from kikitori.translator import Translator, TranslatorError

        t0 = time.time()
        cfg, _ = normalize_config(raw)
        target = cfg["translate"]["target_language"]
        src_lang = "en" if target == "ja" else "ja"
        text = TEST_SAMPLES[src_lang]

        def result(ok: bool, message: str, output: str = "", elapsed: float | None = None):
            el = round(elapsed if elapsed is not None else time.time() - t0, 2)
            log(f"翻译测试：ok={ok} {message} {output}")
            self.proto.emit("translation_test", ok=ok, message=message, input=text, output=output, elapsed=el)

        tr = Translator(cfg, cfg["glossary"], src_lang, target)
        if not tr.enabled:
            result(False, "翻译已关闭（翻译引擎选的是“不翻译”）")
            return
        try:
            err = tr.check()
            if err:
                result(False, err)
                return
            t1 = time.time()
            out = tr.translate(text, [])
            if not out.strip():
                result(False, "翻译服务返回了空结果，请检查模型名", elapsed=time.time() - t1)
            else:
                result(True, "连接正常", out, elapsed=time.time() - t1)
        except TranslatorError as e:
            result(False, str(e))
        except Exception as e:
            traceback.print_exc()
            result(False, f"测试失败：{e}")
        finally:
            tr.close()

    def cmd_shutdown(self, msg=None):
        log("收到 shutdown，退出")
        self.exit(0)

    def exit(self, code: int = 0):
        if self._exiting:
            return
        self._exiting = True
        # 兜底：不管下面卡在哪里，1.5 秒后强制退出（协议要求 stdin 关闭后 2 秒内退出）
        t = threading.Timer(1.5, _hard_exit, args=(code,))
        t.daemon = True
        t.start()
        try:
            with self._lock:
                self._gen += 1
                s, self.session = self.session, None
            self._cancel_download()
            if s is not None:
                s.stop()
        except Exception:
            traceback.print_exc()
        _hard_exit(code)


# ======================================================================
_LIBS_LOCK = threading.Lock()
_libs_ready = False


def import_asr_libs():
    """导入识别用的重型库（faster-whisper / CTranslate2 / onnxruntime / huggingface_hub），失败时抛异常。

    必须加锁、整组一起导入：后台预加载线程和 start 线程同时导入这些互相引用的模块时，
    Python 会检测到“导入死锁”并在其中一个线程里抛 _DeadlockError（打包版导入慢，很容易碰上）。
    """
    global _libs_ready
    with _LIBS_LOCK:
        if _libs_ready:
            return
        t = time.time()
        import faster_whisper  # noqa: F401
        import faster_whisper.utils  # noqa: F401
        import faster_whisper.vad  # noqa: F401
        import huggingface_hub  # noqa: F401
        import onnxruntime  # noqa: F401
        import tqdm.auto  # noqa: F401

        _libs_ready = True
        log(f"识别库导入完成（{time.time() - t:.1f}s）")


def _preload():
    """后台提前导入识别库（打包版要好几秒），让第一次 start 快一点。"""
    try:
        import_asr_libs()
    except Exception:
        log("识别库导入失败（start 时会再试一次并报错）：")
        traceback.print_exc()


def main():
    proto = Protocol(_PROTO_FD)
    try:
        # 已经有的显卡运行库先加进 DLL 搜索路径（没有也没关系：import ctranslate2 / faster_whisper 不需要它，
        # 第一次用显卡之前 _ensure_cuda_runtime 会下载并再加一次）
        from kikitori.cuda_setup import add_cuda_dll_dirs, find_cuda_dirs, runtime_dir

        dirs = add_cuda_dll_dirs()
        kind = find_cuda_dirs()[0]
        log(f"Kikitori 引擎 {VERSION}（Python {sys.version.split()[0]}"
            f"{'，打包版' if getattr(sys, 'frozen', False) else ''}），显卡运行库："
            + (f"{kind}（{len(dirs)} 个目录）" if kind else f"还没有（首次用显卡时下载到 {runtime_dir()}）"))
        import kikitori.audio  # noqa: F401
        import kikitori.engine  # noqa: F401
        try:   # 很轻，先导入：免得 list_devices 和 start 在两个线程里同时导入（失败了到用的时候再报错）
            import pyaudiowpatch  # noqa: F401
        except Exception:
            traceback.print_exc()

        server = Server(proto)
        Server._current = server
    except BaseException as e:
        traceback.print_exc()
        proto.emit("fatal", message=f"引擎启动失败：{e}")
        _hard_exit(1)

    def thread_excepthook(args):
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        if args.exc_type is not SystemExit:
            server.sink.notice(f"引擎内部错误：{args.exc_value}", "error")

    threading.excepthook = thread_excepthook

    proto.emit("hello", version=VERSION, protocol=PROTOCOL)
    server.sink.status("idle", "引擎已就绪")
    threading.Thread(target=_preload, daemon=True, name="preload").start()

    stdin = sys.stdin.buffer if sys.stdin is not None else None
    try:
        while stdin is not None:
            try:
                raw = stdin.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").lstrip("﻿").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                server.error(f"无法解析的命令：{line[:80]}")
                continue
            try:
                server.handle(msg)
            except Exception as e:
                traceback.print_exc()
                server.error(f"处理命令时出错：{e}")
    except KeyboardInterrupt:       # 开发时在控制台按了 Ctrl+C
        log("Ctrl+C，退出")
        server.exit(0)
    except BaseException as e:      # 理论上走不到这里
        traceback.print_exc()
        proto.emit("fatal", message=f"引擎出现无法恢复的错误：{e}")
        _hard_exit(1)

    log("stdin 已关闭（界面退出了），引擎退出")
    server.exit(0)


if __name__ == "__main__":
    main()
