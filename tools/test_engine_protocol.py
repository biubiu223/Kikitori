"""引擎协议端到端测试（docs/protocol.md）：真的启动引擎进程，通过管道发命令、收事件、做断言。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py                 源码模式（python engine_server.py）
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py --exe           打包版（build\\engine\\KikitoriEngine\\KikitoriEngine.exe）
    .venv\\Scripts\\python.exe tools\\test_engine_protocol.py --exe 路径      指定 exe
可选：
    --skip-live        不测真实录音（系统声音）
    --skip-reload      不测“换模型参数后重新加载”
    --skip-main        不跑主流程（只跑下面选中的专项）
    --download-test    额外测一次“首次下载模型”（用临时缓存目录下载 tiny 模型，测完删除）
    --runtime-test     显卡运行库下载专项（本地 HTTP 服务器模拟镜像）：全新下载、不重复下载、断点续传、
                       sha 不对重下、自动换源、下载中 stop、CPU 模式不下载
    --runtime-real     显卡运行库真实下载一次（auto 模式，从真实的 PyPI / 镜像下载 1.3GB）
    --keep-runtime     测完不删除 build/test/ 下的运行库目录（每个 2GB 多）

翻译一律用 tools/mock_llm.py（脚本自己启动/关闭）；DeepSeek 只测“没填 Key”的失败路径（会从子进程环境里去掉 DEEPSEEK_API_KEY）。
所有生成物（引擎 stderr 日志、字幕记录、mock 请求记录、运行库目录）都放在 build/test/ 下；
引擎的运行库目录一律用 KIKITORI_RUNTIME_DIR 指到 build/test/ 下，不碰 %LOCALAPPDATA%\\Kikitori。
精简版 exe 自己不带 CUDA：测试时用本地镜像（wheel 缓存在 build/cuda-wheels/，第一次会从网上下载 1.3GB）给它装运行库。
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/
ROOT = HERE.parent                              # 仓库根目录（engine_server.py 所在目录）
TESTDATA = HERE / "testdata"
MANIFEST = ROOT / "packaging" / "cuda_manifest.json"
WHEEL_CACHE = ROOT / "build" / "cuda-wheels"
MOCK_PORT = 8765
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/v1"
MIRROR_PORT = 8767
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
            if sys.platform == "win32":     # 连子进程一起结束（源码模式下 venv 的 python.exe 会再起一个子进程）
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
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
                provider: str = "openai_compatible", compute_type: str | None = None, model: str | None = None,
                device: str | None = None, source: str | None = None):
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
    if device:
        cfg["asr"]["device"] = device
    if source:
        cfg["download"] = {"source": source}
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
    """用临时的 HuggingFace 缓存目录下载 tiny 模型，检查 downloading_model、进度和下载源（官方 / hf-mirror）。
    测完删除临时缓存。"""
    for source, host in (("official", "huggingface.co"), ("china", "hf-mirror.com")):
        cache = out_dir / f"hf-cache-tmp-{source}"
        shutil.rmtree(cache, ignore_errors=True)
        env2 = dict(env, HF_HUB_CACHE=str(cache))
        eng = Engine(cmd, out_dir / f"engine_download_{source}.stderr.log", env2, f"download-{source}")
        try:
            eng.wait_status("idle", 60)
            m = eng.mark()
            t0 = time.time()
            eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=str(TESTDATA / "tts_en.wav"),
                                                            language="en", model="tiny", source=source)})
            eng.wait_status("listening", 900, m)
            evs = eng.since(m)
            dl = [e for e in evs if e.get("event") == "status" and e.get("state") == "downloading_model"]
            check(dl, f"没有 downloading_model：{statuses(evs)}")
            progress = [e.get("progress") for e in dl if "progress" in e]
            check(progress == sorted(progress), f"下载进度不是递增的：{progress}")
            check(any(host in e.get("message", "") for e in dl), f"下载源应该是 {host}：{dl[-1]}")
            say(f"  ✓ 首次下载（{source} → {host}，{time.time() - t0:.0f}s）："
                f"{' → '.join(dict.fromkeys(statuses(evs)))}；进度 {progress}")
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
#  显卡运行库下载（docs/protocol.md 第 7.1 节）
# ======================================================================
def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def ensure_wheel_cache() -> Path:
    """本地镜像用的 wheel 缓存（build/cuda-wheels/）：没有就从清华镜像 / PyPI 下载一次（1.3GB，支持续传），校验 sha256。"""
    import hashlib

    import httpx

    WHEEL_CACHE.mkdir(parents=True, exist_ok=True)
    for p in load_manifest()["packages"]:
        dst = WHEEL_CACHE / p["filename"]
        ok_mark = dst.with_name(dst.name + ".sha256-ok")
        if dst.is_file() and dst.stat().st_size == p["size"] and ok_mark.is_file() \
                and ok_mark.read_text().strip() == p["sha256"]:
            continue
        if not (dst.is_file() and dst.stat().st_size == p["size"]):
            part = dst.with_name(dst.name + ".part")
            say(f"   准备 wheel 缓存：下载 {p['filename']}（{p['size'] / 2**20:.0f} MB）")
            for base in ["https://pypi.tuna.tsinghua.edu.cn/", "https://files.pythonhosted.org/"] * 2:
                have = part.stat().st_size if part.exists() else 0
                try:
                    with httpx.stream("GET", base + p["path"], headers={"Range": f"bytes={have}-"} if have else {},
                                      timeout=httpx.Timeout(30, connect=10), follow_redirects=True) as r:
                        r.raise_for_status()
                        with open(part, "ab" if r.status_code == 206 else "wb") as f:
                            for ch in r.iter_bytes(1 << 20):
                                f.write(ch)
                    if part.stat().st_size == p["size"]:
                        break
                except Exception as e:
                    say(f"   {base} 下载失败：{e}，重试")
            part.replace(dst)
        h = hashlib.sha256()
        with open(dst, "rb") as f:
            for b in iter(lambda: f.read(8 << 20), b""):
                h.update(b)
        check(h.hexdigest() == p["sha256"], f"wheel 缓存校验失败：{dst}")
        ok_mark.write_text(p["sha256"])
    return WHEEL_CACHE


class MirrorServer:
    """本地 HTTP 服务器，模拟 PyPI 镜像：/<模式>/packages/.../<文件名>，支持 Range。

    模式：ok 正常；slow 限速；flaky 正常下载超过 6MB 后直接断开连接（测速那种小的 Range 请求不断）；dead 一律 503。
    """

    def __init__(self, wheel_dir: Path, port: int = MIRROR_PORT, slow_rate: int = 20 << 20):
        self.files = {p.name: p for p in wheel_dir.glob("*.whl")}
        self.port = port
        self.slow_rate = slow_rate
        self.flaky_cut = 6 << 20
        self.log: list[dict] = []
        self._lock = threading.Lock()
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.split("/", 2)
                mode = parts[1] if len(parts) > 1 else ""
                fname = self.path.rsplit("/", 1)[-1]
                if mode == "dead":
                    self.send_error(503)
                    return
                f = srv.files.get(fname)
                if f is None:
                    self.send_error(404)
                    return
                size = f.stat().st_size
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng:
                    m = re.match(r"bytes=(\d+)-(\d*)$", rng.strip())
                    if not m or int(m.group(1)) >= size:
                        self.send_error(416)
                        return
                    start = int(m.group(1))
                    end = min(size - 1, int(m.group(2))) if m.group(2) else size - 1
                entry = {"mode": mode, "file": fname, "start": start, "end": end, "sent": 0, "done": False,
                         "t": time.time()}
                with srv._lock:
                    srv.log.append(entry)
                probe = rng is not None and end - start + 1 <= (2 << 20)
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(f, "rb") as fh:
                        fh.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = fh.read(min(256 << 10, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                            entry["sent"] += len(chunk)
                            if mode == "slow":
                                time.sleep(len(chunk) / srv.slow_rate)
                            if mode == "flaky" and not probe and entry["sent"] >= srv.flaky_cut:
                                self.close_connection = True     # 模拟网络中断：没发完就断开
                                return
                    entry["done"] = left == 0
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    entry["aborted"] = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="mirror").start()

    def base(self, mode: str) -> str:
        return f"http://127.0.0.1:{self.port}/{mode}/"

    def requests(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.log[since:]]

    def mark(self) -> int:
        with self._lock:
            return len(self.log)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def mirrors_env(*entries) -> str:
    return json.dumps([{"name": n, "base": b, "group": g} for n, b, g in entries], ensure_ascii=False)


def runtime_status(evs: list[dict]) -> list[dict]:
    return [e for e in evs if e.get("event") == "status" and e.get("state") == "downloading_runtime"]


def part_sizes(rt: Path) -> dict[str, int]:
    d = rt / "_downloads"
    return {p.name: p.stat().st_size for p in d.glob("*.part")} if d.is_dir() else {}


def check_runtime_installed(rt: Path, name: str):
    man = load_manifest()
    marker = rt / "kikitori-runtime.json"
    check(marker.is_file(), f"[{name}] 没有写标记文件 {marker}")
    for p in man["packages"]:
        for dll, size in p["dlls"].items():
            f = rt / dll
            check(f.is_file() and f.stat().st_size == size, f"[{name}] {dll} 缺失或大小不对")
    check(not (rt / "_downloads").exists(), f"[{name}] 装完后 _downloads 目录应该删掉")
    check(not list(rt.glob("*.whl")), f"[{name}] 装完后不应该留 wheel")


def run_runtime_suite(cmd, env, out_dir: Path, mirror: MirrorServer, runtime_only_env: dict):
    """显卡运行库下载的各种情况（本地镜像）。runtime_only_env：源码模式下要忽略 Python 环境里的 CUDA。"""
    man = load_manifest()
    pk = {p["name"]: p for p in man["packages"]}
    cublas, nvrtc = pk["nvidia-cublas-cu12"], pk["nvidia-cuda-nvrtc-cu12"]
    total_mb = sum(p["size"] for p in man["packages"]) / 2**20
    rt = out_dir / "runtime-A"
    shutil.rmtree(rt, ignore_errors=True)
    zh = str(TESTDATA / "tts_zh.wav")

    def engine(name, rt_dir, mirrors):
        e = dict(env, **runtime_only_env, KIKITORI_RUNTIME_DIR=str(rt_dir), KIKITORI_DOWNLOAD_MIRRORS=mirrors)
        eng = Engine(cmd, out_dir / f"engine_rt_{name}.stderr.log", e, f"rt-{name}")
        eng.wait_status("idle", 60)
        return eng

    def finish(eng):
        eng.send({"cmd": "shutdown"})
        eng.proc.wait(5)
        check(not eng.bad_lines, f"[{eng.name}] stdout 里有非 JSON 的行：{eng.bad_lines[:3]}")
        eng.check_stderr_clean()

    # ---------- 1. CPU 模式：不下载 ----------
    m_req = mirror.mark()
    eng = engine("cpu", rt, mirrors_env(("本地正常", mirror.base("ok"), "china")))
    try:
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, device="cpu", compute_type="int8",
                                                        target="zh-Hant", source="china")})
        eng.wait_status("listening", 300, m)
        f = eng.wait_for(lambda e: e.get("event") == "final", 300, m, " CPU 模式的 final")
        evs = eng.since(m)
        check(not runtime_status(evs), f"CPU 模式不应该下载运行库：{statuses(evs)}")
        check(not mirror.requests(m_req), "CPU 模式不应该有下载请求")
        check(not rt.exists() or not any(rt.iterdir()), f"CPU 模式不应该创建运行库文件：{rt}")
        say(f"  ✓ CPU 模式：{' → '.join(statuses(evs))}，没有下载；识别出 #{f['id']} {f['text']}")
        finish(eng)
    finally:
        eng.kill()

    # ---------- 2. 下载中 stop：及时停下，.part 不再增长 ----------
    eng = engine("stop", rt, mirrors_env(("本地慢速", mirror.base("slow"), "china")))
    try:
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, source="china")})
        eng.wait_for(lambda e: e.get("state") == "downloading_runtime" and e.get("progress", 0) >= 0.02, 60, m,
                     " 下载进度 ≥ 2%")
        t0 = time.time()
        m2 = eng.mark()
        eng.send({"cmd": "stop"})
        eng.wait_status("stopped", 5, m2)
        dt = time.time() - t0
        time.sleep(1.0)
        s1 = part_sizes(rt)
        time.sleep(2.0)
        s2 = part_sizes(rt)
        late = [e for e in eng.since(m2) if e.get("event") == "status" and e.get("state") != "stopped"]
        check(not late, f"stop 之后还有状态事件：{late[:3]}")
        check(s1 and s1 == s2, f"stop 之后 .part 还在变大：{s1} → {s2}")
        rs = runtime_status(eng.since(m))
        progress = [e["progress"] for e in rs]
        check(progress == sorted(progress), f"下载进度不是递增的：{progress}")
        check(re.search(r"\d+ / \d+ MB · 本地慢速", rs[-1]["message"]), f"进度 message 格式不对：{rs[-1]}")
        gaps = [b - a for a, b in zip([t for t, e in eng.events if e.get("state") == "downloading_runtime"],
                                      [t for t, e in eng.events if e.get("state") == "downloading_runtime"][1:])]
        check(not gaps[1:] or min(gaps[1:]) > 0.4, f"downloading_runtime 太频繁（最小间隔 {min(gaps[1:]):.2f}s）")
        say(f"  ✓ 下载中 stop：{dt:.2f}s 内 stopped，之后 .part 不再增长（{sum(s2.values()) / 2**20:.0f} MB 保留）；"
            f"进度示例：{rs[-1]['message']}")
        finish(eng)
    finally:
        eng.kill()

    # ---------- 3. 下载中进程被杀（模拟断网/崩溃）→ .part 保留 ----------
    eng = engine("kill", rt, mirrors_env(("本地慢速", mirror.base("slow"), "china")))
    try:
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, source="china")})
        eng.wait_for(lambda e: e.get("state") == "downloading_runtime" and e.get("progress", 0) >= 0.06, 60, m,
                     " 下载进度 ≥ 6%")
    finally:
        eng.kill()
    time.sleep(0.5)
    killed = part_sizes(rt)
    resume_at = killed.get(cublas["filename"] + ".part", 0)
    check(resume_at > 0, f"进程被杀后应该留下 .part：{killed}")
    # 再放一个内容全错（全 0）但大小正确的 nvrtc .part：应该校验失败、删掉重下
    with open(rt / "_downloads" / (nvrtc["filename"] + ".part"), "wb") as f:
        f.truncate(nvrtc["size"])
    say(f"  ✓ 下载中进程被杀：.part 保留 {resume_at / 2**20:.0f} MB；另放一个 sha 不对的 nvrtc .part")

    # ---------- 4. 续传 + 第一个源中途断开自动换源 + sha 不对重下 + 装完能识别 ----------
    m_req = mirror.mark()
    eng = engine("resume", rt, mirrors_env(("本地不稳", mirror.base("flaky"), "china"),
                                           ("本地正常", mirror.base("ok"), "china")))
    try:
        m = eng.mark()
        t0 = time.time()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, language="zh", target="zh-Hant",
                                                        source="china")})
        eng.wait_status("listening", 600, m)
        evs = eng.since(m)
        sts = statuses(evs)
        check(sts.index("loading_model") > max(i for i, s in enumerate(sts) if s == "downloading_runtime"),
              f"应该先下载运行库再加载模型：{sts}")
        rs = runtime_status(evs)
        progress = [e["progress"] for e in rs]
        check(progress == sorted(progress) and progress[-1] == 1.0, f"下载进度应该递增并以 1.0 结束：{progress}")
        check(any("本地正常" in e["message"] for e in rs), f"应该换到“本地正常”源：{rs[-1]}")
        reqs = mirror.requests(m_req)
        flaky = [r for r in reqs if r["mode"] == "flaky" and r["file"] == cublas["filename"]]
        ok_cb = [r for r in reqs if r["mode"] == "ok" and r["file"] == cublas["filename"]]
        ok_nv = [r for r in reqs if r["mode"] == "ok" and r["file"] == nvrtc["filename"]]
        check(flaky and flaky[0]["start"] == resume_at, f"应该从 {resume_at} 处续传：{flaky}")
        check(ok_cb and ok_cb[0]["start"] > resume_at and ok_cb[0]["done"],
              f"换源后应该接着续传（不是从头下）：{ok_cb}")
        check(ok_nv and ok_nv[-1]["start"] == 0 and ok_nv[-1]["done"], f"sha 不对的 nvrtc 应该从头重下：{ok_nv}")
        served = sum(r["sent"] for r in reqs)
        check_runtime_installed(rt, "resume")
        say(f"  ✓ 续传：本地不稳 从 {resume_at / 2**20:.0f} MB 处接着下 → 断开 → 自动换“本地正常”从 "
            f"{ok_cb[0]['start'] / 2**20:.0f} MB 处续传；sha 不对的 nvrtc 从头重下；"
            f"本次共传 {served / 2**20:.0f}/{total_mb:.0f} MB，{time.time() - t0:.0f}s 到 listening")
        wait_playback_done(eng, m, 120, "runtime-resume")
        finals, partials, trans = check_subtitles(eng.since(m), lang="zh", modes={"translating"}, name="runtime-resume")
        say(f"  ✓ 装好运行库后显卡识别正常：{len(finals)} 个 final，例如 #{finals[0]['id']} {finals[0]['text']}")
        finish(eng)
    finally:
        eng.kill()

    # ---------- 5. 已有运行库：不重复下载 ----------
    m_req = mirror.mark()
    eng = engine("again", rt, mirrors_env(("本地正常", mirror.base("ok"), "china")))
    try:
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, source="china")})
        eng.wait_status("listening", 300, m)
        evs = eng.since(m)
        check(not runtime_status(evs), f"已经装好运行库，不应该再下载：{statuses(evs)}")
        check(not mirror.requests(m_req), "已经装好运行库，不应该有下载请求")
        eng.wait_for(lambda e: e.get("event") == "final", 60, m, " final")
        say(f"  ✓ 已有运行库：{' → '.join(statuses(evs))}，没有下载请求，识别正常")
        finish(eng)
    finally:
        eng.kill()

    # ---------- 6. auto 选源（坏源排后面）；只有坏源时报错并提示换下载源 ----------
    rt_b = out_dir / "runtime-B"
    shutil.rmtree(rt_b, ignore_errors=True)
    eng = engine("auto", rt_b, mirrors_env(("本地坏源", mirror.base("dead"), "official"),
                                          ("本地正常", mirror.base("ok"), "china")))
    try:
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, source="auto")})
        st = eng.wait_for(lambda e: e.get("state") == "downloading_runtime" and "MB ·" in e.get("message", ""),
                          60, m, " 下载进度")
        check("本地正常" in st["message"], f"auto 模式应该选能用的源：{st}")
        m2 = eng.mark()
        eng.send({"cmd": "stop"})
        eng.wait_status("stopped", 5, m2)
        say(f"  ✓ auto 模式：测速后跳过坏源，用“本地正常”：{st['message']}")
        m = eng.mark()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=zh, source="official")})
        err = eng.wait_status("error", 60, m)
        check("切换下载源" in err["message"] and "CPU" in err["message"], f"失败提示应该告诉用户怎么办：{err}")
        say(f"  ✓ 只有坏源（official）：status error：{err['message']}")
        finish(eng)
    finally:
        eng.kill()


def run_runtime_real(cmd, env, out_dir: Path, runtime_only_env: dict) -> dict:
    """真实从网上下载一次显卡运行库（auto 模式），装好后识别 tts_zh.wav。"""
    rt = out_dir / "runtime-real"
    shutil.rmtree(rt, ignore_errors=True)
    e = dict(env, **runtime_only_env, KIKITORI_RUNTIME_DIR=str(rt))
    e.pop("KIKITORI_DOWNLOAD_MIRRORS", None)
    eng = Engine(cmd, out_dir / "engine_rt_real.stderr.log", e, "rt-real")
    try:
        eng.wait_status("idle", 60)
        m = eng.mark()
        t0 = time.time()
        eng.send({"cmd": "start", "config": make_config(out_dir, audio_file=str(TESTDATA / "tts_zh.wav"),
                                                        language="zh", target="zh-Hant", source="auto")})
        st = eng.wait_for(lambda e: e.get("event") == "status" and e.get("state") in ("loading_model", "error"),
                          3600, m, " 运行库下载完成")
        t_dl = time.time() - t0
        rs = runtime_status(eng.since(m))
        check(st["state"] == "loading_model", f"真实下载失败：{st}")
        check(rs and rs[-1].get("progress") == 1.0, f"运行库下载应该以 progress 1.0 结束：{rs[-1:]}")
        src = rs[-1]["message"].rsplit("·", 1)[-1].strip()
        eng.wait_status("listening", 300, m)
        check_runtime_installed(rt, "real")
        f = eng.wait_for(lambda e: e.get("event") == "final", 60, m, " final")
        log_lines = eng.log_path.read_bytes().decode("utf-8", "replace").splitlines()
        say(f"  ✓ 真实下载（auto）：{t_dl:.0f}s，选中下载源：{src}")
        for line in log_lines:
            if "测速" in line or "下载完成" in line:
                say(f"      {line}")
        say(f"  ✓ 装好后显卡识别正常：#{f['id']} {f['text']}")
        eng.send({"cmd": "shutdown"})
        eng.proc.wait(5)
        eng.check_stderr_clean()
        return {"seconds": round(t_dl), "source": src}
    finally:
        eng.kill()


# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="Kikitori 引擎协议测试")
    ap.add_argument("--exe", nargs="?", const="", default=None,
                    help="测试打包后的 exe（不带路径 = build/engine/KikitoriEngine/KikitoriEngine.exe）")
    ap.add_argument("--python", default=sys.executable, help="源码模式用哪个 python（默认当前解释器）")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-reload", action="store_true")
    ap.add_argument("--skip-eof", action="store_true")
    ap.add_argument("--skip-main", action="store_true")
    ap.add_argument("--download-test", action="store_true")
    ap.add_argument("--runtime-test", action="store_true")
    ap.add_argument("--runtime-real", action="store_true")
    ap.add_argument("--keep-runtime", action="store_true")
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
    for k in ("KIKITORI_RUNTIME_ONLY", "KIKITORI_DOWNLOAD_MIRRORS", "KIKITORI_RUNTIME_DIR"):
        env.pop(k, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 运行库目录一律放在 build/test/ 下（不碰 %LOCALAPPDATA%\Kikitori）。运行库专项结束时这里是装好的，主流程接着用；
    # 没跑专项时，精简 exe 的第一次 start 会从本地镜像装一份
    shared_rt = out_dir / "runtime-A"
    env["KIKITORI_RUNTIME_DIR"] = str(shared_rt)
    # 源码模式下测运行库下载，要忽略 Python 环境里 pip 装的 CUDA
    runtime_only_env = {"KIKITORI_RUNTIME_ONLY": "1"} if mode == "source" else {}

    say(f"== 引擎协议测试（{mode}）：{' '.join(cmd)}")
    say(f"   输出目录：{out_dir}")
    mock = MockLLM(out_dir / "mock_llm_requests.log")
    mirror = None
    ok = False
    t0 = time.time()
    real = None
    try:
        if mode == "exe" or args.runtime_test:
            ensure_wheel_cache()
            mirror = MirrorServer(WHEEL_CACHE)
            env["KIKITORI_DOWNLOAD_MIRRORS"] = mirrors_env(("本地镜像", mirror.base("ok"), "official"),
                                                           ("本地镜像", mirror.base("ok"), "china"))
            say(f"   本地镜像：{mirror.base('ok')}（wheel 缓存 {WHEEL_CACHE}）")
        if args.runtime_test:
            say("-- 显卡运行库下载（本地镜像）")
            run_runtime_suite(cmd, env, out_dir, mirror, runtime_only_env)
        if not args.skip_main:
            say("-- 主流程")
            run_main_suite(cmd, env, out_dir, mock, args)
        if not args.skip_eof:
            say("-- stdin EOF")
            run_eof_suite(cmd, env, out_dir)
        if args.download_test:
            say("-- 首次下载模型")
            run_download_suite(cmd, env, out_dir)
        if args.runtime_real:
            say("-- 显卡运行库真实下载（auto）")
            real = run_runtime_real(cmd, env, out_dir, runtime_only_env)
        ok = True
    except TestFailure as e:
        say(f"\n✗ 测试失败：{e}")
    finally:
        mock.stop()
        if mirror is not None:
            mirror.stop()
        if not args.keep_runtime:
            for d in ("runtime-A", "runtime-B", "runtime-real"):
                shutil.rmtree(out_dir / d, ignore_errors=True)
    if real:
        say(f"   真实下载：{real['seconds']}s，下载源 {real['source']}")
    say(f"\n{'全部通过' if ok else '有失败'}（{mode}，用时 {time.time() - t0:.0f}s）。引擎日志在 {out_dir}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
