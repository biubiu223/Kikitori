"""模拟引擎：完全按 docs/protocol.md 说话，但不加载模型、不录音、不联网。

给界面开发/测试用。启动方式：
    python tools/mock_engine.py            （任意 Python 3.10+，不需要任何第三方库）
界面里设置环境变量 KIKITORI_ENGINE='"python.exe" "<仓库目录>\\tools\\mock_engine.py"' 即可对接。

会循环播放一组示例句子：草稿逐步变长 → final → 译文；偶尔模拟“噪音被删除”“翻译失败”“同语言不翻译”。
"""

import json
import math
import random
import sys
import threading
import time

sys.stdin.reconfigure(encoding="utf-8")
_out = sys.stdout
_out.reconfigure(encoding="utf-8", newline="\n")
sys.stdout = sys.stderr   # 防止误 print 污染协议通道
_lock = threading.Lock()


def emit(**ev):
    with _lock:
        _out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        _out.flush()


def log(*a):
    print("[mock]", *a, file=sys.stderr, flush=True)


SAMPLES = [
    ("こんぺここんぺここんぺこー！ホロライブ3期生の兎田ぺこらぺこ！", "你好佩克拉你好佩克拉！我是Hololive三期生的兔田佩克拉！"),
    ("今日はね、えっと、新しいゲームやっていこうかなと思ってて", "今天呢，呃，我打算来玩个新游戏"),
    ("いや待って待って、それはずるくない？ずるいって絶対", "等等等等，这不犯规吗？绝对犯规吧"),
    ("うわぁ！びっくりした！何これ怖すぎるんだけど", "哇！吓我一跳！这是什么啊太可怕了吧"),
    ("スパチャありがとう！野うさぎさん、いつもありがとうね", "谢谢SC！野兔们，一直以来谢谢你们啦"),
    ("草。いや草生えるって", "草。不是，这也太草了"),
    ("あ、ちょっと待って、これってさ、もしかして", "啊，等一下，这个该不会是"),
    ("同接すごい、みんなありがとう、マジで嬉しいぺこ", "同接好高，大家谢谢你们，真的超开心的佩克"),
    ("メン限のお知らせなんですけど、来週やります", "关于会限的通知，下周会搞"),
    ("じゃあ今日はこの辺で、おつぺこでした！", "那今天就到这里，辛苦了佩克！"),
]

DEVICES = [
    {"name": "扬声器 (Realtek(R) Audio)", "is_default": True},
    {"name": "耳机 (USB Audio Device)", "is_default": False},
    {"name": "Realtek Digital Output (Realtek(R) Audio)", "is_default": False},
]

LANG_NAMES = {"ja": "日语", "en": "英语", "ko": "韩语", "zh": "中文", "ru": "俄语", "fr": "法语",
              "es": "西班牙语", "de": "德语", "auto": "自动识别",
              "zh-Hans": "简体中文", "zh-Hant": "繁體中文", "ko-KR": "한국어"}


class Mock:
    def __init__(self):
        self.running = False
        self.paused = False
        self.config = {}
        self.source, self.target = "ja", "zh-Hans"
        self.next_id = 1
        self.model_loaded = False
        self._gen = 0   # 每次 start/stop 递增，用来让旧的播放线程退出

    def desc(self):
        dev = self.config.get("audio", {}).get("device") or DEVICES[0]["name"]
        prov = self.config.get("translate", {}).get("provider", "deepseek")
        model = self.config.get("translate", {}).get(prov, {}).get("model", "")
        return (f"正在监听：{dev} | {LANG_NAMES.get(self.source, self.source)} → "
                f"{LANG_NAMES.get(self.target, self.target)} | {prov} · {model}")

    # ---------------- commands ----------------
    def start(self, config):
        self.config = config or {}
        self.source = self.config.get("asr", {}).get("language", "ja")
        self.target = self.config.get("translate", {}).get("target_language", "zh-Hans")
        self._gen += 1
        gen = self._gen

        def run():
            if not self.model_loaded:
                emit(event="status", state="loading_model", message="正在加载语音识别模型…", level="info")
                time.sleep(1.5)
                emit(event="status", state="warming_up", message="正在预热显卡…", level="info")
                time.sleep(0.8)
                self.model_loaded = True
            if gen != self._gen:
                return
            prov = self.config.get("translate", {}).get("provider", "deepseek")
            key = self.config.get("translate", {}).get("deepseek", {}).get("api_key", "")
            if prov == "deepseek" and not key:
                emit(event="notice", level="error",
                     message="没有填 DeepSeek API Key：请在设置 → 翻译里填写（目前只显示原文）")
            self.running, self.paused = True, False
            emit(event="status", state="listening", message=self.desc(), level="info")
            threading.Thread(target=self.level_loop, args=(gen,), daemon=True).start()
            self.subtitle_loop(gen)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._gen += 1
        self.running = False
        emit(event="status", state="stopped", message="已停止", level="info")

    # ---------------- fake streams ----------------
    def level_loop(self, gen):
        t0 = time.time()
        while gen == self._gen and self.running:
            if self.paused:
                emit(event="level", rms=0.0)
                time.sleep(0.5)
                continue
            t = time.time() - t0
            rms = max(0.0, 0.04 + 0.035 * math.sin(t * 3.1) + random.uniform(-0.01, 0.02))
            emit(event="level", rms=round(rms, 4))
            time.sleep(0.2)

    def subtitle_loop(self, gen):
        i = 0
        while gen == self._gen and self.running:
            if self.paused:
                time.sleep(0.2)
                continue
            ja, zh = SAMPLES[i % len(SAMPLES)]
            sid = self.next_id
            self.next_id += 1
            n = len(ja)
            # 草稿：逐步变长
            for k in (max(3, n // 3), max(5, 2 * n // 3)):
                if gen != self._gen or self.paused:
                    break
                emit(event="partial", id=sid, text=ja[:k])
                time.sleep(0.6)
            if gen != self._gen:
                return
            if i % 11 == 7:   # 偶尔：判定为噪音，删掉草稿
                emit(event="remove", id=sid)
            else:
                prov = self.config.get("translate", {}).get("provider", "deepseek")
                key = self.config.get("translate", {}).get("deepseek", {}).get("api_key", "")
                no_tr = prov == "off" or (prov == "deepseek" and not key)
                if no_tr:
                    emit(event="final", id=sid, text=ja, lang="ja", mode="no_translation", ts=time.time() - 3)
                elif i % 13 == 5:   # 偶尔：说的就是目标语言
                    emit(event="final", id=sid, text=zh, lang="zh", mode="same_language", ts=time.time() - 3)
                else:
                    emit(event="final", id=sid, text=ja, lang="ja", mode="translating", ts=time.time() - 3)
                    time.sleep(random.uniform(0.4, 0.9))
                    if i % 17 == 9:   # 偶尔：翻译失败
                        emit(event="translation", id=sid, text="", ok=False, error="翻译请求失败：模拟的网络错误")
                        emit(event="notice", level="error", message="⚠ 翻译请求失败：模拟的网络错误")
                    else:
                        out = zh if self.target == "zh-Hans" else f"[{self.target}] {zh}"
                        emit(event="translation", id=sid, text=out, ok=True, elapsed=round(random.uniform(0.4, 0.9), 2))
            i += 1
            time.sleep(random.uniform(0.6, 1.5))

    # ---------------- dispatcher ----------------
    def handle(self, msg):
        cmd = msg.get("cmd")
        log("cmd:", cmd)
        if cmd == "start":
            self.start(msg.get("config"))
        elif cmd == "stop":
            self.stop()
        elif cmd == "pause":
            self.paused = bool(msg.get("value"))
            if self.running:
                emit(event="status", state="paused" if self.paused else "listening",
                     message="已暂停" if self.paused else self.desc(), level="info")
        elif cmd == "set_languages":
            self.source = msg.get("source", self.source)
            self.target = msg.get("target", self.target)
            emit(event="notice", level="info",
                 message=f"已切换：{LANG_NAMES.get(self.source, self.source)} → {LANG_NAMES.get(self.target, self.target)}")
            if self.running and not self.paused:
                emit(event="status", state="listening", message=self.desc(), level="info")
        elif cmd == "reconnect_audio":
            emit(event="notice", level="info", message=f"已重新连接：{DEVICES[0]['name']}")
        elif cmd == "list_devices":
            emit(event="devices", devices=DEVICES)
        elif cmd == "test_translation":
            cfg = (msg.get("config") or {}).get("translate", {})
            prov = cfg.get("provider", "deepseek")
            time.sleep(0.6)
            if prov == "deepseek" and not cfg.get("deepseek", {}).get("api_key"):
                emit(event="translation_test", ok=False, message="没有填 API Key", input="", output="", elapsed=0)
            elif prov == "deepseek" and not str(cfg["deepseek"]["api_key"]).startswith("sk-"):
                emit(event="translation_test", ok=False, message="API Key 无效（401），请检查", input="", output="", elapsed=0.3)
            else:
                emit(event="translation_test", ok=True, message="连接正常", input="こんにちは、今日もよろしくね！",
                     output="大家好，今天也请多关照！", elapsed=0.58)
        elif cmd == "shutdown":
            log("shutdown")
            self._gen += 1
            sys.exit(0)
        else:
            emit(event="notice", level="error", message=f"未知命令：{cmd}")


def main():
    m = Mock()
    emit(event="hello", version="0.0.0-mock", protocol=1)
    emit(event="status", state="idle", message="引擎已就绪（模拟）", level="info")
    for line in sys.stdin:
        line = line.lstrip("﻿").strip()   # 有些写入方会在第一行前面带 BOM
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            emit(event="notice", level="error", message=f"无法解析的命令：{line[:80]}")
            continue
        m.handle(msg)
    log("stdin closed, exiting")   # 界面退出 → 引擎跟着退出


if __name__ == "__main__":
    main()
