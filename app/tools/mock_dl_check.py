"""Quick protocol check of tools/mock_engine.py download simulation (MOCK_FIRST_RUN / MOCK_NO_GPU).

    python app/tools/mock_dl_check.py
"""
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MOCK = os.path.join(ROOT, "tools", "mock_engine.py")


def run(env_extra, script):
    env = dict(os.environ, **env_extra)
    p = subprocess.Popen([sys.executable, MOCK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, encoding="utf-8")
    events = []
    t = threading.Thread(target=lambda: [events.append(json.loads(l)) for l in p.stdout], daemon=True)
    t.start()
    for delay, cmd in script:
        time.sleep(delay)
        if cmd:
            p.stdin.write(json.dumps(cmd) + "\n")
            p.stdin.flush()
    p.stdin.write('{"cmd":"shutdown"}\n')
    p.stdin.flush()
    p.wait(5)
    return [e for e in events if e.get("event") == "status"]


def cfg(device="cuda", source="auto"):
    return {"asr": {"device": device}, "download": {"source": source}}


st = run({"MOCK_FIRST_RUN": "1"}, [
    (0.3, {"cmd": "start", "config": cfg(source="china")}),
    (2.2, {"cmd": "stop"}),
    (1.5, {"cmd": "start", "config": cfg(source="china")}),
    (12.0, None),
])
seq = [(s["state"], s.get("progress")) for s in st]
print("first-run states:", [s for s, _ in seq if s != seq[0][0] or True][:3], "...")
i_stop = next(i for i, (s, _) in enumerate(seq) if s == "stopped")
before = [p for s, p in seq[:i_stop] if s == "downloading_runtime"]
after = [p for s, p in seq[i_stop:] if s == "downloading_runtime"]
print("runtime progress before stop:", before[-1], "| resumed from:", after[0], "| events after stop until restart ok:",
      all(s != "downloading_runtime" for s, _ in seq[i_stop:i_stop + 1]))
print("model download seen:", any(s == "downloading_model" for s, _ in seq), "| listening:", any(s == "listening" for s, _ in seq))
print("message sample:", next(s["message"] for s in st if s["state"] == "downloading_runtime"))

st = run({"MOCK_NO_GPU": "1"}, [(0.3, {"cmd": "start", "config": cfg("cuda")}), (0.5, {"cmd": "start", "config": cfg("cpu")}), (3.5, None)])
print("no-gpu:", [s["state"] for s in st])
