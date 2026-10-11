"""音频输入：录制系统正在播放的声音（WASAPI Loopback），转成 16kHz 单声道 float32。

这样不管是 B 站网页、客户端还是其它播放器，只要你能听到，就能识别。
注意：录的是整个播放设备的声音，其它程序的提示音也会被录进去。
"""

import os
import queue
import threading
import time

import numpy as np
import soxr

TARGET_SR = 16000

# PortAudio 的初始化/释放/枚举设备不是线程安全的：列设备和开始录音可能在不同线程里同时发生，统一加锁
_PA_LOCK = threading.RLock()


def _clean_name(name: str) -> str:
    return name.replace(" [Loopback]", "")


def list_loopback_devices():
    import pyaudiowpatch as pyaudio

    with _PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            try:
                default = p.get_default_wasapi_loopback()
            except Exception:
                default = None
            devices = list(p.get_loopback_device_info_generator())
            return devices, default
        finally:
            p.terminate()


class LoopbackCapture:
    """把系统播放的声音不断推到 out_queue 里（每块是 16k 单声道 float32 的 numpy 数组）。"""

    def __init__(self, out_queue: "queue.Queue[np.ndarray]", device_hint: str = "",
                 fallback_to_default: bool = False,
                 not_found_tip: str = "，请运行 列出音频设备.bat 查看"):
        self.q = out_queue
        self.device_hint = (device_hint or "").strip()
        self.fallback_to_default = fallback_to_default   # 找不到指定设备时改用默认播放设备（而不是报错）
        self.not_found_tip = not_found_tip
        self.device_name = ""
        self.warning = ""          # 启动时值得告诉用户、但不影响录音的问题（例如改用了默认设备）
        self._pa = None
        self._stream = None
        self._resampler = None
        self._channels = 2
        self._lock = threading.Lock()

    def _find_device(self, p):
        self.warning = ""
        if self.device_hint:
            hint = self.device_hint.lower()
            devices = list(p.get_loopback_device_info_generator())
            # 先找名字完全一致的（界面传的是完整设备名），再找包含关系的（config.toml 里可以只填一部分）
            for dev in devices:
                if _clean_name(dev["name"]).lower() == hint:
                    return dev
            for dev in devices:
                if hint in dev["name"].lower():
                    return dev
            msg = f"找不到名字包含「{self.device_hint}」的播放设备"
            if not self.fallback_to_default:
                raise RuntimeError(msg + self.not_found_tip)
            self.warning = msg + "，已改用默认播放设备"
        return p.get_default_wasapi_loopback()

    def start(self):
        import pyaudiowpatch as pyaudio

        with self._lock, _PA_LOCK:
            self._stop_locked()
            self._pa = pyaudio.PyAudio()
            try:
                dev = self._find_device(self._pa)
                self.device_name = _clean_name(dev["name"])
                rate = int(dev["defaultSampleRate"])
                self._channels = max(1, int(dev["maxInputChannels"]))
                # soxr 的流式重采样器能正确处理块与块之间的衔接
                self._resampler = soxr.ResampleStream(rate, TARGET_SR, 1, dtype="float32", quality="HQ")
                self._stream = self._pa.open(
                    format=pyaudio.paFloat32,
                    channels=self._channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=int(rate * 0.03),
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
            except Exception:
                self._stop_locked()
                raise

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio

        try:
            x = np.frombuffer(in_data, dtype=np.float32)
            if self._channels > 1:
                x = x.reshape(-1, self._channels).mean(axis=1)
            y = self._resampler.resample_chunk(x)
            if y.size:
                self.q.put(y.astype(np.float32, copy=False))
        except Exception as e:  # 回调里不能抛异常，否则流会静默停止
            print("[audio] callback error:", e)
        return (None, pyaudio.paContinue)

    def _stop_locked(self):
        with _PA_LOCK:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

    def stop(self):
        with self._lock:
            self._stop_locked()


class FileSource:
    """测试用：把音频文件按真实时间节奏喂进来，模拟直播。再次 start() 会从头播放。"""

    def __init__(self, out_queue, path: str, speed: float = 1.0):
        self.q = out_queue
        self.path = path
        self.speed = speed if speed > 0 else 1.0
        self.device_name = f"文件: {path}"
        self.warning = ""
        self._audio = None
        self._stop = threading.Event()

    def start(self):
        if not os.path.isfile(self.path):
            raise RuntimeError(f"找不到音频文件：{self.path}")
        if self._audio is None:
            from faster_whisper import decode_audio

            self._audio = decode_audio(self.path, sampling_rate=TARGET_SR)
        self._stop.set()                      # 停掉上一次的播放线程
        stop = self._stop = threading.Event()
        audio, speed = self._audio, self.speed

        def run():
            chunk = int(TARGET_SR * 0.03)
            t0 = time.monotonic()
            for n, i in enumerate(range(0, len(audio), chunk)):
                if stop.is_set():
                    return
                self.q.put(audio[i : i + chunk].astype(np.float32))
                # 按绝对时间对齐，避免 sleep 误差累积导致越放越慢
                delay = t0 + (n + 1) * 0.03 / speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            print("[audio] 测试文件播放完毕")

        threading.Thread(target=run, daemon=True, name="file-source").start()

    def stop(self):
        self._stop.set()
