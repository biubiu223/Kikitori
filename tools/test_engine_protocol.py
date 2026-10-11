"""引擎协议端到端测试（docs/protocol.md）：真的启动引擎进程，通过管道发命令、收事件、做断言。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py                 源码模式（python engine_server.py）
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py --exe           打包版（build\\engine\\KikitoriEngine\\KikitoriEngine.exe）
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py --exe 路径      指定 exe
可选：
    --skip-live        不测真实录音（系统声音）
    --skip-reload      不测“换模型参数后重新加载”
    --download-test    额外测一次“首次下载模型”（用临时缓存目录下载 tiny 模型，测完删除）

翻译一律用 tools/mock_llm.py（脚本自己启动/关闭）；DeepSeek 只测“没填 Key”的失败路径（会从子进程环境里去掉 DEEPSEEK_API_KEY）。
所有生成物（引擎 stderr 日志、字幕记录、mock 请求记录）都放在 build/test/ 下。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/
ROOT = HERE.parent                              # 仓库根目录（engine_server.py 所在目录）
TESTDATA = HERE / "testdata"
MOCK_PORT = 8765
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/v1"
BOM = b"\xef\xbb\xbf"
CREATE_NO_WINDOW = 0x08000000

try:
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
except Exception:
    pass


class TestFailure(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise TestFailure(msg)


def say(*a):
    print(*a, flush=True)


# ======================================================================
class Engine:
    """一个引擎子进程：后台线程逐行读 stdout，校验每一行都是合法 JSON 事件。"""

    def __init__(self, cmd: list[str], log_path: Path, env: dict, name: str):
        self.name = name
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log_path, "wb")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            cwd=str(ROOT), env=env, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.events: list[tuple[float, dict]] = []
        self.bad_lines: list[bytes] = []
        self.line_count = 0
        self._cv = threading.Condition()
        self._eof = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            ok = False
            try:
                if raw.endswith(b"\n") and not raw.startswith(BOM):
                    obj = json.loads(raw.decode("utf-8"))
                    ok = isinstance(obj, dict) and isinstance(obj.get("event"), str)
            except Exception:
                ok = False
            with self._cv:
                self.line_count += 1
                if ok:
                    self.events.append((time.time(), obj))
                else:
                    self.bad_lines.append(raw)
                self._cv.notify_all()
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    # ------------------------------------------------------------------
    def send(self, obj: dict, bom: bool = False):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        self.proc.stdin.write((BOM if bom else b"") + data)
        self.proc.stdin.flush()

    def send_raw(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def mark(self) -> int:
        with self._cv:
            return len(self.events)

    def since(self, idx: int) -> list[dict]:
        with self._cv:
            return [e for _, e in self.events[idx:]]

    def wait_for(self, pred, timeout: float, since: int = 0, what: str = "事件") -> dict:
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for _, e in self.events[since:]:
                    if pred(e):
                        return e
                left = deadline - time.time()
                if left <= 0 or self._eof:
                    break
                self._cv.wait(min(left, 0.5))
        recent = [e for _, e in self.events[-12:] if e.get("event") != "level"]
        raise TestFailure(f"[{self.name}] {timeout:.0f}s 内没等到{what}。最近的事件：\n  "
                          + "\n  ".join(json.dumps(e, ensure_ascii=False) for e in recent))

    def wait_status(self, state: str, timeout: float, since: int = 0) -> dict:
        return self.wait_for(lambda e: e.get("event") == "status" and e.get("state") == state,
                             timeout, since, f" status {state}")

    def quiet_for(self, seconds: float, kinds=("partial", "final", "level", "remove")) -> list[dict]:
        """等 seconds 秒，返回这段时间里出现的指定类型事件。"""
        m = self.mark()
        time.sleep(seconds)
        return [e for e in self.since(m) if e.get("event") in kinds]

    def close_stdin(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        self._log.close()

    def check_stderr_clean(self):
        """引擎的 stderr 日志里不应该有 Python 异常堆栈（说明某个线程出错了，即使协议上没表现出来）。"""
        self._log.flush()
        text = self.log_path.read_bytes().decode("utf-8", errors="replace")
        check("Traceback (most recent call last)" not in text,
              f"[{self.name}] 引擎日志里有异常堆栈，见 {self.log_path}")


# ======================================================================
def wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class MockLLM:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        if log_path.exists():
            log_path.unlink()
        if wait_port(MOCK_PORT, 0.3):
            raise TestFailure(f"端口 {MOCK_PORT} 已被占用（是不是有残留的 mock_llm？）")
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "mock_llm.py"), "--port", str(MOCK_PORT), "--log", str(log_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        check(wait_port(MOCK_PORT, 15), "mock_llm 没有启动起来")

    def requests(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:      # 正好读到写了一半的行
                    pass
        return out

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def make_config(out_dir: Path, *, audio_file: str = "", language: str = "zh", target: str = "zh-Hans",
                provider: str = "openai_compatible", compute_type: str | None = None, model: str | None = None):
    cfg = {
        "translate": {
            "provider": provider,
            "target_language": target,
            "openai_compatible": {"base_url": MOCK_URL, "api_key": "", "model": "mock", "temperature": 0.7},
        },
        "asr": {"language": language},
        "audio": {"file": audio_file},
        "log_dir": str(out_dir / "字幕记录"),
    }
    if compute_type:
        cfg["asr"]["compute_type"] = compute_type
    if model:
        cfg["asr"]["model"] = model
    return cfg


def subtitle_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event") in ("partial", "final", "translation", "remove")]


def check_subtitles(events: list[dict], *, lang: str | None, modes: set[str], name: str):
    """检查一段时间里的字幕事件：字段齐全、id 递增、translating 的句子都有译文。"""
    finals = [e for e in events if e["event"] == "final"]
    partials = [e for e in events if e["event"] == "partial"]
    trans = {e["id"]: e for e in events if e["event"] == "translation"}
    check(finals, f"[{name}] 没有收到 final")
    ids = [e["id"] for e in finals]
    check(ids == sorted(ids) and len(set(ids)) == len(ids), f"[{name}] final 的 id 不是递增的：{ids}")
    for e in finals:
        check(isinstance(e.get("id"), int) and isinstance(e.get("text"), str) and e["text"],
              f"[{name}] final 字段不对：{e}")
        check(isinstance(e.get("ts"), (int, float)) and abs(e["ts"] - time.time()) < 600,
              f"[{name}] final.ts 不对：{e}")
        check(e.get("mode") in modes, f"[{name}] final.mode 应该是 {modes}：{e}")
        if lang:
            check(e.get("lang") == lang, f"[{name}] final.lang 应该是 {lang}：{e}")
        if e["mode"] == "translating":
            t = trans.get(e["id"])
            check(t is not None, f"[{name}] 句子 #{e['id']} 没有收到 translation")
            check(t.get("ok") is True and t.get("text", "").startswith("（译）"),
                  f"[{name}] translation 不对：{t}")
            check(isinstance(t.get("elapsed"), (int, float)), f"[{name}] translation 缺少 elapsed：{t}")
        else:
            check(e["id"] not in trans, f"[{name}] mode={e['mode']} 的句子不应该有 translation：{e}")
    for p in partials:
        check(isinstance(p.get("id"), int) and isinstance(p.get("text"), str), f"[{name}] partial 字段不对：{p}")
    return finals, partials, trans


def check_levels(events_with_time: list[tuple[float, dict]], name: str):
    lv = [(t, e) for t, e in events_with_time if e.get("event") == "level"]
    check(lv, f"[{name}] 没有收到 level 事件")
    for _, e in lv:
        check(isinstance(e.get("rms"), (int, float)) and 0 <= e["rms"] <= 1, f"[{name}] level.rms 不对：{e}")
    nonzero = [t for t, e in lv if e["rms"] > 0]
    gaps = [b - a for a, b in zip(nonzero, nonzero[1:])]
    # 管道读取有抖动，留一点余量
    check(not gaps or min(gaps) > 0.12, f"[{name}] level 太频繁（最小间隔 {min(gaps):.3f}s）")
    return len(lv)


def wait_playback_done(eng: Engine, since: int, timeout: float, name: str):
    """文件放完（有过声音之后 level 报 0）后，再等所有 translating 的句子都有译文。"""
    # 判断方法：有过声音，最后一个 level 是 0，而且之后 1 秒没有新的 level（没有音频数据了，不只是静音片段）
    deadline = time.time() + timeout
    while True:
        with eng._cv:
            lv = [(t, e) for t, e in eng.events[since:] if e.get("event") == "level"]
        heard = any(e.get("rms", 0) > 0 for _, e in lv)
        if heard and lv[-1][1].get("rms") == 0 and time.time() - lv[-1][0] > 1.0:
            break
        if time.time() > deadline or eng.proc.poll() is not None:
            raise TestFailure(f"[{name}] {timeout:.0f}s 内没等到播放结束（有声音之后 level 0）")
        time.sleep(0.3)
    deadline = time.time() + 30
    while time.time() < deadline:
        evs = eng.since(since)
        pending = {e["id"] for e in evs if e["event"] == "final" and e["mode"] == "translating"} \
            - {e["id"] for e in evs if e["event"] == "translation"}
        if not pending:
            # 最后一句可能还在识别：再确认 2 秒内没有新的 final
            n = len([e for e in evs if e["event"] == "final"])
            time.sleep(2)
            if len([e for e in eng.since(since) if e["event"] == "final"]) == n:
                return
        else:
            time.sleep(0.3)
    raise TestFailure(f"[{name}] 等译文超时")


def statuses(events: list[dict]) -> list[str]:
    return [e["state"] for e in events if e.get("event") == "status"]


def summarize(finals, trans, name):
    for f in finals:
        t = trans.get(f["id"])
        say(f"    #{f['id']} [{f['lang']}/{f['mode']}] {f['text']}" + (f"  →  {t['text']}" if t else ""))


# ======================================================================
def run_main_suite(cmd, env, out_dir: Path, mock: MockLLM, args) -> dict:
    zh_wav = str(TESTDATA / "tts_zh.wav")
    en_wav = str(TESTDATA / "tts_en.wav")
    results = {}
    eng = Engine(cmd, out_dir / "engine_main.stderr.log", env, "main")
    try:
        # ---------- hello → idle ----------
        t0 = time.time()
        hello = eng.wait_for(lambda e: True, 60, 0, " 第一个事件")
        check(hello.get("event") == "hello" and hello.get("protocol") == 1 and hello.get("version"),
              f"第一个事件应该是 hello：{hello}")
        idle = eng.wait_for(lambda e: e.get("event") == "status", 10, 1, " status")
        check(idle.get("state") == "idle", f"hello 之后应该是 status idle：{idle}")
        say(f"  ✓ hello → idle（{time.time() - t0:.1f}s）  {hello}")

        # ---------- 第一行带 BOM 的命令 + list_devices ----------
        m = eng.mark()
        eng.send({"cmd": "list_devices"}, bom=True)
        dev = eng.wait_for(lambda e: e.get("event") == "devices", 15, m, " devices")
        check(isinstance(dev.get("devices"), list), f"devices 格式不对：{dev}")
        for d in dev["devices"]:
            check(isinstance(d.get("name"), str) and isinstance(d.get("is_default"), bool), f"设备项格式不对：{d}")
        check(not [e for e in eng.since(m) if e.get("event") == "notice" and e.get("level") == "error"],
              "带 BOM 的第一行命令解析失败了")
        say(f"  ✓ 带 BOM 的 list_devices → {len(dev['devices'])} 个设备："
            + "、".join(d["name"] + ("（默认）" if d["is_default"] else "") for d in dev["devices"]))

        # ---------- 错误命令不崩溃 ----------
        for bad, desc in ((b'{"cmd":"no_such_cmd"}\n', "未知命令"), (b"{oops\n", "非法 JSON"),
                          (b'{"cmd":"pause","value":"yes"}\n', "字段错误"), (b'[1,2]\n', "不是对象"),
                          (b'{"cmd":"set_languages","source":"xx"}\n', "不支持的语言")):
            m = eng.mark()
            eng.send_raw(bad)
            eng.wait_for(lambda e: e.get("event") == "notice" and e.get("level") == "error", 5, m, f" {desc}的 notice")
        check(eng.proc.poll() is None, "发了错误命令后引擎退出了")
        say("  ✓ 未知命令 / 非法 JSON / 字段错误 → notice(error)，进程没崩")

        # ---------- test_translation ----------
        m = eng.mark()
        eng.send({"cmd": "test_translation", "config": make_config(out_dir)})
        tt = eng.wait_for(lambda e: e.get("event") == "translation_test", 30, m, " translation_test")
        check(tt.get("ok") is True and tt.get("output", "").startswith("（译）") and tt.get("input"),
              f"mock 翻译测试应该成功：{tt}")
        say(f"  ✓ test_translation（mock）→ ok：{tt['input']} → {tt['output']}（{tt['elapsed']}s）")
        m = eng.mark()
        eng.send({"cmd": "test_translation", "config": {"translate": {"provider": "deepseek"}}})
        tt = eng.wait_for(lambda e: e.get("event") == "translation_test", 30, m, " translation_test")
        check(tt.get("ok") is False and tt.get("message"), f"DeepSeek 没填 Key 应该失败：{tt}")
        say(f"  ✓ test_translation（deepseek 无 Key）→ ok=false：{tt['message']}")

        # ---------- start：中文，目标繁体 → translating ----------
        n_req0 = len(mock.requests())
        m = eng.mark()
        t0 = time.time()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh_wav, language="zh", target="zh-Hant")})
        eng.wait_status("listening", 600, m)
        boot = statuses(eng.since(m))
        check(boot[-1] == "listening" and "loading_model" in boot and "warming_up" in boot
              and boot.index("loading_model") < boot.index("warming_up"),
              f"第一次 start 的状态顺序不对：{boot}")
        listening = [e for e in eng.since(m) if e.get("event") == "status" and e.get("state") == "listening"][-1]
        results["first_start_s"] = round(time.time() - t0, 1)
        say(f"  ✓ start → {' → '.join(boot)}（{results['first_start_s']}s）：{listening['message']}")

        # 播放过程中做一次翻译测试：不能影响正在运行的识别
        eng.wait_for(lambda e: e.get("event") == "final", 60, m, " 第一句 final")
        m2 = eng.mark()
        eng.send({"cmd": "test_translation", "config": make_config(out_dir)})
        tt = eng.wait_for(lambda e: e.get("event") == "translation_test", 30, m2, " translation_test")
        check(tt.get("ok") is True, f"运行中的翻译测试失败：{tt}")
        wait_playback_done(eng, m, 120, "zh→繁体")
        with eng._cv:
            timed = eng.events[m:]
        evs = [e for _, e in timed]
        finals, partials, trans = check_subtitles(evs, lang="zh", modes={"translating"}, name="zh→繁体")
        check(partials, "[zh→繁体] 没有收到 partial")
        nlv = check_levels(timed, "zh→繁体")
        sys_prompts = [r["system"] for r in mock.requests()[n_req0:]]
        check(any("繁体" in s for s in sys_prompts), f"翻译请求的提示词里没有“繁体”：{sys_prompts[:2]}")
        say(f"  ✓ zh 文件：{len(partials)} 个 partial，{len(finals)} 个 final(translating)，"
            f"{len(trans)} 个 translation，{nlv} 个 level；运行中 test_translation 正常")
        summarize(finals, trans, "zh")
        last_id = max(e["id"] for e in finals)

        # ---------- 再次 start（同样的模型参数）：不重新加载；中文 + 简体 → same_language，不发翻译请求 ----------
        n_req1 = len(mock.requests())
        m = eng.mark()
        t0 = time.time()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh_wav, language="zh", target="zh-Hans")})
        eng.wait_status("listening", 60, m)
        boot = statuses(eng.since(m))
        check("loading_model" not in boot and "warming_up" not in boot and "downloading_model" not in boot,
              f"模型参数没变，不应该重新加载：{boot}")
        results["restart_s"] = round(time.time() - t0, 1)
        say(f"  ✓ 再次 start 复用模型：{' → '.join(boot)}（{results['restart_s']}s）")

        first = eng.wait_for(lambda e: e.get("event") == "final", 60, m, " same_language 的 final")
        check(first.get("mode") == "same_language" and first.get("id") > last_id,
              f"中文 + 简体中文应该是 same_language，且 id 接着上次递增（上次最大 {last_id}）：{first}")
        time.sleep(1.0)   # 让 same_language 的句子有机会触发（不应该触发的）翻译请求
        check(len(mock.requests()) == n_req1, "same_language 不应该发翻译请求")
        say(f"  ✓ same_language：#{first['id']} {first['text']}（id 接着上次的 {last_id} 递增，没有翻译请求）")

        # ---------- set_languages：运行中切换成繁体 → 之后的句子 translating ----------
        m_sw = eng.mark()
        eng.send({"cmd": "set_languages", "source": "zh", "target": "zh-Hant"})
        note = eng.wait_for(lambda e: e.get("event") == "notice" and "已切换" in e.get("message", ""), 10, m_sw,
                            " 已切换 的 notice")
        st = eng.wait_status("listening", 10, m_sw)
        check("繁體中文" in st["message"], f"切换后的 listening 状态应该写新的目标语言：{st}")
        say(f"  ✓ set_languages → notice：{note['message']}；status：{st['message']}")
        wait_playback_done(eng, m, 120, "set_languages")
        evs = eng.since(m)
        after = eng.since(m_sw)
        finals_after = [e for e in after if e["event"] == "final"]
        check(finals_after, "切换语言后没有新句子（测试音频太短？）")
        # 切换那一刻正在识别的句子可能还按旧设置走，从第二句开始一定是 translating
        check(any(e["mode"] == "translating" for e in finals_after), f"切换到繁体后应该有 translating：{finals_after}")
        check(finals_after[-1]["mode"] == "translating", f"切换后最后一句应该是 translating：{finals_after[-1]}")
        finals, _, trans = check_subtitles(evs, lang="zh", modes={"same_language", "translating"}, name="set_languages")
        check(len(mock.requests()) > n_req1, "切换到繁体后应该有翻译请求")
        summarize(finals, trans, "set_languages")

        # ---------- pause / 暂停中切换语言、重新连接（应重发 paused 而不是 listening）/ 继续 ----------
        m = eng.mark()
        eng.send({"cmd": "pause", "value": True})
        eng.wait_status("paused", 5, m)
        for cmd, expect, desc in (({"cmd": "set_languages", "source": "zh", "target": "zh-Hans"}, "简体中文", "set_languages"),
                                  ({"cmd": "reconnect_audio"}, "tts_zh.wav", "reconnect_audio")):
            m2 = eng.mark()
            eng.send(cmd)
            st = eng.wait_for(lambda e: e.get("event") == "status", 10, m2, f" 暂停中 {desc} 后的 status")
            check(st.get("state") == "paused" and expect in st.get("message", ""),
                  f"暂停中 {desc} 后应该重发 status paused（message 带“{expect}”），实际：{st}")
            time.sleep(0.5)
            states = statuses(eng.since(m2))
            check(states == ["paused"], f"暂停中 {desc} 后只应该有一个 status paused，实际：{states}")
            say(f"  ✓ 暂停中 {desc} → status paused：{st['message']}")
        m = eng.mark()
        eng.send({"cmd": "pause", "value": False})
        st = eng.wait_status("listening", 5, m)
        check("简体中文" in st["message"], f"继续后的 listening 应该带新语言：{st}")
        say("  ✓ pause → paused，pause false → listening")

        # ---------- stop ----------
        m = eng.mark()
        eng.send({"cmd": "stop"})
        eng.wait_status("stopped", 10, m)
        time.sleep(1.5)   # 正在识别的那一句允许收尾（final / remove）
        late = eng.quiet_for(2.0)
        check(not late, f"stop 之后还在发字幕/音量事件：{late[:3]}")
        say("  ✓ stop → stopped，之后没有 level/partial/final")

        # ---------- 英文：指定 en，再试 auto ----------
        for lang in ("en", "auto"):
            m = eng.mark()
            eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=en_wav, language=lang, target="zh-Hans")})
            eng.wait_status("listening", 60, m)
            boot = statuses(eng.since(m))
            check("loading_model" not in boot, f"[en/{lang}] 不应该重新加载模型：{boot}")
            wait_playback_done(eng, m, 120, f"en/{lang}")
            finals, partials, trans = check_subtitles(eng.since(m), lang="en", modes={"translating"}, name=f"en/{lang}")
            say(f"  ✓ 英文 asr.language={lang}：{len(partials)} 个 partial，{len(finals)} 个 final，{len(trans)} 个 translation")
            summarize(finals, trans, lang)

        # ---------- 换模型参数：应该重新加载 ----------
        if not args.skip_reload:
            m = eng.mark()
            t0 = time.time()
            eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=en_wav, language="en",
                                                            compute_type="int8_float16")})
            eng.wait_status("listening", 600, m)
            boot = statuses(eng.since(m))
            check("loading_model" in boot and "warming_up" in boot, f"换了 compute_type 应该重新加载：{boot}")
            eng.wait_for(lambda e: e.get("event") == "final", 60, m, " int8_float16 的 final")
            say(f"  ✓ 换 compute_type=int8_float16 → 重新加载：{' → '.join(boot)}（{time.time() - t0:.1f}s），能正常识别")
            m = eng.mark()
            eng.send({"cmd": "stop"})
            eng.wait_status("stopped", 10, m)

        # ---------- 真实录音（系统声音）：能开始并收到 level ----------
        if not args.skip_live:
            m = eng.mark()
            eng.send({"cmd": "start", "config": make_config(out_dir, audio_file="", language="ja", target="zh-Hans",
                                                            compute_type="int8_float16" if not args.skip_reload else None)})
            st = eng.wait_for(lambda e: e.get("event") == "status" and e.get("state") in ("listening", "error"),
                              120, m, " listening/error")
            check(st["state"] == "listening", f"真实录音启动失败：{st}")
            lv = eng.wait_for(lambda e: e.get("event") == "level", 5, m, " level")
            say(f"  ✓ 真实录音：{st['message']}；收到 level rms={lv['rms']}")
            m2 = eng.mark()
            eng.send({"cmd": "reconnect_audio"})
            note = eng.wait_for(lambda e: e.get("event") == "notice", 10, m2, " 重新连接的 notice")
            check("已重新连接" in note.get("message", ""), f"reconnect_audio 应该提示已重新连接：{note}")
            eng.wait_status("listening", 10, m2)
            # 没有在放声音时 WASAPI 不送数据，引擎只在静音开始时报一次 0，所以重连后不一定有新的 level
            say(f"  ✓ reconnect_audio → {note['message']} → status listening")
            m = eng.mark()
            eng.send({"cmd": "stop"})
            eng.wait_status("stopped", 10, m)
            say("  ✓ 真实录音 stop")

        # ---------- 字幕记录 ----------
        logs = list((out_dir / "字幕记录").glob("*.txt"))
        check(logs, "log_dir 里没有字幕记录文件")
        say(f"  ✓ 字幕记录：{logs[0]}（{logs[0].stat().st_size} 字节）")

        # ---------- shutdown ----------
        t0 = time.time()
        eng.send({"cmd": "shutdown"})
        try:
            code = eng.proc.wait(5)
        except subprocess.TimeoutExpired:
            raise TestFailure("shutdown 后 5 秒还没退出")
        check(code == 0, f"shutdown 的退出码应该是 0，实际 {code}")
        say(f"  ✓ shutdown → 退出码 0（{time.time() - t0:.2f}s）")

        time.sleep(0.3)
        check(not eng.bad_lines, f"stdout 里有非 JSON 的行：{eng.bad_lines[:3]}")
        say(f"  ✓ stdout 共 {eng.line_count} 行，全部是合法 JSON 事件")
        eng.check_stderr_clean()
        say("  ✓ 引擎 stderr 日志里没有异常堆栈")
        results["events"] = eng.line_count
        return results
    finally:
        eng.kill()


def run_eof_suite(cmd, env, out_dir: Path):
    """关闭 stdin（界面退出/崩溃）→ 引擎 2 秒内自己退出。分别在空闲时和正在监听时测。"""
    for case in ("idle", "listening"):
        eng = Engine(cmd, out_dir / f"engine_eof_{case}.stderr.log", env, f"eof-{case}")
        try:
            eng.wait_status("idle", 60)
            if case == "listening":
                # 先测：start 后马上 stop（模型还在加载）→ stopped，之后不能再冒出 warming_up / listening
                m = eng.mark()
                eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=str(TESTDATA / "tts_zh.wav"))})
                eng.send({"cmd": "stop"})
                eng.wait_status("stopped", 10, m)
                m_stop = eng.mark()
                time.sleep(15)
                late = [e for e in eng.since(m_stop) if e.get("event") in ("status", "partial", "final", "level")]
                check(not late, f"[eof-{case}] 加载中 stop 之后还有事件：{late[:3]}")
                say("  ✓ 模型加载中 stop → stopped，之后没有 warming_up/listening")
                m = eng.mark()
                eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=str(TESTDATA / "tts_zh.wav"))})
                eng.wait_status("listening", 600, m)
                eng.wait_for(lambda e: e.get("event") in ("partial", "final"), 30, m, " 字幕")
            t0 = time.time()
            eng.close_stdin()
            try:
                code = eng.proc.wait(5)
            except subprocess.TimeoutExpired:
                raise TestFailure(f"[eof-{case}] 关闭 stdin 后 5 秒还没退出")
            dt = time.time() - t0
            check(dt < 2.0, f"[eof-{case}] 关闭 stdin 后 {dt:.2f}s 才退出（要求 2 秒内）")
            time.sleep(0.2)
            check(not eng.bad_lines, f"[eof-{case}] stdout 里有非 JSON 的行：{eng.bad_lines[:3]}")
            eng.check_stderr_clean()
            say(f"  ✓ {case} 时关闭 stdin → {dt:.2f}s 内退出（退出码 {code}）")
        finally:
            eng.kill()


def run_download_suite(cmd, env, out_dir: Path):
    """用临时的 HuggingFace 缓存目录下载 tiny 模型，检查 downloading_model 和进度。测完删除临时缓存。"""
    cache = out_dir / "hf-cache-tmp"
    shutil.rmtree(cache, ignore_errors=True)
    env = dict(env, HF_HUB_CACHE=str(cache))
    eng = Engine(cmd, out_dir / "engine_download.stderr.log", env, "download")
    try:
        eng.wait_status("idle", 60)
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=str(TESTDATA / "tts_en.wav"),
                                                        language="en", model="tiny")})
        eng.wait_status("listening", 600, m)
        evs = eng.since(m)
        dl = [e for e in evs if e.get("event") == "status" and e.get("state") == "downloading_model"]
        check(dl, f"没有 downloading_model：{statuses(evs)}")
        progress = [e.get("progress") for e in dl if "progress" in e]
        check(progress == sorted(progress), f"下载进度不是递增的：{progress}")
        say(f"  ✓ 首次下载：{' → '.join(dict.fromkeys(statuses(evs)))}；进度 {progress}")
        eng.wait_for(lambda e: e.get("event") == "final", 60, m, " tiny 模型的 final")
        say("  ✓ 下载的 tiny 模型能正常识别")
        eng.send({"cmd": "shutdown"})
        eng.proc.wait(5)
        check(not eng.bad_lines, f"stdout 里有非 JSON 的行：{eng.bad_lines[:3]}")
        eng.check_stderr_clean()
    finally:
        eng.kill()
        shutil.rmtree(cache, ignore_errors=True)


# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="Kikitori 引擎协议测试")
    ap.add_argument("--exe", nargs="?", const="", default=None,
                    help="测试打包后的 exe（不带路径 = build/engine/KikitoriEngine/KikitoriEngine.exe）")
    ap.add_argument("--python", default=sys.executable, help="源码模式用哪个 python（默认当前解释器）")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-reload", action="store_true")
    ap.add_argument("--skip-eof", action="store_true")
    ap.add_argument("--download-test", action="store_true")
    args = ap.parse_args()

    if args.exe is not None:
        exe = Path(args.exe) if args.exe else ROOT / "build" / "engine" / "KikitoriEngine" / "KikitoriEngine.exe"
        check(exe.is_file(), f"找不到 {exe}，请先运行 packaging/build_engine.ps1")
        cmd, mode = [str(exe)], "exe"
    else:
        cmd, mode = [args.python, str(ROOT / "engine_server.py")], "source"
    out_dir = ROOT / "build" / "test" / mode
    out_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)        # deepseek 只测“没填 Key”
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    say(f"== 引擎协议测试（{mode}）：{' '.join(cmd)}")
    say(f"   输出目录：{out_dir}")
    mock = MockLLM(out_dir / "mock_llm_requests.log")
    ok = False
    t0 = time.time()
    try:
        say("-- 主流程")
        run_main_suite(cmd, env, out_dir, mock, args)
        if not args.skip_eof:
            say("-- stdin EOF")
            run_eof_suite(cmd, env, out_dir)
        if args.download_test:
            say("-- 首次下载模型")
            run_download_suite(cmd, env, out_dir)
        ok = True
    except TestFailure as e:
        say(f"\n✗ 测试失败：{e}")
    finally:
        mock.stop()
    say(f"\n{'全部通过' if ok else '有失败'}（{mode}，用时 {time.time() - t0:.0f}s）。引擎日志在 {out_dir}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
