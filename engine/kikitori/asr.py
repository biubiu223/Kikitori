"""日语语音识别：faster-whisper + 幻听过滤。"""

import re
import time

import numpy as np

# Whisper 在静音/噪音/BGM 上经常“幻听”出的句子（训练数据里 YouTube 字幕结尾的套话）
HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "最後までご視聴いただきありがとうございました",
    "最後までご視聴頂きありがとうございました",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録をお願いします",
    "チャンネル登録お願いします",
    "ご視聴ありがとうございます",
    "字幕視聴ありがとうございました",
    "thankyouforwatching",
    "thanksforwatching",
    "pleasesubscribe",
    "시청해주셔서감사합니다",
    "구독과좋아요부탁드립니다",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "字幕由amaraorg社区提供",
    "谢谢观看",
}

_PUNCT = re.compile(r"[\s、。，,．.！!？?…・〜~「」『』（）()\"'“”‘’ー-]+")
_REPEAT = re.compile(r"(.{1,12}?)\1{4,}")      # 同一片段重复 5 次以上
_NO_SPACE_LANGS = {"ja", "zh", "th", "lo", "my", "yue"}   # 这些语言句子之间不加空格


def _norm(s: str) -> str:
    return _PUNCT.sub("", s).lower()


class Transcriber:
    def __init__(self, model="large-v3", device="cuda", compute_type="float16",
                 language="ja", beam_size=5, hotwords: str | None = None, label: str | None = None):
        from faster_whisper import WhisperModel

        # "auto" = 每句话自动判断语言（传给 Whisper 的是 None）。运行中可以直接改这个属性来切换。
        self.language = language
        self.beam_size = beam_size
        self.hotwords = hotwords or None
        t = time.time()
        # model 可以是模型名，也可以是已经下载好的本地目录（label 是给日志看的名字）
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        print(f"[asr] 模型 {label or model} 加载完成 ({time.time() - t:.1f}s, {device}/{compute_type})")

    def warmup(self):
        silence = np.zeros(16000, dtype=np.float32)
        list(self.model.transcribe(silence, language="ja", beam_size=1)[0])

    def transcribe(self, audio: np.ndarray, final: bool, speech_conf: float = 1.0) -> tuple[str, str | None]:
        """返回 (识别出的文字, 实际语言代码)。"""
        if audio.size < 1600:  # < 0.1s
            return "", None
        lang = None if self.language == "auto" else self.language
        # 人说话再快每秒也就十几个 token（韩语一个字可能占 2 个 token，所以放宽到 25）。按音频长度限制输出，
        # 防止模型陷入“あのーあのーあのー…”的复读循环一直生成到上限（会卡好几秒）
        # （上限 200：提示词最多占 ~225 个 token，两者加起来不能超过 Whisper 的 448）
        max_tokens = min(200, int(audio.size / 16000 * 25) + 20)
        segments, info = self.model.transcribe(
            audio,
            language=lang,
            task="transcribe",
            beam_size=self.beam_size if final else 1,
            # 草稿只求快；正式结果允许温度回退，避免卡在重复循环里
            temperature=[0.0, 0.2, 0.4, 0.6] if final else 0.0,
            vad_filter=False,                     # 已经自己断过句了
            condition_on_previous_text=False,     # 不依赖上一句，防止错误/幻听传染
            without_timestamps=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            hotwords=self.hotwords,
            max_new_tokens=max_tokens,
        )

        parts = []
        for s in segments:
            text = s.text.strip()
            if not text:
                continue
            # 模型自己也认为“没人说话”并且没把握 → 丢
            if s.no_speech_prob > 0.9 and s.avg_logprob < -0.7:
                continue
            # 经典幻听句：只有在声音不太像清晰人声时才丢（主播真说“ご視聴ありがとうございました”时保留）
            if _norm(text) in HALLUCINATIONS and (s.no_speech_prob > 0.3 or speech_conf < 0.7):
                continue
            parts.append(text)

        detected = lang or info.language
        text = ("" if detected in _NO_SPACE_LANGS else " ").join(parts)
        text = _REPEAT.sub(lambda m: m.group(1) * 3, text)   # “あああああああ…”压缩一下
        if not any(ch.isalnum() for ch in text):             # 只剩标点符号
            return "", detected
        return text, detected
