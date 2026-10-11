"""直播同传翻译：调用大模型（DeepSeek / 本地 Ollama / 其它 OpenAI 兼容接口）。"""

import re
import threading

import httpx

from .languages import source_prompt_name, target_prompt_name

PROMPT_TEMPLATE = """你是虚拟主播（VTuber）/网络直播的实时同声传译，负责把{src}直播语音的识别结果翻译成{tgt}字幕。

规则：
1. 只输出这一句的{tgt}译文。不要输出原文、注音、注释、解释或引号。
2. 译文口语化、自然、简洁，像字幕组的直播同传；保留主播的语气和情绪（撒娇、吐槽、惊讶、激动等）。
3. 输入来自语音识别，可能有同音错字、漏字或断句不完整。请结合上文推断主播真正想说的意思再翻译；句子说到一半就翻译成说到一半的样子，不要自行补全或续写。
4. {slang}
5. 只有语气词、笑声或拟声词时，就简短译成对应的{tgt}语气词（例如笑声→「哈哈」/「haha」）。
6. 如果识别结果明显是乱码、没有意义，或只是在唱歌的零碎歌词，输出「…」。"""

# 常见语言组合的圈内用语示例，帮模型对齐“字幕组味儿”
SLANG = {
    ("ja", "zh"): "网络用语和圈内梗用中文圈的通行说法，例如：草→草，てぇてぇ→贴贴，スパチャ→SC，"
                  "メンバーシップ/メン限→会员/会限，リスナー→观众，同接→同接，配信→直播，コラボ→联动，"
                  "ガチャ→抽卡，おつ→辛苦了，えーっと→呃…，あはは→哈哈，うわぁ→哇。",
    ("en", "zh"): "网络用语和圈内梗用中文圈的通行说法，例如：chat→观众/弹幕，superchat→SC，"
                  "membership→会员，collab→联动，stream→直播，oshi→推，clip→切片，"
                  "LOL/LMAO→笑死，pog/poggers→好耶，cringe→尬，GG→GG。",
    ("ja", "en"): "Use common English VTuber-fandom terms, e.g. スパチャ→superchat, メン限→members-only, "
                  "リスナー→chat, 同接→concurrent viewers, コラボ→collab, 草→LOL, てぇてぇ→precious/wholesome.",
}


def build_system_prompt(source: str, target: str, glossary: dict[str, str]) -> str:
    src, tgt = source_prompt_name(source), target_prompt_name(target)
    tgt_base = "zh" if target.startswith("zh") else target
    slang = SLANG.get((source, tgt_base), f"网络用语和圈内梗用{tgt}圈子里的通行说法。")
    prompt = PROMPT_TEMPLATE.format(src=src, tgt=tgt, slang=slang)
    # 对照表是“原文 = 中文”写的，只在译成中文时直接套用
    if glossary and tgt_base == "zh":
        lines = "\n".join(f"{a} → {b}" for a, b in glossary.items())
        prompt += f"\n\n专有名词对照表（遇到时必须使用这些译名）：\n{lines}"
    elif glossary:
        lines = "\n".join(f"{a}（中文：{b}）" for a, b in glossary.items())
        prompt += f"\n\n专有名词参考（括号里是中文含义，请译成{tgt}里的通行叫法）：\n{lines}"
    return prompt


_THINK = re.compile(r"<think>.*?</think>", re.S)


class TranslatorError(Exception):
    pass


# 给用户看的出错提示（告诉用户去界面的哪里改设置）
MSG_NO_KEY = "没有填 DeepSeek API Key：请在 设置 → 翻译 里填写"
MSG_NEED_URL = "OpenAI 兼容接口需要在 设置 → 翻译 里填写接口地址（base_url）和模型名"
MSG_BAD_KEY = "API Key 无效（401），请在 设置 → 翻译 里检查"
MSG_TRUNCATED = "模型输出被截断（可能开启了思考模式），请检查翻译设置"


class Translator:
    def __init__(self, cfg: dict, glossary: dict[str, str], source: str = "ja", target: str = "zh-Hans"):
        tcfg = cfg.get("translate", {})
        self.provider = tcfg.get("provider", "deepseek").strip().lower()
        self.enabled = self.provider not in ("off", "none", "")
        sub = tcfg.get(self.provider, {}) if self.enabled else {}

        self.model = sub.get("model", "")
        self.base_url = (sub.get("base_url") or "").rstrip("/")
        self.temperature = float(sub.get("temperature", 0.7))
        self.api_key = (sub.get("api_key") or "").strip()
        # DeepSeek 新模型默认会先“思考”再回答：慢好几倍，还可能把输出额度用光导致译文为空。同传必须关掉。
        self.extra_body = {}
        if self.provider == "deepseek" and not sub.get("thinking", False):
            self.extra_body = {"thinking": {"type": "disabled"}}

        self.glossary = glossary
        self.set_languages(source, target)

        self._local = threading.local()
        self._ollama_think_ok = True   # 旧版 Ollama 不认识 think 参数时自动关掉

    def set_languages(self, source: str, target: str):
        """运行中切换语言：只是换一份提示词（整体赋值，别的线程读到的要么是旧的要么是新的）。"""
        self.source, self.target = source, target
        self.system_prompt = build_system_prompt(source, target, self.glossary)

    # ------------------------------------------------------------------
    def check(self) -> str | None:
        """启动时检查配置。返回 None 表示没问题，否则返回给用户看的错误提示。"""
        if not self.enabled:
            return None
        if self.provider == "deepseek":
            if not self.api_key:
                return MSG_NO_KEY
            return self._check_models_endpoint()
        if self.provider == "ollama":
            try:
                r = self._client().get(f"{self.base_url}/api/tags", timeout=5)
                r.raise_for_status()
                names = [m.get("name", "") for m in r.json().get("models", [])]
            except Exception:
                return "连不上 Ollama：请先安装并启动 Ollama（https://ollama.com）"
            want = self.model if ":" in self.model else self.model + ":latest"
            if want not in names and self.model not in names:
                return f"Ollama 里还没有模型 {self.model}，请在命令行运行：ollama pull {self.model}"
            return None
        if self.provider == "openai_compatible":
            if not (self.base_url and self.model):
                return MSG_NEED_URL
            return self._check_models_endpoint()
        return f"未知的翻译引擎：{self.provider}"

    def _check_models_endpoint(self) -> str | None:
        """用 /models 接口验证 Key 和模型名。只在确定有问题时报错，网络暂时不通不算。"""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = self._client().get(f"{self.base_url}/models", headers=headers, timeout=8)
        except Exception as e:
            print("[translate] 启动检查时连不上翻译服务（先继续运行）：", e)
            return None
        if r.status_code == 401:
            return MSG_BAD_KEY
        if r.status_code != 200:
            return None
        try:
            ids = [m.get("id") for m in r.json().get("data", [])]
        except Exception:
            return None
        if ids and self.model not in ids:
            # 只提醒不报错：有些名字是别名（例如 deepseek-chat），列表里没有但能用。
            # 真的不存在的话，第一次翻译时会收到服务器的明确报错。
            print(f"[translate] 提示：模型列表里没有「{self.model}」（可用：{', '.join(ids[:8])}），先按别名尝试")
        return None

    def describe(self) -> str:
        if not self.enabled:
            return "不翻译"
        name = {"deepseek": "DeepSeek", "ollama": "本地 Ollama",
                "openai_compatible": "OpenAI 兼容接口"}.get(self.provider, self.provider)
        return f"{name} · {self.model}"

    # ------------------------------------------------------------------
    def _client(self) -> httpx.Client:
        # 每个线程一个连接池，复用 HTTPS 连接，省掉每次握手的延迟
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0))
            self._local.client = c
        return c

    def close(self):
        """关闭当前线程的连接池（临时用一次的翻译器用完后调用）。"""
        c = getattr(self._local, "client", None)
        if c is not None:
            self._local.client = None
            try:
                c.close()
            except Exception:
                pass

    def _build_messages(self, text: str, context: list[tuple[str, str]]):
        system_prompt = self.system_prompt   # 先取一份，防止切换语言时前后不一致
        user = ""
        if context:
            ctx = "\n".join(f"原：{src}\n译：{dst or '（略）'}" for src, dst in context)
            user += f"【上文，仅供参考，不要翻译】\n{ctx}\n\n"
        user += f"【请翻译这一句】\n{text}"
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ]

    def translate(self, text: str, context: list[tuple[str, str]]) -> str:
        messages = self._build_messages(text, context)
        last_err = None
        for _attempt in range(2):   # 网络抖动时重试一次
            try:
                if self.provider == "ollama":
                    out = self._call_ollama(messages)
                else:
                    out = self._call_openai(messages)
                return self._clean(out)
            except TranslatorError:
                raise
            except Exception as e:
                last_err = e
        raise TranslatorError(f"翻译请求失败：{last_err}")

    def _call_openai(self, messages) -> str:
        url = self.base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 300,
            "stream": False,
            **self.extra_body,
        }
        r = self._client().post(url, json=body, headers=headers)
        if r.status_code == 401:
            raise TranslatorError(MSG_BAD_KEY)
        if r.status_code == 402:
            raise TranslatorError("API 账户余额不足（402）")
        if r.status_code in (400, 404, 422):
            # 参数/模型名错误：重试也没用，直接把服务器的说明告诉用户
            try:
                detail = r.json().get("error", {}).get("message", r.text)
            except Exception:
                detail = r.text
            raise TranslatorError(f"翻译服务报错（{r.status_code}）：{detail[:150]}")
        r.raise_for_status()
        choice = r.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if not content.strip() and choice.get("finish_reason") == "length":
            raise TranslatorError(MSG_TRUNCATED)
        return content

    def _call_ollama(self, messages) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "60m",
            "options": {"temperature": self.temperature, "num_predict": 300},
        }
        if self._ollama_think_ok:
            body["think"] = False   # Qwen3 等模型默认会先“思考”，同传要关掉
        r = self._client().post(f"{self.base_url}/api/chat", json=body)
        if r.status_code == 400 and "think" in r.text and self._ollama_think_ok:
            self._ollama_think_ok = False
            body.pop("think", None)
            r = self._client().post(f"{self.base_url}/api/chat", json=body)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    @staticmethod
    def _clean(s: str) -> str:
        s = _THINK.sub("", s).strip()
        for prefix in ("译：", "译:", "中：", "中:", "译文：", "译文:", "翻译：", "翻译:"):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
        if len(s) >= 2 and s[0] in "「“\"'『" and s[-1] in "」”\"'』":
            s = s[1:-1].strip()
        return " ".join(line.strip() for line in s.splitlines() if line.strip())
