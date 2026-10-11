"""人声检测 + 断句。

用 Silero VAD（faster-whisper 自带的 ONNX 模型）逐帧（32ms）判断有没有人声，
把连续的音频切成一句一句：
  - 停顿超过 min_silence_ms        → 这句话结束，送去识别 + 翻译（final）
  - 一句话太长超过 max_segment_s    → 在最像停顿的地方强制切开
  - 说话过程中每隔 partial_interval → 把目前为止的音频送去出“草稿”（partial）
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

SR = 16000
WIN = 512                 # Silero 每次处理 512 个采样 = 32ms
WIN_MS = WIN * 1000 / SR  # 32.0
CTX = 64                  # 模型需要的前文采样数


class StreamingVAD:
    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self.session = get_vad_model().session
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), "float32")
        self.c = np.zeros((1, 1, 128), "float32")
        self.ctx = np.zeros((1, CTX), "float32")

    def __call__(self, window: np.ndarray) -> float:
        x = np.concatenate([self.ctx, window[None, :]], axis=1)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = window[None, -CTX:]
        return float(np.asarray(out).reshape(-1)[0])


@dataclass
class Segment:
    seg_id: int
    audio: np.ndarray
    final: bool
    speech_conf: float = 0.0    # 这一段“像人声”的程度（人声概率的 75 分位），用来辅助过滤幻听
    started_at: float = 0.0     # 开始时间（time.time()），写日志用
    extra: dict = field(default_factory=dict)


class Segmenter:
    def __init__(
        self,
        on_segment: Callable[[Segment], None],
        threshold: float = 0.5,
        min_silence_ms: int = 500,
        max_segment_s: float = 8.0,
        min_speech_ms: int = 250,
        speech_pad_ms: int = 200,
        partial_interval: float = 1.0,
        emit_partial: bool = True,
        id_gen: Callable[[], int] | None = None,
    ):
        import time

        self._time = time.time
        # 句子编号：默认从 1 开始；引擎进程可以传入共享的计数器，让编号在多次 start/stop 之间一直递增
        self._id_gen = id_gen
        self.vad = StreamingVAD()
        self.on_segment = on_segment
        self.thr = threshold
        self.neg_thr = max(0.05, threshold - 0.15)
        self.min_silence_w = max(1, int(min_silence_ms / WIN_MS))
        self.max_seg_w = max(8, int(max_segment_s * 1000 / WIN_MS))
        self.min_speech_w = max(1, int(min_speech_ms / WIN_MS))
        self.pad_w = max(0, int(speech_pad_ms / WIN_MS))
        self.partial_w = max(4, int(partial_interval * 1000 / WIN_MS))
        self.emit_partial = emit_partial

        self._pending = np.zeros(0, "float32")
        self._preroll: deque = deque(maxlen=self.pad_w + 1)
        self._next_id = 1
        self._reset_speech()

    # ------------------------------------------------------------------
    def _new_id(self) -> int:
        if self._id_gen is not None:
            return self._id_gen()
        sid = self._next_id
        self._next_id += 1
        return sid

    def _reset_speech(self):
        self.in_speech = False
        self._wins: list[np.ndarray] = []
        self._probs: list[float] = []
        self._silence = 0
        self._last_partial_len = 0
        self._started_at = 0.0
        self._cur_id = 0

    def _start_speech(self):
        self.in_speech = True
        self._wins = [w for w, _ in self._preroll]
        self._probs = [p for _, p in self._preroll]
        self._silence = 0
        self._last_partial_len = 0
        self._started_at = self._time() - len(self._wins) * WIN_MS / 1000
        self._cur_id = self._new_id()

    # ------------------------------------------------------------------
    def feed(self, samples: np.ndarray):
        """喂入任意长度的 16k 单声道音频。"""
        if samples.size == 0:
            return
        buf = np.concatenate([self._pending, samples]) if self._pending.size else samples
        n = (buf.size // WIN) * WIN
        for i in range(0, n, WIN):
            self._process_window(buf[i : i + WIN])
        self._pending = buf[n:].copy()

    def _process_window(self, w: np.ndarray):
        p = self.vad(w)

        if not self.in_speech:
            self._preroll.append((w, p))
            if p >= self.thr:
                self._start_speech()   # preroll 里已经包含当前这一帧
            return

        self._wins.append(w)
        self._probs.append(p)
        self._silence = self._silence + 1 if p < self.neg_thr else 0

        if self._silence >= self.min_silence_w:
            # 一句话说完了：去掉多余的尾部静音，只留 pad
            keep = len(self._wins) - self._silence + self.pad_w
            self._emit_final(self._wins[:keep], self._probs[:keep])
            self._reset_speech()
            self._preroll.clear()
            return

        if len(self._wins) >= self.max_seg_w:
            # 太长了：在后 40% 里找人声概率最低的地方切开，剩下的作为新一句继续
            lo = int(len(self._wins) * 0.6)
            cut = lo + int(np.argmin(self._probs[lo:]))
            cut = max(cut, 1)
            rest_w, rest_p = self._wins[cut:], self._probs[cut:]
            self._emit_final(self._wins[:cut], self._probs[:cut])
            self._reset_speech()
            self.in_speech = True
            self._wins, self._probs = rest_w, rest_p
            self._started_at = self._time() - len(rest_w) * WIN_MS / 1000
            self._cur_id = self._new_id()
            return

        if self.emit_partial and len(self._wins) - self._last_partial_len >= self.partial_w:
            self._last_partial_len = len(self._wins)
            voiced = sum(1 for x in self._probs if x >= self.thr)
            if voiced >= self.min_speech_w:
                self.on_segment(
                    Segment(self._cur_id, np.concatenate(self._wins), False,
                            float(np.percentile(self._probs, 75)), self._started_at)
                )

    def _emit_final(self, wins, probs):
        voiced = sum(1 for x in probs if x >= self.thr)
        if not wins or voiced < self.min_speech_w:
            # 太短（咳嗽、点击声），丢弃；但如果之前发过草稿，要通知界面删掉
            if self._last_partial_len:
                self.on_segment(Segment(self._cur_id, np.zeros(0, "float32"), True, 0.0, self._started_at))
            return
        self.on_segment(
            Segment(self._cur_id, np.concatenate(wins), True, float(np.percentile(probs, 75)), self._started_at)
        )

    def flush(self):
        """长时间没有新音频（暂停了视频）时调用：把正在说的那句话直接结束。"""
        if self.in_speech:
            self._emit_final(self._wins, self._probs)
            self._reset_speech()
        self._preroll.clear()
        self._pending = np.zeros(0, "float32")
        self.vad.reset()

    def reset(self):
        self._reset_speech()
        self._preroll.clear()
        self._pending = np.zeros(0, "float32")
        self.vad.reset()
