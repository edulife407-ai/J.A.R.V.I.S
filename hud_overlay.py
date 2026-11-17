# hud_overlay.py
"""
JARVIS GLASSMORPHIC FLOATING HUD OVERLAY
- Automatically intercepts terminal output (You, Jarvis, Actions, Thoughts, Errors)
- Streams conversation word-by-word in a compact, draggable frosted-glass window
- Uses Win32 WDA_EXCLUDEFROMCAPTURE so Jarvis's Optic Nerve screenshots see right through it!
"""
import os
import re
import sys
import json
import time
import socket
import ctypes
import atexit
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import font as tkfont

HUD_UDP_IP = "127.0.0.1"
HUD_UDP_PORT = 18765


# =====================================================================
# PART 1: TERMINAL STDOUT INTERCEPTOR (RUNS INSIDE LAPTOP_CLIENT.PY)
# =====================================================================
class _HUDStdoutTee:
    """Forwards every terminal print() line to the floating HUD via 0.05ms non-blocking UDP."""
    def __init__(self, original_stream):
        self.original_stream = original_stream
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._buf = ""

    def write(self, data: str):
        try:
            self.original_stream.write(data)
        except Exception:
            pass

        if not data:
            return

        self._buf += data
        while "\n" in self._buf or "\r" in self._buf:
            sep_idx = min(
                [i for i in (self._buf.find("\n"), self._buf.find("\r")) if i != -1]
            )
            sep_char = self._buf[sep_idx]
            line = self._buf[:sep_idx]
            self._buf = self._buf[sep_idx + 1:]

            clean = line.strip()
            if not clean:
                continue
            # Skip repetitive Barge-In meter spam unless active frames > 0
            if "[Barge-In Meter]" in clean and "Active Frames: 0/" in clean:
                continue

            is_carriage = (sep_char == "\r")
            payload = json.dumps({"line": clean, "transient": is_carriage}).encode("utf-8")
            try:
                self.sock.sendto(payload[:1400], (HUD_UDP_IP, HUD_UDP_PORT))
            except Exception:
                pass

    def flush(self):
        try:
            self.original_stream.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()


_HUD_PROCESS = None


def start_jarvis_hud():
    """
    Call this once at the top of `laptop_client.py`.
    Spawns the Glass HUD in an isolated process (zero GIL lag) and hooks sys.stdout/stderr.
    """
    global _HUD_PROCESS
    if "--no-hud" in sys.argv:
        return

    hud_script = os.path.abspath(__file__)
    try:
        creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        _HUD_PROCESS = subprocess.Popen(
            [sys.executable, hud_script, "--hud-worker"],
            creationflags=creation_flags,
        )
        atexit.register(lambda: _HUD_PROCESS.terminate() if _HUD_PROCESS else None)
    except Exception as e:
        print(f"[HUD Launch Warning] {e}")
        return

    sys.stdout = _HUDStdoutTee(sys.stdout)
    sys.stderr = _HUDStdoutTee(sys.stderr)


# =====================================================================
# PART 2: STANDALONE GLASSMORPHIC HUD WINDOW (RUNS IN HUD WORKER)
# =====================================================================
class JarvisGlassHUD:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("JARVIS HUD")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.88)
        self.root.configure(bg="#090d16")

        # Compact dimensions: 410x255 default, positioned neatly in the bottom-right corner
        self.full_w = 410
        self.full_h = 255
        self.mini_w = 235
        self.mini_h = 36
        self.is_collapsed = False

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        start_x = max(20, sw - self.full_w - 28)
        start_y = max(20, sh - self.full_h - 68)
        self.root.geometry(f"{self.full_w}x{self.full_h}+{start_x}+{start_y}")

        self._drag_x = 0
        self._drag_y = 0
        self.msg_queue = queue.Queue()
        self._typewriter_job = None
        self._pending_words = []
        self._active_tag = "jarvis"

        self._build_ui()
        self.root.update_idletasks()
        self._apply_win32_glass_and_stealth()

        # Start UDP listener thread
        threading.Thread(target=self._udp_listener_loop, daemon=True).start()
        self.root.after(40, self._process_queue)

    def _apply_win32_glass_and_stealth(self):
        """
        1) Applies Windows 11 rounded corners (DWMWA_WINDOW_CORNER_PREFERENCE).
        2) Applies WDA_EXCLUDEFROMCAPTURE (0x11) so Jarvis's mss screenshots NEVER see this HUD!
        """
        if os.name != "nt":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()

            # 1. Windows 11 Rounded Corners (DWMWCP_ROUND = 2)
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(preference), ctypes.sizeof(preference)
            )

            # 2. Exclude HUD from screenshots so Optic Nerve sees straight through it!
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    def _build_ui(self):
        # Outer 1px glowing cyan-glass border frame
        self.border_frame = tk.Frame(
            self.root, bg="#090d16", highlightbackground="#1e3a5f", highlightthickness=1
        )
        self.border_frame.pack(fill=tk.BOTH, expand=True)

        # ---------------- TOP HEADER BAR (DRAGGABLE) ----------------
        self.header = tk.Frame(self.border_frame, bg="#0f172a", height=34)
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.pack_propagate(False)

        self.status_dot = tk.Label(
            self.header, text="●", fg="#38bdf8", bg="#0f172a", font=("Segoe UI", 11, "bold")
        )
        self.status_dot.pack(side=tk.LEFT, padx=(10, 4))

        self.title_lbl = tk.Label(
            self.header,
            text="J.A.R.V.I.S.",
            fg="#e2e8f0",
            bg="#0f172a",
            font=("Consolas", 9, "bold"),
        )
        self.title_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self.state_badge = tk.Label(
            self.header,
            text="STANDBY",
            fg="#38bdf8",
            bg="#172554",
            font=("Consolas", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.state_badge.pack(side=tk.LEFT, pady=6)

        # Right control buttons: Collapse [_], Clear [C], Close [×]
        btn_close = tk.Label(
            self.header, text="×", fg="#64748b", bg="#0f172a", font=("Segoe UI", 12, "bold"), cursor="hand2", padx=6
        )
        btn_close.pack(side=tk.RIGHT, padx=(0, 4))
        btn_close.bind("<Button-1>", lambda e: self.root.destroy())

        self.btn_collapse = tk.Label(
            self.header, text="–", fg="#94a3b8", bg="#0f172a", font=("Consolas", 10, "bold"), cursor="hand2", padx=6
        )
        self.btn_collapse.pack(side=tk.RIGHT)
        self.btn_collapse.bind("<Button-1>", lambda e: self._toggle_collapse())

        btn_clear = tk.Label(
            self.header, text="CLR", fg="#64748b", bg="#0f172a", font=("Consolas", 8, "bold"), cursor="hand2", padx=5
        )
        btn_clear.pack(side=tk.RIGHT)
        btn_clear.bind("<Button-1>", lambda e: self._clear_log())

        # Bind dragging to header and labels
        for w in (self.header, self.status_dot, self.title_lbl, self.state_badge):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)

        # ---------------- BODY CONTAINER ----------------
        self.body = tk.Frame(self.border_frame, bg="#090d16")
        self.body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))

        # Live Action / Telemetry Strip
        self.telemetry_frame = tk.Frame(
            self.body, bg="#111827", highlightbackground="#1e293b", highlightthickness=1
        )
        self.telemetry_frame.pack(fill=tk.X, side=tk.TOP, pady=(0, 6))

        self.telemetry_lbl = tk.Label(
            self.telemetry_frame,
            text="⚡ Ready — Say 'Hey Jarvis' or press Ctrl+Alt+J",
            fg="#94a3b8",
            bg="#111827",
            font=("Consolas", 8),
            anchor="w",
            padx=6,
            pady=3,
        )
        self.telemetry_lbl.pack(fill=tk.X)

        # Conversation Log Text Box (Word-by-Word Stream)
        self.log_text = tk.Text(
            self.body,
            bg="#090d16",
            fg="#e2e8f0",
            font=("Segoe UI", 9),
            bd=0,
            highlightthickness=0,
            wrap=tk.WORD,
            cursor="arrow",
            state=tk.DISABLED,
            spacing1=2,
            spacing3=4,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Allow dragging from the text body as well
        self.log_text.bind("<ButtonPress-1>", self._start_drag)
        self.log_text.bind("<B1-Motion>", self._on_drag)

        # Color tags for conversation & telemetry
        self.log_text.tag_configure("user_tag", foreground="#38bdf8", font=("Consolas", 8, "bold"))
        self.log_text.tag_configure("user_msg", foreground="#e0f2fe", font=("Segoe UI", 9, "bold"))
        self.log_text.tag_configure("jarvis_tag", foreground="#34d399", font=("Consolas", 8, "bold"))
        self.log_text.tag_configure("jarvis_msg", foreground="#d1fae5", font=("Segoe UI", 9))
        self.log_text.tag_configure("action_msg", foreground="#94a3b8", font=("Consolas", 8))
        self.log_text.tag_configure("error_msg", foreground="#f87171", font=("Consolas", 8, "bold"))
        self.log_text.tag_configure("barge_msg", foreground="#fbbf24", font=("Consolas", 8, "bold"))

        self._append_static_line("SYSTEM", "Glass HUD attached. Invisible to Optic Nerve.", "action_msg")

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _toggle_collapse(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if not self.is_collapsed:
            self.body.pack_forget()
            self.root.geometry(f"{self.mini_w}x{self.mini_h}+{x}+{y}")
            self.btn_collapse.config(text="□")
            self.is_collapsed = True
        else:
            self.root.geometry(f"{self.full_w}x{self.full_h}+{x}+{y}")
            self.body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))
            self.btn_collapse.config(text="–")
            self.is_collapsed = False

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.telemetry_lbl.config(text="⚡ Log cleared — Listening...", fg="#94a3b8")

    def _set_state_badge(self, label: str, fg_color: str, bg_color: str, border_color: str = "#1e3a5f"):
        self.state_badge.config(text=label, fg=fg_color, bg=bg_color)
        self.status_dot.config(fg=fg_color)
        self.border_frame.config(highlightbackground=border_color)

    def _append_static_line(self, prefix: str, text: str, tag: str):
        self.log_text.config(state=tk.NORMAL)
        if prefix:
            self.log_text.insert(tk.END, f"[{prefix}] ", tag)
        self.log_text.insert(tk.END, f"{text}\n", tag)
        self._trim_old_lines()
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _start_word_by_word_stream(self, speaker: str, full_text: str):
        """Streams YOU or JARVIS dialogue word-by-word across the HUD."""
        # Flush any previous unfinished typewriter words immediately
        if self._pending_words:
            self.log_text.config(state=tk.NORMAL)
            rem = " ".join(self._pending_words) + "\n"
            self.log_text.insert(tk.END, rem, self._active_tag)
            self.log_text.config(state=tk.DISABLED)
            self._pending_words.clear()
        if self._typewriter_job:
            try:
                self.root.after_cancel(self._typewriter_job)
            except Exception:
                pass
            self._typewriter_job = None

        self.log_text.config(state=tk.NORMAL)
        if speaker == "YOU":
            self.log_text.insert(tk.END, "🎙️ YOU: ", "user_tag")
            self._active_tag = "user_msg"
        else:
            self.log_text.insert(tk.END, "🔊 JARVIS: ", "jarvis_tag")
            self._active_tag = "jarvis_msg"
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

        words = full_text.strip().split()
        self._pending_words = words
        self._step_typewriter()

    def _step_typewriter(self):
        if not self._pending_words:
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, "\n", self._active_tag)
            self._trim_old_lines()
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
            self._typewriter_job = None
            return

        word = self._pending_words.pop(0)
        sep = " " if self._pending_words else ""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, word + sep, self._active_tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

        # ~32ms per word gives a crisp, real-time movie HUD caption effect
        self._typewriter_job = self.root.after(32, self._step_typewriter)

    def _trim_old_lines(self):
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 60:
            self.log_text.delete("1.0", f"{lines - 50}.0")

    def _udp_listener_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HUD_UDP_IP, HUD_UDP_PORT))
        except Exception:
            return
        while True:
            try:
                data, _ = sock.recvfrom(2048)
                msg = json.loads(data.decode("utf-8", errors="ignore"))
                self.msg_queue.put(msg)
            except Exception:
                continue

    def _process_queue(self):
        try:
            while not self.msg_queue.empty():
                item = self.msg_queue.get_nowait()
                self._parse_terminal_line(item.get("line", ""), item.get("transient", False))
        except Exception:
            pass
        self.root.after(35, self._process_queue)

    def _parse_terminal_line(self, line: str, is_transient: bool):
        if not line:
            return

        # 1. Transient spinner / Barge-In meter updates -> show in the top telemetry strip only
        if is_transient or "[Jarvis]" in line or "[Neural Core" in line or "[Barge-In Meter]" in line:
            clean_t = re.sub(r"^\[Jarvis\]\s*[|/\\\-\s]*", "🧠 ", line)
            self.telemetry_lbl.config(text=clean_t[:58], fg="#38bdf8")
            return

        # 2. User Verified Speech: 🎙️ [You (475ms | Neural-Match: 0.61)]: "Hey Jarvis..."
        m_you = re.search(r"\[You\s*\(([^)]+)\)\]:\s*\"(.+)\"", line)
        if m_you:
            meta, spoken = m_you.group(1), m_you.group(2)
            self._set_state_badge("HEARD YOU", "#38bdf8", "#0c4a6e", "#0284c7")
            self.telemetry_lbl.config(text=f"🎙️ Verified ({meta})", fg="#38bdf8")
            self._start_word_by_word_stream("YOU", spoken)
            return

        # 3. Jarvis Speaking: 🔊 [JARVIS | Style=NORMAL | Speed=0%]: "..."
        m_jarvis = re.search(r"🔊\s*\[JARVIS[^\]]*\]:\s*\"(.+)\"", line)
        if m_jarvis:
            spoken = m_jarvis.group(1).replace("[LOADING] ", "")
            self._set_state_badge("SPEAKING", "#34d399", "#064e3b", "#10b981")
            self._start_word_by_word_stream("JARVIS", spoken)
            return

        # 4. Barge-In Interrupt: ⚡ [BARGE-IN INTERRUPT]
        if "[BARGE-IN INTERRUPT]" in line:
            self._set_state_badge("BARGE-IN", "#fbbf24", "#78350f", "#f59e0b")
            self.telemetry_lbl.config(text="⚡ Interrupted by your voice — Listening...", fg="#fbbf24")
            self._append_static_line("INTERRUPT", "Cut off Jarvis mid-sentence", "barge_msg")
            return

        # 5. Executing Goal Header: === JARVIS EXECUTING: '...' ===
        m_exec = re.search(r"===\s*JARVIS EXECUTING:\s*'(.+)'\s*===", line)
        if m_exec:
            goal = m_exec.group(1)
            self._set_state_badge("EXECUTING", "#c084fc", "#3b0764", "#9333ea")
            self.telemetry_lbl.config(text=f"⚙️ Goal: {goal[:46]}", fg="#c084fc")
            return

        # 6. Brain Thought / Fast-Router / Optic Nerve
        if "Step 1 Thought:" in line or "[Optic Nerve" in line:
            clean_thought = line.replace("Step 1 Thought:", "").strip()
            self.telemetry_lbl.config(text=f"🧠 {clean_thought[:52]}", fg="#a78bfa")
            return

        # 7. Skill Action & Result
        if "[Action] Executing" in line:
            act = line.replace("[Action]", "").strip()
            self.telemetry_lbl.config(text=f"⚡ {act[:52]}", fg="#38bdf8")
            return

        if "[Result]" in line:
            res = line.replace("[Result]", "").strip()
            self.telemetry_lbl.config(text=f"✅ {res[:52]}", fg="#34d399")
            self._append_static_line("ACTION", res, "action_msg")
            return

        if "Turn finished: COMPLETED" in line:
            self._set_state_badge("LISTENING", "#38bdf8", "#172554", "#1e3a5f")
            return

        # 8. Standby / Detach / Errors / Security Gate
        if "[Live Session Timeout]" in line or "Going on standby" in line:
            self._set_state_badge("STANDBY", "#94a3b8", "#1e293b", "#1e3a5f")
            self.telemetry_lbl.config(text="💤 Standby — Say 'Jarvis' or press Ctrl+Alt+J", fg="#94a3b8")
            return

        if "[Voice Module DETACHED]" in line:
            self._set_state_badge("HOTKEY ONLY", "#fbbf24", "#451a03", "#d97706")
            self.telemetry_lbl.config(text="🔌 Voice Detached (Press Ctrl+Alt+V to attach)", fg="#fbbf24")
            return

        if "[Voice Module RE-ATTACHED]" in line:
            self._set_state_badge("LISTENING", "#38bdf8", "#172554", "#0284c7")
            self.telemetry_lbl.config(text="🔌 Voice Re-Attached — Hands-free active", fg="#38bdf8")
            return

        if any(err_k in line for err_k in ["Error", "ERROR", "⚠️", "[Security Gate]", "Safety Abort"]):
            self._set_state_badge("ALERT", "#f87171", "#450a0a", "#ef4444")
            self.telemetry_lbl.config(text=f"⚠️ {line[:52]}", fg="#f87171")
            self._append_static_line("ALERT", line[:90], "error_msg")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    hud = JarvisGlassHUD()
    hud.run()