"""悬浮字幕窗口（tkinter）：置顶、半透明、无边框，可拖动、可缩放、右键菜单。

其它线程只能通过 post_* 方法往队列里塞消息，真正改界面的代码都在主线程里跑。
"""

import ctypes
import json
import os
import queue
import sys
import tkinter as tk
import tkinter.font as tkfont

from .languages import SOURCE_LANGS, TARGET_LANGS, font_for, normalize_source, normalize_target

BG = "#14161a"
FG_ZH = "#ffffff"
FG_JA = "#aab4c3"
FG_PARTIAL = "#7fb0ff"
FG_PENDING = "#6b7380"
FG_STATUS = {"info": "#8fd18f", "warn": "#f0c060", "error": "#ff7b72"}

MAX_ENTRIES = 50


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 高分屏下字不发虚
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _pick_font(families: set, *candidates: str) -> str:
    for c in candidates:
        if c in families:
            return c
    return candidates[-1]


class Overlay:
    def __init__(self, cfg: dict, state_path: str, log_dir: str,
                 default_source: str = "ja", default_target: str = "zh-Hans"):
        self.cfg = cfg
        self.state_path = state_path
        self.log_dir = log_dir
        self.on_pause = lambda paused: None
        self.on_reconnect = lambda: None
        self.on_quit = lambda: None
        self.on_languages = lambda source, target: None

        state = self._load_state()
        # 语言：右键菜单改过的优先，否则用 config.toml 里的
        self.source_lang = normalize_source(state.get("source_lang", default_source))
        self.target_lang = normalize_target(state.get("target_lang", default_target))
        self.font_size = int(state.get("font_size", cfg.get("font_size", 22)))
        self.ja_font_size = int(state.get("ja_font_size", cfg.get("ja_font_size", 14)))
        self.max_lines = int(state.get("max_lines", cfg.get("max_lines", 3)))
        self.opacity = float(state.get("opacity", cfg.get("opacity", 0.8)))

        _enable_dpi_awareness()
        self.root = root = tk.Tk()
        root.title("听译")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.opacity)
        root.configure(bg=BG)

        self.show_ja = tk.BooleanVar(value=bool(state.get("show_japanese", cfg.get("show_japanese", True))))
        self.centered = tk.BooleanVar(value=bool(state.get("centered", cfg.get("align", "left") == "center")))
        self.paused = tk.BooleanVar(value=False)
        self.lines_var = tk.IntVar(value=self.max_lines)
        self.opacity_var = tk.IntVar(value=int(round(self.opacity * 100)))
        self.source_var = tk.StringVar(value=self.source_lang)
        self.target_var = tk.StringVar(value=self.target_lang)

        # zh_font = 译文（大字），ja_font = 原文（小字）；具体字体按语言选
        self._families = set(tkfont.families(root))
        zh_family = _pick_font(self._families, "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "TkDefaultFont")
        self.zh_font = tkfont.Font(root, family=zh_family, size=self.font_size, weight="bold")
        self.ja_font = tkfont.Font(root, family=zh_family, size=self.ja_font_size)
        self.status_font = tkfont.Font(root, family=zh_family, size=9)
        self._apply_language_fonts()

        # ---- 窗口位置/宽度：优先用上次关闭时的。高度随字幕内容自动伸缩，底边固定（像字幕一样往上长）----
        scale = root.winfo_fpixels("1i") / 96.0
        self._pad_top, self._pad_bottom = int(20 * scale), int(10 * scale)
        self._min_h = int(44 * scale)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = int(state.get("width") or cfg.get("width", 960) * scale)
        w = min(max(320, w), sw)
        x = int(state.get("x", (sw - w) // 2))
        bottom = int(state.get("bottom", sh - int(90 * scale)))
        # 防止上次在副屏关掉、这次副屏没接导致窗口跑到屏幕外
        x = min(max(0, x), max(0, sw - w))
        bottom = min(max(self._min_h, bottom), sh)
        root.geometry(f"{w}x{self._min_h}+{x}+{bottom - self._min_h}")

        # ---- 布局 ----
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=int(16 * scale), pady=(self._pad_top, self._pad_bottom))
        self.rows = []
        self._build_rows()

        self.placeholder = tk.Label(self.body, text="（等待声音…  右键打开菜单）", bg=BG, fg=FG_PENDING,
                                    font=self.status_font)

        self.status = tk.Label(root, text="", bg=BG, fg=FG_STATUS["info"], font=self.status_font, anchor="w")
        self.status.place(x=int(10 * scale), y=int(2 * scale))
        self._status_after = None

        self.grip = tk.Label(root, text="↔", bg=BG, fg="#555b66", cursor="size_we",
                             font=tkfont.Font(root, size=10))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        # ---- 鼠标操作 ----
        self._drag = None
        root.bind_all("<ButtonPress-1>", self._on_press)
        root.bind_all("<B1-Motion>", self._on_motion)
        root.bind_all("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        root.bind_all("<Button-3>", self._show_menu)
        root.bind("<Configure>", self._on_configure)
        self._build_menu()

        self.entries: dict[int, dict] = {}   # seg_id -> {ja, zh, partial, pending}
        self.q: "queue.Queue[tuple]" = queue.Queue()
        root.after(40, self._poll)
        root.after(2000, self._keep_on_top)
        root.after(50, self._render)
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # ================== 给其它线程用的接口（线程安全） ==================
    def post_partial(self, seg_id, ja):
        self.q.put(("partial", seg_id, ja))

    def post_final(self, seg_id, ja, pending: bool):
        self.q.put(("final", seg_id, ja, pending))

    def post_translation(self, seg_id, zh):
        self.q.put(("zh", seg_id, zh))

    def post_remove(self, seg_id):
        self.q.put(("remove", seg_id))

    def post_status(self, text, level="info", timeout: float | None = None):
        self.q.put(("status", text, level, timeout))

    # ================== 主线程 ==================
    def run(self):
        self.root.mainloop()

    def _poll(self):
        changed = False
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "partial":
                    _, sid, ja = msg
                    e = self.entries.get(sid)
                    if e is None:
                        self.entries[sid] = {"ja": ja, "zh": "", "partial": True, "pending": False}
                    elif e["partial"]:
                        e["ja"] = ja
                    changed = True
                elif kind == "final":
                    _, sid, ja, pending = msg
                    self.entries[sid] = {"ja": ja, "zh": "", "partial": False, "pending": pending}
                    changed = True
                elif kind == "zh":
                    _, sid, zh = msg
                    if sid in self.entries:
                        self.entries[sid].update(zh=zh, pending=False)
                        changed = True
                elif kind == "remove":
                    if self.entries.pop(msg[1], None) is not None:
                        changed = True
                elif kind == "status":
                    self._set_status(*msg[1:])
        except queue.Empty:
            pass
        if changed:
            # entries 按 seg_id 排序（id 递增 = 时间顺序），只留最近的一些
            if len(self.entries) > MAX_ENTRIES:
                for sid in sorted(self.entries)[: len(self.entries) - MAX_ENTRIES]:
                    del self.entries[sid]
            self._render()
        self.root.after(40, self._poll)

    def _build_rows(self):
        for r in self.rows:
            r["frame"].destroy()
        self.rows = []
        for _ in range(self.max_lines):
            f = tk.Frame(self.body, bg=BG)
            ja = tk.Label(f, bg=BG, fg=FG_JA, font=self.ja_font, justify="left", anchor="w")
            zh = tk.Label(f, bg=BG, fg=FG_ZH, font=self.zh_font, justify="left", anchor="w")
            self.rows.append({"frame": f, "ja": ja, "zh": zh})
        self._apply_wrap()

    def _render(self):
        show_ja = self.show_ja.get()
        center = self.centered.get()
        justify, anchor = ("center", "center") if center else ("left", "w")

        items = [self.entries[k] for k in sorted(self.entries)]
        if not show_ja:   # 不看日文时，草稿行没有意义
            items = [e for e in items if not e["partial"]]
        items = items[-self.max_lines:]

        for r in self.rows:
            r["frame"].pack_forget()
            r["ja"].pack_forget()
            r["zh"].pack_forget()
        self.placeholder.pack_forget()
        if not items:
            self.placeholder.pack(side="bottom", anchor="w")

        # rows[0] 放最新一条，贴着窗口底部，旧的往上推
        for r, e in zip(self.rows, reversed(items)):
            r["frame"].pack(side="bottom", fill="x", pady=(0, 6))
            if (show_ja or e["partial"]) and e["ja"]:
                r["ja"].config(text=e["ja"] + (" …" if e["partial"] else ""),
                               fg=FG_PARTIAL if e["partial"] else FG_JA, justify=justify, anchor=anchor)
                r["ja"].pack(side="top", fill="x")
            if not e["partial"]:
                if e["pending"]:
                    r["zh"].config(text="…", fg=FG_PENDING, justify=justify, anchor=anchor)
                    r["zh"].pack(side="top", fill="x")
                elif e["zh"]:
                    r["zh"].config(text=e["zh"], fg=FG_ZH, justify=justify, anchor=anchor)
                    r["zh"].pack(side="top", fill="x")
        self._fit_height()

    def _fit_height(self):
        """按内容调整窗口高度，保持底边不动。"""
        self.root.update_idletasks()
        need = max(self._min_h, self.body.winfo_reqheight() + self._pad_top + self._pad_bottom)
        need = min(need, self.root.winfo_screenheight())
        h = self.root.winfo_height()
        if abs(need - h) < 2:
            return
        x, y, w = self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width()
        self.root.geometry(f"{w}x{need}+{x}+{y + h - need}")

    def _set_status(self, text, level="info", timeout=None):
        if self._status_after:
            self.root.after_cancel(self._status_after)
            self._status_after = None
        self.status.config(text=text, fg=FG_STATUS.get(level, FG_STATUS["info"]))
        self.status.lift()
        if timeout:
            self._status_after = self.root.after(int(timeout * 1000), lambda: self.status.config(text=""))

    def _apply_wrap(self):
        w = max(200, self.body.winfo_width() or self.root.winfo_width() - 40)
        changed = False
        for r in self.rows:
            if str(r["zh"].cget("wraplength")) != str(w):
                r["ja"].config(wraplength=w)
                r["zh"].config(wraplength=w)
                changed = True
        if changed:
            self._fit_height()

    def _on_configure(self, e):
        if e.widget is self.root:
            self.root.after_idle(self._apply_wrap)

    def _keep_on_top(self):
        # 有些全屏播放器/其它置顶窗口会把我们盖住，定期重新置顶
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(2000, self._keep_on_top)

    # ---------------- 拖动 / 缩放 ----------------
    def _on_press(self, e):
        r = self.root
        mode = "resize" if e.widget is self.grip else "move"
        self._drag = (mode, e.x_root, e.y_root, r.winfo_x(), r.winfo_y(), r.winfo_width(), r.winfo_height())

    def _on_motion(self, e):
        if not self._drag:
            return
        mode, x0, y0, wx, wy, ww, wh = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if mode == "move":
            self.root.geometry(f"+{wx + dx}+{wy + dy}")
        else:   # 只改宽度，高度跟着内容自动变
            self.root.geometry(f"{max(320, ww + dx)}x{wh}")

    # ---------------- 右键菜单 ----------------
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_checkbutton(label="暂停识别", variable=self.paused,
                          command=lambda: self._toggle_pause())
        m.add_separator()
        src = tk.Menu(m, tearoff=0)
        for code, name, _ in SOURCE_LANGS:
            src.add_radiobutton(label=name, value=code, variable=self.source_var,
                                command=self._change_languages)
        m.add_cascade(label="说话语言", menu=src)
        tgt = tk.Menu(m, tearoff=0)
        for code, name, _ in TARGET_LANGS:
            tgt.add_radiobutton(label=name, value=code, variable=self.target_var,
                                command=self._change_languages)
        m.add_cascade(label="翻译成", menu=tgt)
        m.add_separator()
        m.add_checkbutton(label="显示原文", variable=self.show_ja, command=self._render)
        m.add_checkbutton(label="字幕居中", variable=self.centered, command=self._render)

        size = tk.Menu(m, tearoff=0)
        size.add_command(label="放大  A+", command=lambda: self._change_font(+2))
        size.add_command(label="缩小  A-", command=lambda: self._change_font(-2))
        m.add_cascade(label="字号", menu=size)

        lines = tk.Menu(m, tearoff=0)
        for n in range(1, 7):
            lines.add_radiobutton(label=f"{n} 条", value=n, variable=self.lines_var,
                                  command=self._change_lines)
        m.add_cascade(label="显示条数", menu=lines)

        op = tk.Menu(m, tearoff=0)
        for v in (100, 90, 80, 70, 60, 50, 40):
            op.add_radiobutton(label=f"{v}%", value=v, variable=self.opacity_var,
                               command=self._change_opacity)
        m.add_cascade(label="不透明度", menu=op)

        m.add_separator()
        m.add_command(label="清空字幕", command=self._clear)
        m.add_command(label="重新连接音频设备", command=lambda: self.on_reconnect())
        m.add_command(label="打开字幕记录文件夹", command=self._open_logs)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    def _show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_pause(self):
        p = self.paused.get()
        self.on_pause(p)
        if p:
            self._set_status("⏸ 已暂停（右键 → 取消“暂停识别”继续）", "warn")
        else:
            self._set_status("▶ 继续识别", "info", 3)

    def _change_languages(self):
        self.source_lang = self.source_var.get()
        self.target_lang = self.target_var.get()
        self._apply_language_fonts()
        self.on_languages(self.source_lang, self.target_lang)
        self._fit_height()
        self._save_state()   # 马上记住，不用等正常退出

    def _apply_language_fonts(self):
        """同一个汉字中日字形不同、韩文要有韩文字形，所以原文和译文按语言各选字体。"""
        self.ja_font.config(family=_pick_font(self._families, *font_for(self.source_lang)))
        self.zh_font.config(family=_pick_font(self._families, *font_for(self.target_lang)))

    def _change_font(self, d):
        self.font_size = min(60, max(10, self.font_size + d))
        self.ja_font_size = min(40, max(8, self.ja_font_size + (1 if d > 0 else -1)))
        self.zh_font.config(size=self.font_size)
        self.ja_font.config(size=self.ja_font_size)
        self._fit_height()

    def _change_lines(self):
        self.max_lines = self.lines_var.get()
        self._build_rows()
        self._render()

    def _change_opacity(self):
        self.opacity = self.opacity_var.get() / 100
        self.root.attributes("-alpha", self.opacity)

    def _clear(self):
        self.entries.clear()
        self._render()

    def _open_logs(self):
        os.makedirs(self.log_dir, exist_ok=True)
        os.startfile(self.log_dir)

    # ---------------- 状态保存 / 退出 ----------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        state = {
            "x": self.root.winfo_x(),
            "bottom": self.root.winfo_y() + self.root.winfo_height(),
            "width": self.root.winfo_width(),
            "font_size": self.font_size,
            "ja_font_size": self.ja_font_size,
            "max_lines": self.max_lines,
            "opacity": self.opacity,
            "show_japanese": self.show_ja.get(),
            "centered": self.centered.get(),
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("[overlay] 保存窗口状态失败：", e)

    def quit(self):
        self._save_state()
        try:
            self.on_quit()
        finally:
            self.root.destroy()
