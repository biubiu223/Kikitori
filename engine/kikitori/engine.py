"""把各部分串起来：

  音频线程 ──► 断句(VAD) ──► 识别线程(Whisper, GPU) ──► 翻译线程池(LLM) ──► 事件接收方(sink)
                              │ 正式句优先，草稿只保留最新的

引擎不直接操作界面，所有输出都通过 EventSink 的方法发出去；
engine_server.py 把这些事件写成 JSON Lines 发给 WinUI 界面（见 docs/protocol.md）。
"""

import math
import os
import queue
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from .asr import Transcriber
from .languages import same_language, source_menu_name, target_menu_name
from .translator import Translator, TranslatorError
from .vad import Segment, Segmenter

# final 事件的 mode
MODE_TRANSLATING = "translating"         # 译文稍后由 translation 事件给出
MODE_NO_TRANSLATION = "no_translation"   # 翻译关闭或配置有误，只显示原文
MODE_SAME_LANGUAGE = "same_language"     # 说的就是目标语言，原文直接当译文显示

LEVEL_INTERVAL = 0.2      # 音量事件最多每 0.2 秒一次
LEVEL_SILENT_AFTER = 0.5  # 超过 0.5 秒没有音频数据就报一次 0
IDLE_FLUSH_AFTER = 0.4    # 超过 0.4 秒没有音频数据就把说到一半的句子结束掉


class EventSink:
    """引擎 → 界面 的事件接口（默认什么都不做）。

    各方法会在引擎的各个后台线程里被调用，实现必须线程安全。
    """

    def partial(self, seg_id: int, text: str):
        """说话过程中的原文草稿（同一个 id 会多次刷新）。"""

    def final(self, seg_id: int, text: str, lang: str | None, mode: str, ts: float):
        """一句话识别完成。mode 见 MODE_*，ts 为这句开始说的时间（Unix 秒）。"""

    def translation(self, seg_id: int, text: str, ok: bool, error: str | None = None,
                    elapsed: float | None = None):
        """译文（ok=False 时 text 为空，error 是原因）。"""

    def remove(self, seg_id: int):
        """这句最终被判定为噪音/空白，删掉对应的草稿。"""

    def status(self, state: str, message: str, level: str = "info", **extra):
        """引擎状态：downloading_model / loading_model / warming_up / listening / paused / stopped / error …
        extra 里可以有 progress（0~1，下载进度）。"""

    def notice(self, message: str, level: str = "info"):
        """临时提示（level：info / warn / error）。"""

    def level(self, rms: float):
        """音量（16k 单声道音频的 RMS，0~1）。"""


# ======================================================================
#  模型下载 / 加载
# ======================================================================
# faster-whisper 需要的文件（和 faster_whisper.utils.download_model 一致）
_MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def _repo_id(name: str) -> str:
    if "/" in name:
        return name
    from faster_whisper.utils import _MODELS

    repo = _MODELS.get(name)
    if repo is None:
        raise ValueError(f"不认识的模型名「{name}」（可用：large-v3、large-v3-turbo，或 HuggingFace 上的模型 ID）")
    return repo


class _NullWriter:
    def write(self, s):
        return len(s)

    def flush(self):
        pass


class ModelDownloadCancelled(Exception):
    """下载模型时收到 stop / shutdown。"""


class _TooSlow(Exception):
    """auto 模式下官方源太慢，换镜像。"""


# 识别模型的下载源（docs/protocol.md 第 7.2 节）
HF_ENDPOINTS = {"official": ("huggingface.co", "https://huggingface.co"),
                "china": ("hf-mirror.com", "https://hf-mirror.com")}
SLOW_AFTER_S = 20.0          # auto 模式：官方源下载 20 秒后平均速度还低于 300KB/s 就换镜像
SLOW_BYTES_PER_S = 300 << 10


def _hf_endpoints(mode: str, repo: str) -> list[tuple[str, str, str]]:
    """按下载源设置排出要尝试的 (名字, 地址, 分组)。auto：先试官方，连不上就把镜像排前面。"""
    off = (*HF_ENDPOINTS["official"], "official")
    cn = (*HF_ENDPOINTS["china"], "china")
    if mode == "official":
        return [off]
    if mode == "china":
        return [cn]
    try:
        import httpx

        r = httpx.get(f"{off[1]}/api/models/{repo}", timeout=httpx.Timeout(6.0, connect=5.0))
        if r.status_code < 500:
            return [off, cn]
        print(f"[asr] {off[0]} 返回 {r.status_code}，改用镜像")
    except Exception as e:
        print(f"[asr] 连不上 {off[0]}（{type(e).__name__}），改用镜像 {cn[0]}")
    return [cn, off]


def _expected_download_size(repo: str, endpoint: str) -> int:
    """模型文件总大小（字节），用来算下载进度。查不到返回 0。"""
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.utils import filter_repo_objects

        info = HfApi(endpoint=endpoint).model_info(repo, files_metadata=True)
        names = [s.rfilename for s in info.siblings or []]
        keep = set(filter_repo_objects(names, allow_patterns=_MODEL_FILES))
        return sum((s.size or 0) for s in info.siblings or [] if s.rfilename in keep)
    except Exception as e:
        print("[asr] 查不到模型大小（不显示下载进度）：", e)
        return 0


def _progress_tqdm(on_progress, expected_total: int, cancelled=None, too_slow_check: bool = False):
    """造一个 tqdm 子类交给 huggingface_hub：不往任何地方输出进度条，只把“已下载字节数”报告给 on_progress。

    下载线程每收到一块数据都会调 update()：在这里检查 cancelled()（stop/shutdown）和“官方源太慢”，
    抛异常就能让 huggingface_hub 中止下载（已下载的部分留在缓存里，下次接着下）。
    """
    from tqdm.auto import tqdm as base_tqdm

    bars = []
    lock = threading.Lock()
    state = {"last": 0.0, "best": 0.0, "sent": 0.0, "t0": time.monotonic(), "abort": None}

    def check_abort(bar):
        if state["abort"] is not None:
            raise state["abort"]
        if cancelled is not None and cancelled():
            state["abort"] = ModelDownloadCancelled()
            raise state["abort"]
        if too_slow_check and getattr(bar, "unit", "") == "B":
            dt = time.monotonic() - state["t0"]
            if dt > SLOW_AFTER_S and bar.n / dt < SLOW_BYTES_PER_S:
                state["abort"] = _TooSlow(f"{bar.n / dt / 1024:.0f} KB/s")
                raise state["abort"]

    def report():
        now = time.monotonic()
        with lock:
            if now - state["last"] < 1.0:
                return
            state["last"] = now
            byte_bars = [b for b in bars if getattr(b, "unit", "") == "B"]
            # “Reconstructing”那条是写到硬盘的字节数（最准）；没有的话用别的按字节计的进度条
            pick = ([b for b in byte_bars if "Reconstruct" in str(getattr(b, "desc", ""))]
                    or [b for b in byte_bars if "Downloading bytes" not in str(getattr(b, "desc", ""))]
                    or byte_bars)
            if not pick:
                return
            bar = pick[0]
            total = expected_total or (bar.total or 0)
            if total <= 0:
                return
            frac = max(state["best"], min(0.999, float(bar.n) / total))
            state["best"] = frac
            if frac - state["sent"] < 0.005:      # 没什么变化就不报
                return
            state["sent"] = frac
        try:
            on_progress(frac)
        except Exception:
            traceback.print_exc()

    class ProgressTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            kwargs["file"] = _NullWriter()
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)
            with lock:
                bars.append(self)

        def update(self, n=1):
            r = super().update(n)
            check_abort(self)
            report()
            return r

    return ProgressTqdm


def resolve_model(name: str, sink: EventSink, source: str = "auto", cancelled=None) -> str:
    """返回模型所在的本地目录；本地还没有就先下载（通过 sink 报告 downloading_model 和进度）。

    source：下载源 "auto" / "official"（huggingface.co）/ "china"（hf-mirror.com）。
    cancelled：可选，返回 True 时中止下载（抛 ModelDownloadCancelled）。
    """
    if os.path.isdir(name):
        return name
    repo = _repo_id(name)
    import huggingface_hub
    import huggingface_hub.constants as hf_constants

    try:
        # 已经下载过：直接用本地缓存，不联网（也就不会因为网络不好卡在启动上）
        return huggingface_hub.snapshot_download(repo, allow_patterns=_MODEL_FILES, local_files_only=True)
    except Exception:
        pass

    # 不用 Xet 协议，统一走普通 HTTP：镜像站不一定支持 Xet，而且这样进度回调里能中止下载
    hf_constants.HF_HUB_DISABLE_XET = True
    sink.status("downloading_model", f"正在准备下载语音识别模型 {name}…", "info", progress=0.0)
    endpoints = _hf_endpoints(source, repo)
    last_err: Exception | None = None
    for i, (label, endpoint, group) in enumerate(endpoints):
        if cancelled is not None and cancelled():
            raise ModelDownloadCancelled()
        total = _expected_download_size(repo, endpoint)
        if total >= 1 << 30:
            size = f"约 {total / (1 << 30):.1f}GB"
        elif total > 0:
            size = f"约 {total / (1 << 20):.0f}MB"
        else:
            size = "约 1.5–3GB"
        msg = f"正在下载语音识别模型 {name}（{size}，只需下载一次）· {label}"
        print("[asr]", msg)
        sink.status("downloading_model", msg, "info", progress=0.0)
        tqdm_class = _progress_tqdm(
            lambda p, msg=msg: sink.status("downloading_model", msg, "info", progress=round(p, 3)), total,
            cancelled=cancelled,
            # auto 模式下，官方源太慢就换镜像（只在后面还有别的源时）
            too_slow_check=(source == "auto" and group == "official" and i + 1 < len(endpoints)))
        t = time.time()
        try:
            path = huggingface_hub.snapshot_download(repo, allow_patterns=_MODEL_FILES, tqdm_class=tqdm_class,
                                                     endpoint=endpoint)
        except ModelDownloadCancelled:
            print("[asr] 模型下载已中止（下次接着下）")
            raise
        except _TooSlow as e:
            print(f"[asr] {label} 太慢（{e}），换下一个下载源")
            last_err = e
            continue
        except Exception as e:
            if cancelled is not None and cancelled():
                raise ModelDownloadCancelled() from e
            print(f"[asr] 从 {label} 下载失败：{type(e).__name__}: {e}")
            last_err = e
            continue
        print(f"[asr] 模型 {name} 下载完成（{time.time() - t:.0f}s，{label}）：{path}")
        sink.status("downloading_model", msg, "info", progress=1.0)
        return path
    names = "、".join(e[0] for e in endpoints)
    raise RuntimeError(f"下载失败（已尝试 {names}；请检查网络连接，或者在 设置 → 语音识别 里切换下载源，"
                       f"下次启动会接着下载）：{last_err}")


def load_transcriber(acfg: dict, language: str, hotwords: str | None, sink: EventSink,
                     source: str = "auto", cancelled=None) -> Transcriber:
    """（需要时先下载）加载 Whisper 模型并预热，过程通过 sink.status 报告。失败时抛异常。"""
    name = str(acfg.get("model", "large-v3"))
    device = str(acfg.get("device", "cuda"))
    compute_type = str(acfg.get("compute_type", "float16"))
    path = resolve_model(name, sink, source, cancelled)
    sink.status("loading_model", "正在加载语音识别模型…")
    asr = Transcriber(
        model=path,
        device=device,
        compute_type=compute_type,
        language=language,
        beam_size=int(acfg.get("beam_size", 5)),
        hotwords=hotwords,
        label=name,
    )
    sink.status("warming_up", "正在预热显卡…" if device == "cuda" else "正在预热语音识别模型…")
    asr.warmup()
    return asr


def model_error_message(e: Exception, acfg: dict) -> str:
    msg = f"语音识别模型加载失败：{e}"
    low = str(e).lower()
    if str(acfg.get("device", "cuda")) == "cuda" and any(k in low for k in ("cuda", "cublas", "cudnn", "gpu", "device")):
        msg += "（如果电脑没有 NVIDIA 显卡，请把识别设备改成 CPU）"
    return msg


# ======================================================================
#  引擎
# ======================================================================
class Engine:
    """一次“监听会话”：录音 → 断句 → 识别 → 翻译。

    transcriber 是已经加载好的模型（由 engine_server 加载，多次 start/stop 之间复用）。
    """

    def __init__(self, cfg: dict, sink: EventSink, make_source, transcriber: Transcriber, log_path: str,
                 source_lang: str = "ja", target_lang: str = "zh-Hans", id_gen=None):
        self.cfg = cfg
        self.sink = sink
        self.make_source = make_source
        self.src_lang = source_lang     # 说话语言（"auto" = 自动识别）
        self.tgt_lang = target_lang     # 翻译成什么语言

        self.glossary = {str(k): str(v) for k, v in (cfg.get("glossary") or {}).items() if str(k).strip()}
        self.translator = Translator(cfg, self.glossary, source_lang, target_lang)
        self.translate_on = False
        self.translate_error: str | None = None
        self.context_lines = max(0, min(10, int(cfg.get("translate", {}).get("context_lines", 4))))

        self.audio_q: "queue.Queue" = queue.Queue()
        self.source = None
        self.asr: Transcriber = transcriber
        self.segmenter: Segmenter | None = None
        self._id_gen = id_gen

        self.paused = False
        self.running = False            # 已经开始录音（boot 成功）
        self._stop = threading.Event()
        self._lock = threading.Lock()   # 保护 source / running / paused 和 stop 之间的先后顺序
        self._threads: list[threading.Thread] = []

        # 识别任务：正式句排队，草稿只留最新一个
        self._cv = threading.Condition()
        self._finals: deque[Segment] = deque()
        self._partial: Segment | None = None
        self._finalized: deque[int] = deque(maxlen=200)
        self._open_partials: set[int] = set()   # 发过草稿、还没出正式结果的句子（只在识别线程里改）

        # 翻译
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translate")
        self._hist_lock = threading.Lock()
        self._history: dict[int, dict] = {}   # seg_id -> {src, dst, ts}

        # 同样的错误提示 10 秒内只发一次（比如断网时每句都翻译失败）
        self._notice_lock = threading.Lock()
        self._notice_last: dict[str, float] = {}

        # 字幕记录
        self._log_path = log_path
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------
    def boot(self) -> bool:
        """同步执行：检查翻译配置 → 开始录音。成功开始监听返回 True。"""
        try:
            return self._boot()
        except Exception as e:
            traceback.print_exc()
            self.sink.status("error", f"引擎启动失败：{e}", "error")
            self.stop()
            return False

    def _boot(self) -> bool:
        acfg = self.cfg.get("asr", {})
        vcfg = self.cfg.get("vad", {})
        # 复用已经加载好的模型，只换这次的识别参数
        self.asr.language = self.src_lang
        self.asr.beam_size = int(acfg.get("beam_size", 5))
        self.asr.hotwords = " ".join(self.glossary.keys()) or None
        if self._stop.is_set():
            return False

        # 翻译配置检查
        err = self.translator.check()
        self.translate_error = err
        self.translate_on = self.translator.enabled and err is None
        if err:
            print("[translate]", err)
        if self._stop.is_set():
            return False

        self.segmenter = Segmenter(
            self._on_segment,
            threshold=float(vcfg.get("threshold", 0.5)),
            min_silence_ms=int(vcfg.get("min_silence_ms", 500)),
            max_segment_s=float(vcfg.get("max_segment_s", 8.0)),
            min_speech_ms=int(vcfg.get("min_speech_ms", 250)),
            partial_interval=float(acfg.get("partial_interval", 1.0)),
            emit_partial=bool(acfg.get("show_partial", True)),
            id_gen=self._id_gen,
        )

        for target, name in ((self._asr_loop, "asr"), (self._audio_loop, "audio")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)

        try:
            src = self.make_source(self.audio_q)
            with self._lock:
                if self._stop.is_set():
                    return False
                self.source = src
            src.start()
        except Exception as e:
            traceback.print_exc()
            self.sink.status("error", f"无法录制系统声音：{e}", "error")
            self.stop()
            return False

        with self._lock:
            if self._stop.is_set():
                src.stop()
                return False
            self.running = True
            msg = self.listening_message()
            print("[engine]", msg)
            if self.paused:
                self.sink.status("paused", self.paused_message())
            else:
                self.sink.status("listening", msg)
        if getattr(src, "warning", ""):
            self.sink.notice(src.warning, "warn")
        if err:
            self.sink.notice(f"{err}（目前只显示原文）", "error")
        return True

    # ------------------------------------------------------------------
    def _audio_loop(self):
        # segmenter 只在这个线程里被调用，避免多线程同时改它的状态
        idle_flushed = True
        now = time.monotonic()
        last_audio = now
        lv_sum, lv_n, lv_t = 0.0, 0, now
        silent_sent = False
        while not self._stop.is_set():
            try:
                chunk = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                idle = time.monotonic() - last_audio
                # 没有声音进来了（暂停了视频/直播卡了）：把说到一半的句子结束掉
                if idle >= IDLE_FLUSH_AFTER and not idle_flushed:
                    self._safe_flush()
                    idle_flushed = True
                if idle >= LEVEL_SILENT_AFTER and not silent_sent and not self._stop.is_set():
                    self.sink.level(0.0)
                    silent_sent = True
                    lv_sum, lv_n = 0.0, 0
                continue

            now = time.monotonic()
            last_audio = now
            silent_sent = False
            # 音量（暂停时也照常报告：录音没停）
            if chunk.size:
                lv_sum += float(np.dot(chunk, chunk))
                lv_n += chunk.size
            if lv_n and now - lv_t >= LEVEL_INTERVAL:
                rms = math.sqrt(lv_sum / lv_n)
                if not math.isfinite(rms):
                    rms = 0.0
                if not self._stop.is_set():
                    self.sink.level(round(min(1.0, rms), 4))
                lv_sum, lv_n, lv_t = 0.0, 0, now

            if self.paused:
                # 刚暂停：把说到一半的那句收尾，之后的声音全部丢掉
                if not idle_flushed:
                    self._safe_flush()
                    idle_flushed = True
                continue
            idle_flushed = False
            try:
                self.segmenter.feed(chunk)
            except Exception:
                traceback.print_exc()

    def _safe_flush(self):
        try:
            self.segmenter.flush()
        except Exception:
            traceback.print_exc()

    def _on_segment(self, seg: Segment):
        with self._cv:
            if seg.final:
                self._finals.append(seg)
                if self._partial is not None and self._partial.seg_id == seg.seg_id:
                    self._partial = None
            else:
                self._partial = seg
            self._cv.notify()

    def _asr_loop(self):
        try:
            while True:
                with self._cv:
                    while not self._finals and self._partial is None and not self._stop.is_set():
                        self._cv.wait(0.5)
                    if self._stop.is_set():
                        break
                    if self._finals:
                        seg = self._finals.popleft()
                    else:
                        seg, self._partial = self._partial, None
                try:
                    if seg.final:
                        self._handle_final(seg)
                    elif seg.seg_id not in self._finalized:
                        text, _ = self.asr.transcribe(seg.audio, final=False, speech_conf=seg.speech_conf)
                        if text and seg.seg_id not in self._finalized and not self._stop.is_set():
                            self._open_partials.add(seg.seg_id)
                            self.sink.partial(seg.seg_id, text)
                except Exception as e:
                    traceback.print_exc()
                    self._notice_throttled(f"识别出错：{e}", "error")
                    if seg.final and seg.seg_id in self._open_partials:
                        self._remove(seg.seg_id)
        finally:
            # 停止时还没出正式结果的草稿：通知界面删掉，免得一直挂在屏幕上
            for sid in sorted(self._open_partials):
                self.sink.remove(sid)
            self._open_partials.clear()

    def _remove(self, seg_id: int):
        # 只有界面见过（发过草稿）的句子才需要删
        if seg_id in self._open_partials:
            self._open_partials.discard(seg_id)
            self.sink.remove(seg_id)

    def _handle_final(self, seg: Segment):
        self._finalized.append(seg.seg_id)
        if seg.audio.size == 0:
            self._remove(seg.seg_id)
            return
        t0 = time.time()
        text, lang = self.asr.transcribe(seg.audio, final=True, speech_conf=seg.speech_conf)
        dur = seg.audio.size / 16000
        if not text:
            self._remove(seg.seg_id)
            return
        self._open_partials.discard(seg.seg_id)
        print(f"[asr] #{seg.seg_id} {dur:4.1f}s 音频 → {time.time() - t0:.2f}s  [{lang}] {text}")

        with self._hist_lock:
            self._history[seg.seg_id] = {"src": text, "dst": None, "ts": seg.started_at}
            if len(self._history) > 100:
                for k in sorted(self._history)[:-100]:
                    del self._history[k]

        if self.translate_on and same_language(lang, self.tgt_lang):
            # 说的就是目标语言（比如自动识别时主播说了句中文）：不用翻译，直接用大字显示
            with self._hist_lock:
                self._history[seg.seg_id]["dst"] = text
            self.sink.final(seg.seg_id, text, lang, MODE_SAME_LANGUAGE, seg.started_at)
            self._write_log(seg.started_at, text, None)
        elif self.translate_on:
            self.sink.final(seg.seg_id, text, lang, MODE_TRANSLATING, seg.started_at)
            try:
                self._pool.submit(self._translate, seg.seg_id, text, seg.started_at)
            except RuntimeError:   # 已经停止（线程池关了）
                self.sink.translation(seg.seg_id, "", False, error="已停止，没有翻译")
                self._write_log(seg.started_at, text, None)
        else:
            self.sink.final(seg.seg_id, text, lang, MODE_NO_TRANSLATION, seg.started_at)
            self._write_log(seg.started_at, text, None)

    def _translate(self, seg_id: int, src: str, ts: float):
        with self._hist_lock:
            prev = [self._history[k] for k in sorted(self._history) if k < seg_id]
        ctx = [(h["src"], h["dst"]) for h in prev[-self.context_lines:]] if self.context_lines > 0 else []
        t0 = time.time()
        dst, err = "", None
        try:
            dst = self.translator.translate(src, ctx)
        except TranslatorError as e:
            err = str(e)
        except Exception as e:
            traceback.print_exc()
            err = f"翻译出错：{e}"
        if err is None and not dst:
            err = "翻译服务返回了空结果"
        elapsed = time.time() - t0
        print(f"[tr]  #{seg_id} {elapsed:.2f}s  {dst or err}")
        with self._hist_lock:
            if seg_id in self._history:
                self._history[seg_id]["dst"] = dst
        if err:
            self.sink.translation(seg_id, "", False, error=err, elapsed=round(elapsed, 2))
            self._notice_throttled(err, "error")
        else:
            self.sink.translation(seg_id, dst, True, elapsed=round(elapsed, 2))
        self._write_log(ts, src, dst or "（翻译失败）")

    def _notice_throttled(self, message: str, level: str = "error", every: float = 10.0):
        now = time.monotonic()
        with self._notice_lock:
            if now - self._notice_last.get(message, -1e9) < every:
                return
            self._notice_last[message] = now
        self.sink.notice(message, level)

    def _write_log(self, ts: float, src: str, dst: str | None):
        line = f"[{datetime.fromtimestamp(ts).strftime('%H:%M:%S')}] {src}\n"
        if dst is not None:
            line += f"           {dst}\n"
        try:
            with self._log_lock:
                os.makedirs(os.path.dirname(os.path.abspath(self._log_path)), exist_ok=True)
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:   # 写记录失败不影响字幕
            print("[engine] 写字幕记录失败：", e)

    # ------------------------------------------------------------------
    def lang_desc(self) -> str:
        return f"{source_menu_name(self.src_lang)} → {target_menu_name(self.tgt_lang)}"

    def _state_detail(self) -> str:
        dev = self.source.device_name if self.source is not None else "?"
        if self.translate_on:
            tr = self.translator.describe()
        elif self.translator.enabled:
            tr = "翻译未开启（设置有误）"
        else:
            tr = "不翻译"
        return f"{dev} | {self.lang_desc()} | {tr}"

    def listening_message(self) -> str:
        return f"正在监听：{self._state_detail()}"

    def paused_message(self) -> str:
        return f"已暂停：{self._state_detail()}"

    def emit_state(self) -> bool:
        """重发当前的真实状态（暂停中 → paused，否则 → listening），message 带最新的设备/语言。
        还没开始监听（或已停止）时什么都不发，返回 False。"""
        with self._lock:
            if not self.running or self._stop.is_set():
                return False
            if self.paused:
                self.sink.status("paused", self.paused_message())
            else:
                self.sink.status("listening", self.listening_message())
            return True

    def set_languages(self, source: str, target: str):
        """运行中切换语言，马上对下一句生效，不用重启。"""
        self.src_lang, self.tgt_lang = source, target
        self.asr.language = source
        self.translator.set_languages(source, target)
        with self._hist_lock:
            self._history.clear()   # 旧语言的上文对新语言没用，反而会干扰
        print("[engine] 切换语言：", self.lang_desc())
        self.sink.notice(f"已切换：{self.lang_desc()}", "info")

    def set_paused(self, paused: bool) -> bool:
        """暂停/继续识别（录音不停，只是丢弃音频）。返回是否已经在监听。"""
        with self._lock:
            self.paused = paused
            return self.running

    def reconnect(self) -> bool:
        """重新连接音频设备（同步执行，调用方自己放到后台线程）。"""
        src = self.source
        if src is None:
            self.sink.notice("还没有开始录音", "warn")
            return False
        try:
            src.stop()
            src.start()
        except Exception as e:
            traceback.print_exc()
            with self._lock:
                self.running = False
            self.sink.status("error", f"重新连接失败：{e}", "error")
            return False
        with self._lock:
            if self._stop.is_set():
                src.stop()
                return False
            self.running = True
        print("[engine] 已重新连接：", src.device_name)
        self.sink.notice(f"已重新连接：{src.device_name}", "info")
        if getattr(src, "warning", ""):
            self.sink.notice(src.warning, "warn")
        return True

    def stop(self):
        with self._lock:
            self._stop.set()
            self.running = False
            src = self.source
        with self._cv:
            self._cv.notify_all()
        if src is not None:
            try:
                src.stop()
            except Exception:
                traceback.print_exc()
        # 已经在排队的翻译让它做完（界面上那几句的“…”会换成译文），不再接新的
        self._pool.shutdown(wait=False)

    def join(self, timeout: float = 3.0):
        """stop() 之后等录音/识别线程真正退出（最多 timeout 秒），好让旧模型能从显存里释放。"""
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
