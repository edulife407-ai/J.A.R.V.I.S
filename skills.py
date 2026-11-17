# skills.py
import os
import re
import math
import json
import time
import ctypes
import sqlite3
import urllib.parse
import subprocess
from datetime import datetime, timedelta
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

# Lock process to 1:1 physical screen pixels
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    import uiautomation as auto
    HAS_UIA = True
except Exception:
    HAS_UIA = False

kb = KeyboardController()
mouse = MouseController()

MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_MEDIA_PLAY_PAUSE = 0xB3  # System-wide global pause/play

try:
    ctypes.windll.user32.mouse_event.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_ulong,
    ]
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")
SCHEDULES_FILE = os.path.join(BASE_DIR, "schedules.json")
INSTRUCTIONS_FILE = os.path.join(BASE_DIR, "instructions.md")
WORKSPACE_FILE = os.path.join(BASE_DIR, "workspace.md")
DB_FILE = os.path.join(BASE_DIR, "jarvis_core.db")
CONFIRM_FILE = os.path.join(BASE_DIR, "pending_confirmation.json")

CDP_PORT = 9222
CDP_BASE_URL = f"http://127.0.0.1:{CDP_PORT}"

BROWSER_EXE_MAP = {
    "brave": "brave",
    "edge": "msedge",
    "msedge": "msedge",
    "microsoft edge": "msedge",
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
}

FAST_APP_LAUNCH_MAP = {
    "brave": f"brave --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "brave browser": f"brave --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "edge": f"msedge --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "microsoft edge": f"msedge --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "chrome": f"chrome --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "google chrome": f"chrome --remote-debugging-port={CDP_PORT} --force-renderer-accessibility",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "explorer": "explorer",
    "file explorer": "explorer",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
    "code": "code",
    "vscode": "code",
    "visual studio code": "code",
}

FAST_APP_CLOSE_MAP = {
    "notepad": ["notepad.exe"],
    "brave": ["brave.exe"],
    "edge": ["msedge.exe"],
    "chrome": ["chrome.exe"],
    "firefox": ["firefox.exe"],
    "calculator": ["CalculatorApp.exe", "calc.exe"],
    "calc": ["CalculatorApp.exe", "calc.exe"],
    "paint": ["mspaint.exe"],
    "code": ["Code.exe"],
    "vscode": ["Code.exe"],
    "cmd": ["cmd.exe"],
}

COLOR_RGB_ANCHORS = {
    "red": (200, 45, 45),
    "blue": (40, 90, 200),
    "navy": (25, 45, 110),
    "green": (45, 160, 65),
    "yellow": (220, 195, 45),
    "black": (30, 30, 30),
    "white": (235, 235, 235),
    "grey": (128, 128, 128),
    "pink": (220, 110, 160),
    "orange": (230, 120, 40),
    "purple": (130, 50, 160),
    "brown": (120, 75, 45),
    "maroon": (115, 25, 35),
}


def _send_ctrl_shortcut(char_key: str, shift: bool = False):
    """
    Fires hardware-level Win32 virtual keycodes (VK_CONTROL + VK_KEY) so Google Chrome/Brave
    NEVER ignores Ctrl+W, Ctrl+L, Ctrl+T, Ctrl+A, or Ctrl+V.
    """
    user32 = ctypes.windll.user32
    vk_map = {
        "w": 0x57,
        "t": 0x54,
        "l": 0x4C,
        "a": 0x41,
        "c": 0x43,
        "v": 0x56,
        "tab": 0x09,
    }
    vk = vk_map.get(char_key.lower(), ord(char_key.upper()[0]))
    # Ensure Escape closes any accidentally highlighted Chrome menu bar first if closing a tab
    if char_key.lower() == "w":
        user32.keybd_event(0x1B, 0, 0, 0)  # VK_ESCAPE down
        user32.keybd_event(0x1B, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)

    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    if shift:
        user32.keybd_event(VK_SHIFT, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if shift:
        user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

def _toggle_media_play_pause():
    """Fires the global Windows media key to pause background audio (PW, Spotify, etc.) instantly."""
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)

def init_sqlite_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS exams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_name TEXT NOT NULL,
                exam_date TEXT NOT NULL,
                notes TEXT DEFAULT '',
                status TEXT DEFAULT 'UPCOMING'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS syllabus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                chapter TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                updated_at TEXT DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'TODO',
                updated_at TEXT DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                skill TEXT NOT NULL,
                description TEXT NOT NULL,
                undo_type TEXT DEFAULT '',
                undo_payload TEXT DEFAULT ''
            )
        """)
        cur.execute("SELECT COUNT(*) FROM projects")
        if cur.fetchone()[0] == 0:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            cur.executemany(
                "INSERT INTO projects (project_name, task, status, updated_at) VALUES (?, ?, ?, ?)",
                [
                    ("ESP32 MP3 Recorder", "Finalize I2S mic (INMP441) + SD card SPI pinout schematic", "TODO", now_str),
                    ("ESP32 MP3 Recorder", "Write DMA ring-buffer WAV/MP3 recording firmware", "TODO", now_str),
                    ("JARVIS Core", "Complete Stage 1 CDP Browser & Stage 2 SQLite Tracker", "IN_PROGRESS", now_str),
                ],
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SQLite Init Warning] {e}")


def ensure_aux_files():
    try:
        if not os.path.exists(INSTRUCTIONS_FILE):
            with open(INSTRUCTIONS_FILE, "w", encoding="utf-8") as f:
                f.write(
                    "# JARVIS STANDING INSTRUCTIONS\n"
                    "- Execute user commands deterministically using skills.\n"
                    "- Always stay inside the user's current browser instead of switching browsers.\n"
                    "- If a requested site is already open in any tab or window, switch to it instead of opening duplicates.\n"
                )
        if not os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "preferred_browser": "brave",
                        "last_active_browser": "brave",
                        "last_active_site": "",
                        "facts": [],
                        "session_history": [],
                    },
                    f,
                    indent=2,
                )
        if not os.path.exists(SCHEDULES_FILE):
            with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)
        if not os.path.exists(WORKSPACE_FILE):
            with open(WORKSPACE_FILE, "w", encoding="utf-8") as f:
                f.write("")
        init_sqlite_db()
    except Exception as e:
        print(f"[Skills Init Warning] {e}")


def log_reversible_action(skill: str, description: str, undo_type: str = "", undo_payload: str = ""):
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO action_log (timestamp, skill, description, undo_type, undo_payload) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().strftime("%H:%M:%S"), skill, description, undo_type, undo_payload),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def undo_last_action() -> str:
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT id, skill, description, undo_type, undo_payload FROM action_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return "Jarvis says: No recent actions recorded in the undo trail, sir."
        log_id, skill, desc, u_type, u_payload = row
        cur.execute("DELETE FROM action_log WHERE id = ?", (log_id,))
        conn.commit()
        conn.close()

        if u_type == "reopen_tab":
            count = int(u_payload) if str(u_payload).isdigit() else 1
            for _ in range(count):
                _send_ctrl_shortcut("t", shift=True)
                time.sleep(0.18)
            return f"Jarvis says: Reversed '{desc}' by restoring {count} closed browser tab(s), sir."
        elif u_type == "browser_back":
            with kb.pressed(Key.alt):
                kb.press(Key.left)
                kb.release(Key.left)
            return f"Jarvis says: Reversed '{desc}' by navigating back."
        elif u_type == "relaunch_app" and u_payload:
            subprocess.Popen(f'start "" {u_payload}', shell=True)
            return f"Jarvis says: Reversed '{desc}' by relaunching {u_payload}."
        return f"Jarvis says: Logged action '{desc}' removed from trail."
    except Exception as e:
        return f"Jarvis says: Undo error: {e}"


def save_pending_confirmation(skill_payload: dict, prompt_msg: str) -> str:
    try:
        with open(CONFIRM_FILE, "w", encoding="utf-8") as f:
            json.dump({"payload": skill_payload, "timestamp": time.time(), "prompt": prompt_msg}, f, indent=2)
    except Exception:
        pass
    return f"Jarvis says: [CONFIRMATION GATE] {prompt_msg} (Say 'confirm' or 'cancel')."


def pop_pending_confirmation() -> dict | None:
    if not os.path.exists(CONFIRM_FILE):
        return None
    try:
        with open(CONFIRM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(CONFIRM_FILE)
        if time.time() - float(data.get("timestamp", 0)) < 180:
            return data.get("payload")
    except Exception:
        pass
    return None


def get_pushback_context() -> str:
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT exam_name, exam_date FROM exams WHERE status = 'UPCOMING' ORDER BY exam_date ASC LIMIT 1")
        exam_row = cur.fetchone()
        cur.execute("SELECT subject, chapter FROM syllabus WHERE status != 'COMPLETED' LIMIT 2")
        syl_rows = cur.fetchall()
        conn.close()

        reminders = []
        if exam_row:
            reminders.append(f"upcoming exam '{exam_row[0]}' on {exam_row[1]}")
        if syl_rows:
            pending_list = ", ".join([f"{r[0]} ({r[1]})" for r in syl_rows])
            reminders.append(f"pending syllabus: {pending_list}")
        return "; ".join(reminders)
    except Exception:
        return ""


def get_system_status_report() -> str:
    cpu_str = "Nominal"
    ram_str = "Unknown"
    bat_str = "AC Power"
    disk_str = "Unknown"

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.15)
        cpu_str = f"{cpu_pct:.0f}% load"
    except Exception:
        pass

    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        mem_stat = MEMORYSTATUSEX()
        mem_stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_stat))
        total_gb = mem_stat.ullTotalPhys / (1024 ** 3)
        used_gb = (mem_stat.ullTotalPhys - mem_stat.ullAvailPhys) / (1024 ** 3)
        ram_str = f"{mem_stat.dwMemoryLoad}% ({used_gb:.1f}/{total_gb:.1f} GB)"
    except Exception:
        pass

    try:
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]
        pwr = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
            pct = pwr.BatteryLifePercent
            charging = "Charging" if pwr.ACLineStatus == 1 else "Discharging"
            if pct <= 100:
                bat_str = f"{pct}% ({charging})"
    except Exception:
        pass

    try:
        import shutil
        total, used, free = shutil.disk_usage(BASE_DIR)
        disk_str = f"{free // (1024 ** 3)} GB free on drive"
    except Exception:
        pass

    cdp_online = "ONLINE (Port 9222)" if _cdp_get_tabs() else "STANDBY (Pixel+HitTest Mode)"
    return (
        f"Jarvis says: All systems operational, sir. Battery at {bat_str} | "
        f"CPU: {cpu_str} | Memory: {ram_str} | Storage: {disk_str} | Browser Bridge: {cdp_online}."
    )


def get_daily_briefing() -> str:
    now_str = datetime.now().strftime("%A, %d %B %Y (%I:%M %p)")
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT exam_name, exam_date, notes FROM exams WHERE status = 'UPCOMING' ORDER BY exam_date ASC LIMIT 3")
        exams = cur.fetchall()
        cur.execute("SELECT subject, COUNT(*) FROM syllabus WHERE status = 'COMPLETED' GROUP BY subject")
        done_syl = dict(cur.fetchall())
        cur.execute("SELECT subject, chapter FROM syllabus WHERE status != 'COMPLETED' LIMIT 4")
        pending_syl = cur.fetchall()
        cur.execute("SELECT project_name, task, status FROM projects WHERE status != 'DONE' LIMIT 4")
        proj_tasks = cur.fetchall()
        conn.close()
    except Exception:
        exams, done_syl, pending_syl, proj_tasks = [], {}, [], []

    schedules = []
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            schedules = [s for s in json.load(f) if not s.get("done")]
    except Exception:
        pass

    parts = [f"Good day, sir. Briefing for {now_str}:"]
    if exams:
        ex_str = "; ".join([f"{e[0]} on {e[1]}" + (f" ({e[2]})" if e[2] else "") for e in exams])
        parts.append(f"Upcoming Exams: {ex_str}.")
    else:
        parts.append("Exams: No datesheets logged yet.")

    if pending_syl:
        p_str = ", ".join([f"{s[0]}: {s[1]}" for s in pending_syl])
        parts.append(f"Active Syllabus Targets: {p_str} (Completed chapters: {done_syl or '0'}).")
    else:
        parts.append("Syllabus: All logged chapters complete or awaiting entries.")

    if proj_tasks:
        pr_str = "; ".join([f"[{p[0]}] {p[1]} ({p[2]})" for p in proj_tasks])
        parts.append(f"Workshop & Projects: {pr_str}.")

    if schedules:
        sc_str = ", ".join([f"{s['command']} at {s['time']}" for s in schedules[:3]])
        parts.append(f"Pending Timers: {sc_str}.")

    return "Jarvis says: " + " | ".join(parts)


def manage_sqlite_tracker(args: dict) -> str:
    category = str(args.get("category", "project")).strip().lower()
    action = str(args.get("action", "add")).strip().lower()
    title = str(args.get("title") or args.get("chapter") or args.get("task") or args.get("exam_name") or "").strip()
    subject_or_proj = str(args.get("subject") or args.get("project") or "General").strip()
    date_or_status = str(args.get("date") or args.get("status") or "").strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()

        if action in ["list", "query", "status", "show"]:
            conn.close()
            return get_daily_briefing()

        if "exam" in category:
            if action in ["complete", "done", "delete", "remove"]:
                cur.execute("UPDATE exams SET status = 'DONE' WHERE LOWER(exam_name) LIKE ?", (f"%{title.lower()}%",))
                conn.commit()
                conn.close()
                return f"Jarvis says: Marked exam '{title}' as completed, sir."
            cur.execute(
                "INSERT INTO exams (exam_name, exam_date, notes, status) VALUES (?, ?, ?, 'UPCOMING')",
                (title or subject_or_proj, date_or_status or "TBD", str(args.get("notes", ""))),
            )
            conn.commit()
            conn.close()
            return f"Jarvis says: Logged exam '{title or subject_or_proj}' ({date_or_status or 'TBD'}) into your datesheet."

        elif "syllabus" in category or "study" in category or "chapter" in category:
            status_val = "COMPLETED" if (action in ["complete", "done", "finish"] or "complete" in date_or_status.lower()) else "PENDING"
            cur.execute("SELECT id FROM syllabus WHERE LOWER(chapter) LIKE ?", (f"%{title.lower()}%",))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE syllabus SET status = ?, updated_at = ? WHERE id = ?", (status_val, now_str, existing[0]))
            else:
                cur.execute(
                    "INSERT INTO syllabus (subject, chapter, status, updated_at) VALUES (?, ?, ?, ?)",
                    (subject_or_proj, title, status_val, now_str),
                )
            conn.commit()
            conn.close()
            return f"Jarvis says: Updated syllabus tracker — {subject_or_proj}: '{title}' is now {status_val}."

        else:
            status_val = "DONE" if (action in ["complete", "done", "finish"] or "done" in date_or_status.lower()) else "TODO"
            cur.execute("SELECT id FROM projects WHERE LOWER(task) LIKE ?", (f"%{title.lower()}%",))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", (status_val, now_str, existing[0]))
            else:
                cur.execute(
                    "INSERT INTO projects (project_name, task, status, updated_at) VALUES (?, ?, ?, ?)",
                    (subject_or_proj, title, status_val, now_str),
                )
            conn.commit()
            conn.close()
            return f"Jarvis says: Workshop tracker updated for [{subject_or_proj}] — '{title}' ({status_val})."
    except Exception as e:
        return f"Jarvis says: Tracker database error: {e}"


# ==================== CDP + HIT-TEST TAB & PIXEL BUY-BOX ENGINE ====================

def _cdp_get_tabs() -> list[dict]:
    try:
        import requests
        res = requests.get(f"{CDP_BASE_URL}/json/list", timeout=0.30)
        if res.status_code == 200:
            return [t for t in res.json() if t.get("type") == "page"]
    except Exception:
        pass
    return []


def _cdp_activate_tab(tab_id: str) -> bool:
    try:
        import requests
        res = requests.get(f"{CDP_BASE_URL}/json/activate/{tab_id}", timeout=0.35)
        return res.status_code == 200
    except Exception:
        return False


def _cdp_close_tab(tab_id: str) -> bool:
    try:
        import requests
        res = requests.get(f"{CDP_BASE_URL}/json/close/{tab_id}", timeout=0.35)
        return res.status_code == 200
    except Exception:
        return False


def _cdp_eval_js(js_expression: str, active_window: str = "") -> str | None:
    tabs = _cdp_get_tabs()
    if not tabs:
        return None
    target_tab = tabs[0]
    al = (active_window or "").lower()
    for t in tabs:
        t_title = (t.get("title") or "").lower()
        if t_title and t_title[:15] in al:
            target_tab = t
            break

    ws_url = target_tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return None

    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=1.0)
        msg = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js_expression, "returnByValue": True},
        })
        ws.send(msg)
        raw_resp = ws.recv()
        ws.close()
        resp_data = json.loads(raw_resp)
        return str(resp_data.get("result", {}).get("result", {}).get("value", ""))
    except Exception:
        return None


def _cdp_smart_click(is_buy_now: bool, active_window: str) -> str | None:
    if not _cdp_get_tabs():
        return None
    js_code = """
    (function() {
        const selectors = [
            '#add-to-cart-button',
            'input[name="submit.add-to-cart"]',
            'button[name="submit.add-to-cart"]',
            '#buy-now-button',
            'button._2KpZ6l._2U9uOA._3v1-ww'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) { el.scrollIntoView({block: 'center'}); el.click(); return 'CDP_CLICKED:' + (el.value || el.innerText || sel); }
        }
        return '';
    })();
    """
    res = _cdp_eval_js(js_code, active_window)
    if res and res.startswith("CDP_CLICKED:"):
        return f"CDP DOM Engine directly clicked '{res.split(':', 1)[1]}'."
    return None


def _find_ecommerce_button_by_pixel_color(sw: int, sh: int, prefer_buy_now: bool = False) -> tuple[int, int, str] | None:
    """
    20ms Zero-DOM Pixel Scanner for Amazon / Flipkart 'Add to Cart' & 'Buy Now' buttons.
    Even when Google Chrome reports (0 DOM elements), Amazon's Add to Cart button is an unmistakable
    Golden-Yellow pill (#FFD814 / #F7CA00) located on the right half of the screen (x > 48% sw, y > 180).
    """
    try:
        import mss
        from PIL import Image

        left_x = int(sw * 0.48)
        top_y = int(sh * 0.17)
        crop_w = int(sw * 0.49)
        crop_h = int(sh * 0.78)

        with mss.MSS() as sct:
            sct_img = sct.grab({"left": left_x, "top": top_y, "width": crop_w, "height": crop_h})
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # Downsample by 4x for a 0.005s scan
        scale = 4
        small_w, small_h = max(1, img.width // scale), max(1, img.height // scale)
        small = img.resize((small_w, small_h))
        px = small.load()

        yellow_rows = {}
        orange_rows = {}

        for y in range(small_h):
            y_xs = []
            o_xs = []
            for x in range(small_w):
                r, g, b = px[x, y]
                # Amazon Add-to-Cart Golden Yellow (#FFD814 / #F7CA00 / #FFCE12)
                if r > 225 and 185 <= g <= 232 and b < 65 and (r - b) > 170:
                    y_xs.append(x)
                # Amazon Buy-Now / Flipkart Orange (#FFA41C / #FA8900 / #FB641B)
                elif r > 230 and 100 <= g <= 180 and b < 55 and (r - g) > 55:
                    o_xs.append(x)

            # A real button spans at least 28px horizontally (7 downsampled pixels)
            if len(y_xs) >= 7 and (max(y_xs) - min(y_xs)) < 95:
                yellow_rows[y] = (min(y_xs), max(y_xs))
            if len(o_xs) >= 7 and (max(o_xs) - min(o_xs)) < 95:
                orange_rows[y] = (min(o_xs), max(o_xs))

        target_rows = orange_rows if (prefer_buy_now and orange_rows) else (yellow_rows or orange_rows)
        label = "Buy Now (Orange Button)" if target_rows is orange_rows else "Add to Cart (Yellow Button)"

        if not target_rows:
            return None

        # Find contiguous vertical band of button rows (at least 2 rows = 8px tall)
        sorted_ys = sorted(target_rows.keys())
        best_cluster = []
        curr_cluster = [sorted_ys[0]]
        for y in sorted_ys[1:]:
            if y - curr_cluster[-1] <= 2:
                curr_cluster.append(y)
            else:
                if len(curr_cluster) > len(best_cluster):
                    best_cluster = curr_cluster
                curr_cluster = [y]
        if len(curr_cluster) > len(best_cluster):
            best_cluster = curr_cluster

        if len(best_cluster) < 2:
            return None

        mid_y = best_cluster[len(best_cluster) // 2]
        x1, x2 = target_rows[mid_y]
        mid_x = (x1 + x2) // 2

        screen_x = left_x + int(mid_x * scale)
        screen_y = top_y + int(mid_y * scale)
        return screen_x, screen_y, label
    except Exception:
        return None


def _get_chrome_tabs_via_hittest(sw: int, active_window: str) -> list[dict]:
    """
    Forces Google Chrome / Brave / Edge to reveal all open tabs in 0.03s by hit-testing
    the top tab strip at (x, 18) via ControlFromPoint, bypassing Chrome's 0-DOM-element block!
    """
    if not HAS_UIA:
        return []
    tabs = []
    seen_bounds = set()
    active_low = (active_window or "").lower().strip()
    # Strip browser suffix from OS window title so we can match the active tab title accurately
    clean_win_title = re.sub(r"\s*-\s*(?:google\s+chrome|brave|microsoft\s+edge|mozilla\s+firefox).*$", "", active_low, flags=re.I).strip()

    try:
        with auto.UIAutomationInitializerInThread():
            # Probe points along the top tab bar (y=18) to wake up Chrome's TabStrip container
            tab_strip_container = None
            for probe_x in [120, 260, 420, 600]:
                if probe_x >= sw - 160:
                    break
                ctrl = auto.ControlFromPoint(probe_x, 18)
                if not ctrl:
                    continue
                curr = ctrl
                for _ in range(5):
                    if not curr:
                        break
                    ct_name = curr.ControlTypeName
                    if ct_name == "TabControl":
                        tab_strip_container = curr
                        break
                    if ct_name == "TabItemControl":
                        parent = curr.GetParentControl()
                        if parent:
                            tab_strip_container = parent
                            break
                    curr = curr.GetParentControl()
                if tab_strip_container:
                    break

            raw_tab_ctrls = []
            if tab_strip_container:
                for child in tab_strip_container.GetChildren():
                    if child.ControlTypeName == "TabItemControl":
                        raw_tab_ctrls.append(child)
                    else:
                        for sub in child.GetChildren():
                            if sub.ControlTypeName == "TabItemControl":
                                raw_tab_ctrls.append(sub)
            else:
                # Direct sweep across the tab bar if container wasn't wrapped in TabControl
                for px in range(70, sw - 180, 85):
                    c = auto.ControlFromPoint(px, 18)
                    while c and c.ControlTypeName != "TabItemControl":
                        c = c.GetParentControl()
                    if c and c.ControlTypeName == "TabItemControl":
                        raw_tab_ctrls.append(c)

            idx = 1
            for tc in raw_tab_ctrls:
                try:
                    r = tc.BoundingRectangle
                    if r.width() < 20 or r.top > 60:
                        continue
                    b_key = (int(r.left) // 12, int(r.right) // 12)
                    if b_key in seen_bounds:
                        continue
                    seen_bounds.add(b_key)

                    t_name = (tc.Name or "").strip()
                    t_low = t_name.lower()
                    cx = int((r.left + r.right) / 2)
                    cy = int((r.top + r.bottom) / 2)

                    is_sel = False
                    try:
                        sel_pat = tc.GetSelectionItemPattern()
                        if sel_pat and sel_pat.IsSelected:
                            is_sel = True
                    except Exception:
                        pass
                    if not is_sel and clean_win_title and t_low:
                        if clean_win_title[:16] in t_low or t_low[:16] in clean_win_title:
                            is_sel = True

                    tabs.append({
                        "index": idx,
                        "id": "TabItem",
                        "text": t_name,
                        "cx": cx,
                        "cy": cy,
                        "bounds": [int(r.left), int(r.top), int(r.right), int(r.bottom)],
                        "is_active": is_sel,
                    })
                    idx += 1
                except Exception:
                    continue
    except Exception:
        pass
    return tabs


def load_instructions() -> str:
    ensure_aux_files()
    try:
        with open(INSTRUCTIONS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def load_memory() -> dict:
    ensure_aux_files()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "preferred_browser" not in data or data["preferred_browser"] == "default":
                data["preferred_browser"] = "brave"
            if "last_active_browser" not in data:
                data["last_active_browser"] = data.get("preferred_browser", "brave")
            if "session_history" not in data:
                data["session_history"] = []
            return data
    except Exception:
        return {
            "preferred_browser": "brave",
            "last_active_browser": "brave",
            "last_active_site": "",
            "facts": [],
            "session_history": [],
        }


def update_memory_state(last_browser: str = "", last_site: str = "", turn_entry: str = "") -> None:
    ensure_aux_files()
    try:
        mem = load_memory()
        if last_browser and last_browser in BROWSER_EXE_MAP:
            mem["last_active_browser"] = BROWSER_EXE_MAP[last_browser]
        if last_site:
            mem["last_active_site"] = last_site
        if turn_entry:
            hist = mem.get("session_history", [])
            hist.append(turn_entry)
            mem["session_history"] = hist[-12:]
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2)
    except Exception:
        pass


def save_memory_fact(fact: str, key: str = "") -> str:
    ensure_aux_files()
    try:
        fact_clean = str(fact or "").strip()
        if not fact_clean:
            return "Empty memory fact ignored."
        mem = load_memory()
        if key:
            mem[key] = fact_clean
        else:
            facts = mem.get("facts", [])
            if fact_clean not in facts:
                facts.append(fact_clean)
            mem["facts"] = facts[-50:]
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2)
        print(f"[Memory Updated] Saved to {MEMORY_FILE}: {fact_clean}")
        return f"Saved to memory: {fact_clean}"
    except Exception as e:
        return f"Memory error: {e}"


def load_workspace() -> str:
    ensure_aux_files()
    try:
        with open(WORKSPACE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else "EMPTY"
    except Exception:
        return "EMPTY"


def append_workspace(source_title: str, text: str) -> None:
    ensure_aux_files()
    clean_text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not clean_text:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    header = f"\n\n=== [WORKSPACE SOURCE: {source_title} | {timestamp}] ===\n"
    with open(WORKSPACE_FILE, "a", encoding="utf-8") as f:
        f.write(header + clean_text[:6000])
    print(f"[Workspace Updated] Pinned {min(len(clean_text), 6000)} chars from '{source_title}'")


def clear_workspace_file() -> None:
    with open(WORKSPACE_FILE, "w", encoding="utf-8") as f:
        f.write("")
    print("[Workspace Cleared] workspace.md wiped clean.")


def add_schedule(when: str, command: str) -> str:
    ensure_aux_files()
    try:
        when_clean = (
            when.strip()
            .lower()
            .replace("minutes", "m")
            .replace("minute", "m")
            .replace("mins", "m")
            .replace("min", "m")
            .replace(" ", "")
        )
        if when_clean.endswith("m"):
            num_str = when_clean.lstrip("+")[:-1]
            mins = max(1, int(num_str))
            target_time = (datetime.now() + timedelta(minutes=mins)).strftime("%H:%M")
        else:
            target_time = when_clean

        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            schedules = json.load(f)

        schedules.append({
            "time": target_time,
            "command": command.strip(),
            "done": False,
        })
        with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
            json.dump(schedules, f, indent=2)
        print(f"[Schedule Updated] Added '{command}' at {target_time} in {SCHEDULES_FILE}")
        return f"Scheduled '{command}' for {target_time}."
    except Exception as e:
        return f"Schedule error: {e}"


def pop_due_schedules() -> list[str]:
    ensure_aux_files()
    due_commands = []
    try:
        now_hhmm = datetime.now().strftime("%H:%M")
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            schedules = json.load(f)

        changed = False
        for item in schedules:
            if not item.get("done") and item.get("time") <= now_hhmm:
                item["done"] = True
                due_commands.append(item.get("command", ""))
                changed = True

        if changed:
            with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
                json.dump(schedules, f, indent=2)
    except Exception:
        pass
    return [c for c in due_commands if c]


def get_screen_metrics() -> tuple[int, int]:
    try:
        sw = int(ctypes.windll.user32.GetSystemMetrics(0))
        sh = int(ctypes.windll.user32.GetSystemMetrics(1))
        return max(200, sw), max(200, sh)
    except Exception:
        return 1920, 1080


def detect_browser_from_title(window_title: str) -> str:
    wl = (window_title or "").lower()
    if "brave" in wl:
        return "brave"
    if "edge" in wl:
        return "msedge"
    if "chrome" in wl:
        return "chrome"
    if "firefox" in wl or "mozilla" in wl:
        return "firefox"
    return ""


def find_os_window_by_keyword(keyword: str) -> tuple[int, str] | None:
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    matches = []
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _enum_cb(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.strip()
                    tl = title.lower()
                    if kw in tl and not any(ig in tl for ig in ["jarvis — live session", "laptop_client.py" , "jarvis hud"]):
                        matches.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        cb = WNDENUMPROC(_enum_cb)
        user32.EnumWindows(cb, 0)
    except Exception:
        pass

    if matches:
        for hwnd, title in matches:
            if "brave" in title.lower():
                return hwnd, title
        return matches[0]
    return None


def focus_os_window(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        curr_fg = user32.GetForegroundWindow()
        if curr_fg == hwnd:
            return True
        # Use AttachThreadInput instead of tapping Alt so Chrome's 3-dot menu bar is NEVER triggered!
        fg_thread = user32.GetWindowThreadProcessId(curr_fg, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        if fg_thread and target_thread and fg_thread != target_thread:
            user32.AttachThreadInput(fg_thread, target_thread, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(fg_thread, target_thread, False)
        else:
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.20)
        return True
    except Exception:
        return False


def _smooth_move(target_x: int, target_y: int, duration: float = 0.14):
    try:
        start_x, start_y = mouse.position
        dist = math.hypot(target_x - start_x, target_y - start_y)
        if dist < 8:
            mouse.position = (target_x, target_y)
            return
        steps = max(6, min(14, int(duration / 0.012)))
        sleep_step = duration / steps
        for i in range(1, steps + 1):
            t = i / steps
            ease = 1.0 - ((1.0 - t) ** 3)
            nx = int(start_x + (target_x - start_x) * ease)
            ny = int(start_y + (target_y - start_y) * ease)
            mouse.position = (nx, ny)
            time.sleep(sleep_step)
        mouse.position = (target_x, target_y)
    except Exception:
        mouse.position = (target_x, target_y)


def _human_click(target_x: int, target_y: int, settle: float = 0.45, button=Button.left):
    _smooth_move(target_x, target_y, duration=0.14)
    time.sleep(0.05)
    mouse.click(button, 1)
    if settle > 0:
        time.sleep(settle)


def _precision_trackpad_scroll(direction: str, amount: int, sw: int, sh: int):
    cx, cy = int(sw * 0.52), int(sh * 0.55)
    _smooth_move(cx, cy, duration=0.07)
    time.sleep(0.02)

    sign = 1 if direction == "up" else -1
    total_wheel_units = max(80, min(1200, int(amount * 90)))
    frames = 22
    accumulated = 0.0

    for i in range(1, frames + 1):
        weight = math.sin(math.pi * (i / frames))
        step_val = (total_wheel_units / 14.0) * weight
        accumulated += step_val
        int_delta = int(round(accumulated))
        if int_delta != 0:
            accumulated -= int_delta
            try:
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, sign * int_delta, 0)
            except Exception:
                pass
        time.sleep(0.013)


def compute_spatial_zone(bounds: list[int], screen_w: int = 1920, screen_h: int = 1080) -> tuple[str, int, int]:
    if not bounds or len(bounds) < 4:
        return "CENTER-MID", screen_w // 2, screen_h // 2
    x1, y1, x2, y2 = bounds[:4]
    cx = int((x1 + x2) / 2)
    cy = int((y1 + y2) / 2)

    if cx < screen_w * 0.36:
        h_tag = "LEFT"
    elif cx > screen_w * 0.64:
        h_tag = "RIGHT"
    else:
        h_tag = "CENTER"

    if cy < screen_h * 0.28:
        v_tag = "TOP"
    elif cy > screen_h * 0.72:
        v_tag = "BOTTOM"
    else:
        v_tag = "MID"

    return f"{h_tag}-{v_tag}", cx, cy


def clean_click_target_and_position(raw_target: str, explicit_pos: str = "") -> tuple[str, str, int | None, str]:
    t = (raw_target or "").strip()
    pos = (explicit_pos or "").strip().lower()

    idx_match = re.search(r"(?:#|item\s+|element\s+|number\s+)(\d+)\b", t, flags=re.I)
    if not idx_match and t.isdigit():
        idx_match = re.match(r"^(\d+)$", t)
    extracted_idx = int(idx_match.group(1)) if idx_match else None

    t = re.sub(r"\bl+e+f+t+[-\s]*(?:m+o+s+t+)?\b", "left", t, flags=re.I)
    t = re.sub(r"\br+i+g+h+t+[-\s]*(?:m+o+s+t+)?\b", "right", t, flags=re.I)
    t = re.sub(r"\bt+o+p+[-\s]*(?:m+o+s+t+)?\b", "top", t, flags=re.I)
    t = re.sub(r"\bb+o+t+t+o+m+[-\s]*(?:m+o+s+t+)?\b", "bottom", t, flags=re.I)
    t = re.sub(r"\b(?:m+i+d+d+l+e+|c+e+n+t+e+r+)\b", "center", t, flags=re.I)

    spatial_pat = r"\b(?:on\s+the\s+|at\s+the\s+|in\s+the\s+|to\s+the\s+)?(top[-\s]?left|top[-\s]?right|bottom[-\s]?left|bottom[-\s]?right|left|right|top|bottom|center)\b(?:\s+side|\s+one|\s+corner)?"
    m = re.search(spatial_pat, t, flags=re.I)
    if m:
        found_pos = m.group(1).lower().replace(" ", "-")
        if not pos:
            pos = found_pos
        t = (t[:m.start()] + " " + t[m.end():]).strip()

    extracted_color = ""
    color_pat = r"\b(" + "|".join(COLOR_RGB_ANCHORS.keys()) + r")\b"
    mc = re.search(color_pat, t, flags=re.I)
    if mc:
        extracted_color = mc.group(1).lower()
        t = (t[:mc.start()] + " " + t[mc.end():]).strip()

    t = re.sub(r"\b(?:to\s+enlarge(?:\s+it)?|to\s+open(?:\s+it)?|to\s+view(?:\s+it)?|please|jarvis)\b", "", t, flags=re.I)
    t = re.sub(r"^(?:that|this|the|a|an|on)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:that|this|one|button|link|item|side|video|thumbnail|picture|photo|image)$", "", t, flags=re.I)
    t = re.sub(r"(?:#|item\s+|element\s+|number\s+)\d+\b", "", t, flags=re.I)
    t = " ".join(t.split()).strip()

    return t, pos, extracted_idx, extracted_color


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _get_clipboard_text() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    text = ""
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if handle:
                    ptr = kernel32.GlobalLock(handle)
                    if ptr:
                        try:
                            text = ctypes.wstring_at(ptr)
                        finally:
                            kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
            break
        time.sleep(0.04)
    return (text or "").strip()


def _set_clipboard_text(text: str) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        ctypes.memmove(ptr, data, len(data))
                        kernel32.GlobalUnlock(h_mem)
                        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                        return True
            finally:
                user32.CloseClipboard()
            break
        time.sleep(0.03)
    return False


def _clear_clipboard() -> None:
    user32 = ctypes.windll.user32
    for _ in range(5):
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()
            break
        time.sleep(0.04)


def _paste_text(text: str, press_enter: bool = True, clear_first: bool = True):
    time.sleep(0.08)
    if clear_first:
        _send_ctrl_shortcut("a")
        time.sleep(0.04)
        kb.press(Key.backspace)
        kb.release(Key.backspace)
        time.sleep(0.04)

    if _set_clipboard_text(text):
        _send_ctrl_shortcut("v")
    else:
        kb.type(text)

    if press_enter:
        time.sleep(0.16)
        kb.press(Key.enter)
        kb.release(Key.enter)
        time.sleep(0.6)


def _capture_active_screen_text() -> str:
    _clear_clipboard()
    time.sleep(0.2)

    sw, sh = get_screen_metrics()
    target_x, target_y = int(sw * 0.35), int(sh * 0.45)
    _human_click(target_x, target_y, settle=0.2)

    _send_ctrl_shortcut("a")
    time.sleep(0.25)
    _send_ctrl_shortcut("c")
    time.sleep(0.4)

    mouse.click(Button.left, 1)

    copied = _get_clipboard_text()
    if not copied:
        copied = _extract_text_via_uia()

    lines = [" ".join(line.split()) for line in copied.splitlines() if line.strip()]
    return "\n".join(lines)


def _extract_text_via_uia() -> str:
    if not HAS_UIA:
        return ""
    collected = []
    t_start = time.perf_counter()
    try:
        with auto.UIAutomationInitializerInThread():
            fg = auto.GetForegroundControl()
            if not fg:
                return ""
            queue = [(fg, 0)]
            while queue and len(" ".join(collected)) < 3500:
                if (time.perf_counter() - t_start) > 1.2:
                    break
                ctrl, depth = queue.pop(0)
                if depth > 12:
                    continue
                try:
                    c_type = ctrl.ControlTypeName
                    if c_type in {"TextControl", "DocumentControl", "EditControl"}:
                        txt = (ctrl.Name or "").strip()
                        if len(txt) > 20 and txt not in collected:
                            collected.append(txt)
                    for child in ctrl.GetChildren()[:25]:
                        queue.append((child, depth + 1))
                except Exception:
                    continue
    except Exception:
        pass
    return " ".join(" ".join(collected).split())


def _find_open_browser_tab(keyword: str, indexed_elements: list[dict], sw: int = 1920, active_window: str = "") -> dict | None:
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    kw = re.sub(r"^https?://(?:www\.)?", "", kw)
    kw = kw.split(".")[0].split("/")[0].strip()
    if len(kw) < 2:
        return None

    for e in indexed_elements:
        if e["id"].lower() == "tabitem" or e["cy"] < 55:
            if kw in e["text"].lower() and "new tab" not in e["text"].lower():
                return e

    # Fallback: Hit-test Chrome's top tab bar directly if DOM had 0 elements
    for t in _get_chrome_tabs_via_hittest(sw, active_window):
        if kw in t["text"].lower() and "new tab" not in t["text"].lower():
            return t
    return None


def _resolve_sticky_browser(explicit_browser: str, active_window: str) -> str:
    b_clean = (explicit_browser or "").strip().lower()
    if b_clean in BROWSER_EXE_MAP:
        exe = BROWSER_EXE_MAP[b_clean]
        update_memory_state(last_browser=exe)
        return exe

    active_b = detect_browser_from_title(active_window)
    if active_b:
        update_memory_state(last_browser=active_b)
        return active_b

    mem = load_memory()
    saved_b = str(mem.get("last_active_browser") or mem.get("preferred_browser") or "brave").strip().lower()
    return BROWSER_EXE_MAP.get(saved_b, "brave")


def _open_url(url: str, browser: str = "", active_window: str = "", use_address_bar: bool = False):
    url = (url or "").strip(" .?!,;:")
    # Always normalize US amazon.com to amazon.in
    url = re.sub(r"https?://(?:www\.)?amazon\.com\b", "https://www.amazon.in", url, flags=re.I)
    target_exe = _resolve_sticky_browser(browser, active_window)

    if use_address_bar and detect_browser_from_title(active_window):
        _send_ctrl_shortcut("l")
        time.sleep(0.18)
        _paste_text(url, press_enter=True, clear_first=True)
        return

    cdp_flag = f"--remote-debugging-port={CDP_PORT} --force-renderer-accessibility" if target_exe in ["brave", "msedge", "chrome"] else ""
    subprocess.Popen(f'start "" {target_exe} {cdp_flag} "{url}"', shell=True)
    time.sleep(0.9)


def _score_element_color(bounds: list[int], target_color: str, sw: int, sh: int) -> float:
    if not target_color or target_color not in COLOR_RGB_ANCHORS:
        return 0.0
    try:
        import mss
        from PIL import Image
        x1, y1, x2, y2 = bounds[:4]
        crop_box = {
            "left": max(0, x1 - 20),
            "top": max(185, y1 - 220),
            "width": max(20, min(sw - x1, (x2 - x1) + 40)),
            "height": max(20, min(sh - max(185, y1 - 220), (y2 - y1) + 220)),
        }
        with mss.MSS() as sct:
            sct_img = sct.grab(crop_box)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX").resize((16, 16))
            pixels = list(img.getdata())

        tr, tg, tb = COLOR_RGB_ANCHORS[target_color]
        best_dist = 999.0
        for r, g, b in pixels:
            if target_color != "white" and r > 238 and g > 238 and b > 238:
                continue
            dist = ((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
        return -best_dist
    except Exception:
        return 0.0


def _quick_find_dom_button(keywords: list[str], sw: int, sh: int) -> tuple[int, int, str] | None:
    if not HAS_UIA:
        return None
    t_start = time.perf_counter()
    try:
        with auto.UIAutomationInitializerInThread():
            fg = auto.GetForegroundControl()
            if not fg:
                return None
            queue = [(fg, 0)]
            while queue:
                if (time.perf_counter() - t_start) > 0.65:
                    break
                ctrl, depth = queue.pop(0)
                if depth > 16:
                    continue
                try:
                    c_type = ctrl.ControlTypeName
                    c_name = " ".join((ctrl.Name or "").strip().split())
                    c_low = c_name.lower()

                    if c_type in {"ButtonControl", "HyperlinkControl", "GroupControl"} and c_low and not ctrl.IsOffscreen:
                        if "items in shopping cart" not in c_low and "cart icon" not in c_low:
                            if any(k in c_low for k in keywords):
                                r = ctrl.BoundingRectangle
                                if r.width() > 15 and r.height() > 15 and r.top > 160:
                                    cx = int((r.left + r.right) / 2)
                                    cy = int((r.top + r.bottom) / 2)
                                    if 0 < cx < sw and 160 < cy < sh:
                                        return cx, cy, c_name
                    if depth < 16:
                        for child in ctrl.GetChildren()[:35]:
                            queue.append((child, depth + 1))
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _hunt_add_to_cart(sw: int, sh: int, prefer_buy_now: bool = False, active_window: str = "") -> tuple[int, int, str] | None:
    """
    3-Layer E-Commerce Button Hunter:
    1) Checks CDP DOM if port 9222 is active.
    2) Scans screen pixels in 20ms for Amazon's Golden-Yellow (#FFD814) 'Add to Cart' or Orange 'Buy Now' button!
    3) Scrolls down up to 4 times if the button is below the fold.
    """
    for _ in range(4):
        # 1. Pixel-Cluster Scanner (Works even when Chrome reports 0 DOM elements!)
        px_match = _find_ecommerce_button_by_pixel_color(sw, sh, prefer_buy_now=prefer_buy_now)
        if px_match:
            return px_match

        # 2. UIA DOM Scanner
        dom_match = _quick_find_dom_button(["add to cart", "add to bag", "buy now", "buy it now"], sw, sh)
        if dom_match:
            return dom_match

        # 3. Scroll down 1 viewport row and let page settle
        _precision_trackpad_scroll("down", 4, sw, sh)
        time.sleep(0.35)
    return None


def _normalize_skill_aliases(skill: str, args: dict) -> tuple[str, dict]:
    s = skill.strip().lower().replace("-", "_").replace(" ", "_")
    a = dict(args)

    if s in ["add_to_cart", "add_to_bag", "buy_now"]:
        return "add_to_cart", a
    if s in ["status_report", "system_health", "hardware_status", "battery_status"]:
        return "system_status", a
    if s in ["morning_briefing", "briefing", "get_briefing"]:
        return "daily_briefing", a
    if s in ["undo", "undo_that", "reverse_action"]:
        return "undo_action", a
    if s in ["add_exam", "add_syllabus", "add_project", "update_tracker", "query_tracker", "tracker"]:
        return "manage_tracker", a
    if s in ["close_other_tabs", "clear_other_tabs", "delete_other_tabs", "close_duplicate_tabs", "keep_only_current_tab"]:
        a["action"] = "close_other_tabs"
        return "browser_nav", a
    if s in ["close_tab", "delete_tab", "clear_tab", "close_current_tab", "close_browser_tab"]:
        a["action"] = "close_tab"
        return "browser_nav", a
    if s in ["switch_tab", "next_tab", "change_tab", "select_tab", "focus_tab"]:
        a["action"] = "switch_tab"
        return "browser_nav", a
    if s in ["prev_tab", "previous_tab"]:
        a["action"] = "prev_tab"
        return "browser_nav", a
    if s in ["new_tab", "open_tab"]:
        a["action"] = "new_tab"
        return "browser_nav", a
    if s in ["go_back", "back", "navigate_back"]:
        a["action"] = "back"
        return "browser_nav", a
    if s in ["go_forward", "forward", "navigate_forward"]:
        a["action"] = "forward"
        return "browser_nav", a
    if s in ["refresh", "reload", "refresh_page", "reload_page"]:
        a["action"] = "refresh"
        return "browser_nav", a
    if s in ["scroll_down", "scrool_down", "scroll"]:
        a.setdefault("direction", "down")
        return "scroll_page", a
    if s in ["scroll_up", "scrool_up"]:
        a["direction"] = "up"
        return "scroll_page", a
    if s in ["click", "tap", "press", "select", "choose"]:
        return "click_element", a
    if s in ["type", "input", "enter_text", "fill"]:
        if a.get("target") or a.get("x"):
            return "fill_input", a
        return "type_text", a
    return s, a


def execute_skill(skill_payload: dict, active_window: str, ui_elements: list[dict]) -> str:
    try:
        raw_skill = str(skill_payload.get("skill", "")).strip().lower()
        raw_args = skill_payload.get("args") or {}
        if not isinstance(raw_args, dict):
            raw_args = {}

        skill, args = _normalize_skill_aliases(raw_skill, raw_args)
        for k_clean in ["app_name", "query", "text", "target", "tab", "url"]:
            if k_clean in args and isinstance(args[k_clean], str):
                args[k_clean] = args[k_clean].strip(" .?!,;:")

        if skill == "confirm_action":
            pending = pop_pending_confirmation()
            if not pending:
                return "Jarvis says: There is no pending action awaiting confirmation, sir."
            pending["args"]["_confirmed"] = True
            return execute_skill(pending, active_window, ui_elements)

        if skill == "cancel_action":
            pop_pending_confirmation()
            return "Jarvis says: Pending action aborted, sir."

        if skill == "system_status":
            return get_system_status_report()

        if skill == "daily_briefing":
            return get_daily_briefing()

        if skill == "manage_tracker":
            return manage_sqlite_tracker(args)

        if skill == "undo_action":
            return undo_last_action()

        sw, sh = get_screen_metrics()
        active_lower = (active_window or "").lower()
        in_browser = any(b in active_lower for b in [
            "brave", "edge", "chrome", "firefox", "mozilla",
            "youtube", "amazon", "flipkart", "myntra", "wikipedia", "google"
        ])

        indexed_elements = []
        idx_counter = 1
        for el in (ui_elements or []):
            if el.get("id") == "ACTIVE_WINDOW":
                continue
            bounds = el.get("bounds") or [0, 0, sw, sh]
            zone, cx, cy = compute_spatial_zone(bounds, sw, sh)
            indexed_elements.append({
                "index": idx_counter,
                "id": str(el.get("id", "")),
                "text": str(el.get("text", "")).strip(),
                "bounds": bounds,
                "zone": zone,
                "cx": cx,
                "cy": cy,
                "is_active": bool(el.get("is_active", False)),
            })
            idx_counter += 1

        # Dedicated 0.0s Add-to-Cart / Buy-Now Skill
        if skill == "add_to_cart":
            prefer_buy = bool(args.get("buy_now", False))
            cdp_res = _cdp_smart_click(prefer_buy, active_window)
            if cdp_res:
                return cdp_res
            btn = _hunt_add_to_cart(sw, sh, prefer_buy_now=prefer_buy, active_window=active_window)
            if btn:
                bx, by, bname = btn
                _human_click(bx, by, settle=0.6)
                log_reversible_action("add_to_cart", f"Clicked {bname}", "browser_back", "")
                return f"Locked onto '{bname}' at ({bx}, {by}) and clicked it."
            return "Could not locate the Add to Cart button on this page."

        if skill == "youtube_search":
            query = str(args.get("query", "")).strip()
            browser = str(args.get("browser", "")).strip().lower()
            encoded = urllib.parse.quote_plus(query)
            url = f"https://www.youtube.com/results?search_query={encoded}"

            for ct in _cdp_get_tabs():
                if "youtube.com" in (ct.get("url") or "").lower():
                    _cdp_activate_tab(ct["id"])
                    active_lower = "youtube"
                    in_browser = True
                    break

            if in_browser and "youtube" not in active_lower:
                yt_tab = _find_open_browser_tab("youtube", indexed_elements, sw, active_window)
                if yt_tab:
                    _human_click(yt_tab["cx"], yt_tab["cy"], settle=0.3)
                    active_lower = "youtube"

            if "youtube" not in active_lower and not browser:
                win_match = find_os_window_by_keyword("youtube")
                if win_match:
                    focus_os_window(win_match[0])
                    active_lower = win_match[1].lower()
                    in_browser = True

            use_bar = in_browser and ("youtube" in active_lower) and not browser
            _open_url(url, browser=browser, active_window=active_window, use_address_bar=use_bar)
            update_memory_state(last_site="youtube")
            return f"Searched YouTube for '{query}'."

        elif skill == "open_website":
            url = str(args.get("url", "https://www.google.com")).strip()
            if not url.startswith("http"):
                url = "https://" + url
            browser = str(args.get("browser", "")).strip().lower()
            site_kw = re.sub(r"^https?://(?:www\.)?", "", url).split(".")[0].split("/")[0].lower()

            if not browser:
                for ct in _cdp_get_tabs():
                    if site_kw and (site_kw in (ct.get("url") or "").lower() or site_kw in (ct.get("title") or "").lower()):
                        if _cdp_activate_tab(ct["id"]):
                            update_memory_state(last_site=site_kw)
                            return f"CDP switched directly to open tab '{ct.get('title')}'."

            if in_browser and not browser:
                existing_tab = _find_open_browser_tab(site_kw, indexed_elements, sw, active_window)
                if existing_tab:
                    _human_click(existing_tab["cx"], existing_tab["cy"], settle=0.35)
                    update_memory_state(last_site=site_kw)
                    return f"Switched directly to your open '{existing_tab['text']}' tab."

            if not browser and site_kw:
                win_match = find_os_window_by_keyword(site_kw)
                if win_match:
                    focus_os_window(win_match[0])
                    update_memory_state(last_site=site_kw)
                    return f"Switched back to your open '{win_match[1]}' window."

            _open_url(url, browser=browser, active_window=active_window, use_address_bar=False)
            update_memory_state(last_site=site_kw)
            return f"Opened {url} in {_resolve_sticky_browser(browser, active_window)}."

        elif skill == "launch_app":
            raw_app = str(args.get("app_name", "")).strip(" .?!,;:").lower()
            # Strip any leftover conversational filler ("brave for me instead" -> "brave")
            raw_app = re.sub(
                r"\s+(?:for\s+me|for\s+men|instead|please|right\s+now|now|sir|browser|app|application).*$",
                "",
                raw_app,
                flags=re.I,
            ).strip()

            # Match known canonical app names even if extra words were attached
            app = raw_app
            for known_key in FAST_APP_LAUNCH_MAP:
                if re.search(rf"\b{re.escape(known_key)}\b", raw_app):
                    app = known_key
                    break

            if app in ["brave", "brave browser", "edge", "microsoft edge", "chrome", "google chrome", "firefox", "notepad", "code", "vscode"]:
                win_kw = app.split()[0]
                win_match = find_os_window_by_keyword(win_kw)
                if win_match:
                    focus_os_window(win_match[0])
                    if win_kw in BROWSER_EXE_MAP:
                        update_memory_state(last_browser=BROWSER_EXE_MAP[win_kw])
                    return f"Switched to your open '{win_match[1]}' window."

            if app in FAST_APP_LAUNCH_MAP:
                exe_cmd = FAST_APP_LAUNCH_MAP[app]
                subprocess.Popen(f'start "" {exe_cmd}', shell=True)
                if app in BROWSER_EXE_MAP:
                    update_memory_state(last_browser=BROWSER_EXE_MAP[app])
                time.sleep(0.4)
                return f"Instantly launched '{app}'."

            # SAFETY GUARD: Never type multi-word sentences into Windows Start Menu (which opens Edge Bing search!)
            if len(app.split()) > 2 or any(w in app for w in ["for", "instead", "please", "open", "search"]):
                return f"Jarvis says: I wasn't sure which application '{raw_app}' refers to, sir."

            kb.press(Key.cmd)
            kb.release(Key.cmd)
            time.sleep(0.25)
            _paste_text(app, press_enter=True, clear_first=False)
            return f"Launched application '{app}'."

        elif skill == "close_app":
            app = str(args.get("app_name", "")).strip().lower()
            exe_list = FAST_APP_CLOSE_MAP.get(app, [f"{app}.exe"])
            for exe_name in exe_list:
                subprocess.run(f'taskkill /IM "{exe_name}" /F >nul 2>&1', shell=True)
            log_reversible_action("close_app", f"Closed {app}", "relaunch_app", FAST_APP_LAUNCH_MAP.get(app, app))
            return f"Instantly closed '{app}'."

        elif skill == "fill_input":
            target = str(args.get("target", "search")).strip().lower()
            text = str(args.get("text") or args.get("query") or "").strip()
            is_form_field = any(f in target for f in ["name", "email", "phone", "mobile", "password", "otp", "address", "pin", "city"])
            default_enter = False if is_form_field else True
            press_enter = bool(args.get("press_enter", default_enter))
            vision_x = args.get("x") if args.get("_vision_verified") else None
            vision_y = args.get("y") if args.get("_vision_verified") else None

            if not text:
                return "fill_input error: No text provided to type."

            if vision_x is not None and vision_y is not None:
                vx, vy = int(vision_x), int(vision_y)
                if 10 < vx < sw and 70 < vy < sh:
                    _human_click(vx, vy, settle=0.10)
                    _paste_text(text, press_enter=press_enter, clear_first=True)
                    return f"Glided to input '{target}' at ({vx}, {vy}) and entered '{text}'."

            chosen = None
            edit_candidates = [
                e for e in indexed_elements
                if e["id"].lower() in ["edit", "combobox"]
                and "address and search bar" not in e["text"].lower()
                and e["cy"] > 80
            ]
            target_tokens = [tok for tok in re.split(r"\W+", target) if len(tok) >= 2]
            for e in edit_candidates:
                el_low = e["text"].lower()
                if (target and target in el_low) or any(tok in el_low for tok in target_tokens):
                    chosen = e
                    break
            if not chosen and edit_candidates and not is_form_field:
                chosen = edit_candidates[0]

            if chosen:
                _human_click(chosen["cx"], chosen["cy"], settle=0.10)
                _paste_text(text, press_enter=press_enter, clear_first=True)
                return f"Entered '{text}' into #{chosen['index']} '{chosen['text']}' at ({chosen['cx']}, {chosen['cy']})."

            mem_site = str(load_memory().get("last_active_site", "")).lower()
            if not is_form_field:
                if "amazon" in active_lower or mem_site == "amazon":
                    url = f"https://www.amazon.in/s?k={urllib.parse.quote_plus(text)}"
                    _open_url(url, active_window=active_window, use_address_bar=True)
                    return f"Searched Amazon in current tab for '{text}'."
                elif "youtube" in active_lower or mem_site == "youtube":
                    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(text)}"
                    _open_url(url, active_window=active_window, use_address_bar=True)
                    return f"Searched YouTube in current tab for '{text}'."
                elif "flipkart" in active_lower or mem_site == "flipkart":
                    url = f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(text)}"
                    _open_url(url, active_window=active_window, use_address_bar=True)
                    return f"Searched Flipkart in current tab for '{text}'."

            return f"Could not find input box '{target}' on screen."

        elif skill == "scroll_page":
            direction = str(args.get("direction", "down")).strip().lower()
            amount = max(1, min(12, int(args.get("amount", 4))))

            if direction in ["top", "home"]:
                kb.press(Key.home)
                kb.release(Key.home)
                return "Glided to the top of the page."
            elif direction in ["bottom", "end"]:
                kb.press(Key.end)
                kb.release(Key.end)
                return "Glided to the bottom of the page."

            _precision_trackpad_scroll(direction, amount, sw, sh)
            return f"Smoothly glided {direction} (trackpad precision, amount={amount})."

        elif skill == "click_element":
            raw_target = str(args.get("target", "")).strip()
            explicit_pos = str(args.get("position", "")).strip().lower()
            arg_idx = args.get("index")
            # CRITICAL FIX: Only trust (x, y) if verified by Optic Nerve (never trust LLM-hallucinated coordinates!)
            vision_x = args.get("x") if args.get("_vision_verified") else None
            vision_y = args.get("y") if args.get("_vision_verified") else None
            raw_low = raw_target.lower()

            is_irreversible_buy = any(p in raw_low for p in ["place your order", "pay now", "complete purchase", "delete account"])
            if is_irreversible_buy and not args.get("_confirmed"):
                return save_pending_confirmation(skill_payload, f"You asked to click '{raw_target}', which is an irreversible action. Shall I proceed, sir?")

            is_add_to_cart = any(p in raw_low for p in ["add to cart", "add to bag", "buy now", "cart"]) and "cart icon" not in raw_low
            is_header_request = any(p in raw_low for p in ["home", "logo", "sign in", "login", "account", "orders", "returns", "cart icon", "tab"])
            min_valid_y = 10 if is_header_request else 190

            # 1. If this is an Add to Cart or Buy Now request, use the 3-Layer Cart Hunter immediately!
            if is_add_to_cart:
                cdp_res = _cdp_smart_click("buy now" in raw_low, active_window)
                if cdp_res:
                    return cdp_res
                btn = _hunt_add_to_cart(sw, sh, prefer_buy_now=("buy now" in raw_low), active_window=active_window)
                if btn:
                    bx, by, bname = btn
                    _human_click(bx, by, settle=0.6)
                    log_reversible_action("click_element", f"Clicked {bname}", "browser_back", "")
                    return f"Locked onto '{bname}' at ({bx}, {by}) and clicked it."

            # 2. Verified Optic Nerve Coordinates
            if vision_x is not None and vision_y is not None:
                vx, vy = int(vision_x), int(vision_y)
                if 5 < vx < sw and min_valid_y <= vy < sh:
                    _human_click(vx, vy, settle=0.5)
                    log_reversible_action("click_element", f"Clicked {raw_target}", "browser_back", "")
                    return f"Optic Nerve glided and clicked '{raw_target}' at ({vx}, {vy})."

            clean_target, pos, parsed_idx, color_name = clean_click_target_and_position(raw_target, explicit_pos)
            target_idx = int(arg_idx) if (arg_idx is not None and str(arg_idx).isdigit()) else parsed_idx

            if target_idx is not None:
                for e in indexed_elements:
                    if e["index"] == target_idx and e["cy"] >= min_valid_y:
                        _human_click(e["cx"], e["cy"], settle=0.5)
                        log_reversible_action("click_element", f"Clicked {e['text']}", "browser_back", "")
                        return f"Glided and clicked #{e['index']} [{e['zone']}] '{e['text']}' at ({e['cx']}, {e['cy']})."

            candidates = []
            t_low = clean_target.lower()
            stop_words = {"video", "videos", "thumbnail", "picture", "photo", "image", "item", "product", "clothes"}
            target_tokens = [tok for tok in re.split(r"\W+", t_low) if len(tok) >= 2 and tok not in stop_words]

            for e in indexed_elements:
                el_low = e["text"].lower()
                if not el_low or "address and search bar" in el_low:
                    continue
                if e["cy"] < min_valid_y:
                    continue
                if not is_header_request and el_low in ["amazon.in", "amazon", "youtube", "youtube home", "skip to main content"]:
                    continue

                if t_low and t_low in el_low:
                    candidates.append((100, e))
                elif target_tokens:
                    overlap = sum(1 for tok in target_tokens if tok in el_low)
                    if overlap > 0:
                        score = int((overlap / len(target_tokens)) * 85)
                        candidates.append((score, e))

            if not candidates and not target_tokens and (pos or color_name):
                candidates = [
                    (50, e) for e in indexed_elements
                    if e["id"].lower() in ["hyperlink", "button"] and e["cy"] >= 200
                ]

            if not candidates:
                return f"Element '{raw_target}' not found in content area."

            max_score = max(score for score, _ in candidates)
            best_pool = [e for score, e in candidates if score >= max_score - 15]

            if pos:
                if "left" in pos:
                    best_pool.sort(key=lambda e: e["cx"])
                    best_pool = best_pool[:max(1, (len(best_pool) + 1) // 2)]
                elif "right" in pos:
                    best_pool.sort(key=lambda e: -e["cx"])
                    best_pool = best_pool[:max(1, (len(best_pool) + 1) // 2)]
                elif "top" in pos:
                    best_pool.sort(key=lambda e: e["cy"])
                    best_pool = best_pool[:max(1, (len(best_pool) + 1) // 2)]
                elif "bottom" in pos:
                    best_pool.sort(key=lambda e: -e["cy"])
                    best_pool = best_pool[:max(1, (len(best_pool) + 1) // 2)]

            if color_name:
                best_pool.sort(key=lambda e: _score_element_color(e["bounds"], color_name, sw, sh), reverse=True)

            chosen = best_pool[0]
            _human_click(chosen["cx"], chosen["cy"], settle=0.5)
            log_reversible_action("click_element", f"Clicked {chosen['text']}", "browser_back", "")
            return f"Glided and clicked #{chosen['index']} [{chosen['zone']}] '{chosen['text']}' at ({chosen['cx']}, {chosen['cy']})."

        elif skill == "google_search":
            query = str(args.get("query", "")).strip()
            browser = str(args.get("browser", "")).strip().lower()
            encoded = urllib.parse.quote_plus(query)
            url = f"https://www.google.com/search?q={encoded}"
            use_bar = in_browser and ("google" in active_lower) and not browser
            _open_url(url, browser=browser, active_window=active_window, use_address_bar=use_bar)
            return f"Searched Google for '{query}'."

        elif skill == "type_text":
            text = str(args.get("text", ""))
            press_enter = bool(args.get("press_enter", False))
            clear_first = bool(args.get("clear_first", False))
            _paste_text(text, press_enter=press_enter, clear_first=clear_first)
            return f"Typed '{text}'."

        elif skill == "browser_nav":
            action = str(args.get("action", "back")).strip().lower()
            target_tab = str(args.get("target") or args.get("tab") or "").strip().lower()
            count = max(1, min(15, int(args.get("count", 1))))

            if action == "back":
                with kb.pressed(Key.alt):
                    kb.press(Key.left)
                    kb.release(Key.left)
                return "Navigated back."
            elif action == "forward":
                with kb.pressed(Key.alt):
                    kb.press(Key.right)
                    kb.release(Key.right)
                return "Navigated forward."
            elif action in ["refresh", "reload"]:
                kb.press(Key.f5)
                kb.release(Key.f5)
                return "Refreshed current page."
            elif action in ["next_tab", "switch_tab"]:
                if target_tab:
                    for ct in _cdp_get_tabs():
                        if target_tab in (ct.get("title") or "").lower() or target_tab in (ct.get("url") or "").lower():
                            if _cdp_activate_tab(ct["id"]):
                                return f"CDP switched directly to open tab '{ct.get('title')}'."
                    matched_tab = _find_open_browser_tab(target_tab, indexed_elements, sw, active_window)
                    if matched_tab:
                        _human_click(matched_tab["cx"], matched_tab["cy"], settle=0.35)
                        return f"Switched directly to open tab '{matched_tab['text']}'."
                    win_match = find_os_window_by_keyword(target_tab)
                    if win_match:
                        focus_os_window(win_match[0])
                        return f"Switched to your open '{win_match[1]}' window."
                _send_ctrl_shortcut("tab", shift=False)
                return "Switched to next tab."
            elif action == "prev_tab":
                _send_ctrl_shortcut("tab", shift=True)
                return "Switched to previous tab."

            elif action == "close_other_tabs":
                # 1. Try CDP first if enabled
                cdp_tabs = _cdp_get_tabs()
                if cdp_tabs:
                    active_cdp_id = cdp_tabs[0].get("id")
                    for ct in cdp_tabs:
                        t_title = (ct.get("title") or "").lower().strip()
                        if t_title and len(t_title) > 3 and t_title in active_lower:
                            active_cdp_id = ct.get("id")
                            break
                    closed_cdp = 0
                    for ct in cdp_tabs:
                        if ct.get("id") == active_cdp_id:
                            continue
                        t_combined = ((ct.get("title") or "") + " " + (ct.get("url") or "")).lower()
                        if not target_tab or target_tab in ["all", "other", "others", "duplicate", "this"] or target_tab in t_combined:
                            if _cdp_close_tab(ct["id"]):
                                closed_cdp += 1
                    if closed_cdp > 0:
                        log_reversible_action("close_other_tabs", f"Closed {closed_cdp} background tabs", "reopen_tab", str(closed_cdp))
                    return f"CDP Engine closed {closed_cdp} other tab(s) while keeping your active tab open."

                # 2. Use Hit-Test Tab Scanner so even when Chrome reports 0 DOM elements, we find every open tab!
                all_tabs = [e for e in indexed_elements if e["id"].lower() == "tabitem" or e["cy"] < 50]
                if not all_tabs:
                    all_tabs = _get_chrome_tabs_via_hittest(sw, active_window)

                if not all_tabs:
                    return "Could not detect open tabs on the browser tab bar."

                active_tabs = [t for t in all_tabs if t.get("is_active")]
                if not active_tabs:
                    clean_win = re.sub(r"\s*-\s*(?:google\s+chrome|brave|microsoft\s+edge).*$", "", active_lower, flags=re.I).strip()
                    for t in all_tabs:
                        t_clean = t["text"].lower().strip()
                        if t_clean and len(t_clean) > 3 and (t_clean[:14] in clean_win or clean_win[:14] in t_clean):
                            active_tabs.append(t)
                            break

                if not active_tabs:
                    return "Safety Abort: Could not verify which tab is currently active to protect it."

                keep_cx = active_tabs[0]["cx"]
                tabs_to_close = [t for t in all_tabs if abs(t["cx"] - keep_cx) > 25]

                if target_tab and target_tab not in ["all", "other", "others", "duplicate", "duplicates", "this", "current"]:
                    tabs_to_close = [t for t in tabs_to_close if target_tab in t["text"].lower()]

                tabs_to_close.sort(key=lambda item: -item["cx"])
                closed_count = 0
                for t in tabs_to_close:
                    _human_click(t["cx"], t["cy"], settle=0.22, button=Button.middle)
                    closed_count += 1

                if closed_count > 0:
                    log_reversible_action("close_other_tabs", f"Closed {closed_count} background tabs", "reopen_tab", str(closed_count))
                return f"Closed {closed_count} background tab(s) while protecting your current tab."

            elif action in ["close_tab", "close"]:
                if target_tab and target_tab not in active_lower and target_tab not in ["this", "current", "active", "one"]:
                    for ct in _cdp_get_tabs():
                        if target_tab in ((ct.get("title") or "") + (ct.get("url") or "")).lower():
                            if _cdp_close_tab(ct["id"]):
                                log_reversible_action("close_tab", f"Closed {ct.get('title')}", "reopen_tab", "1")
                                return f"CDP closed background tab '{ct.get('title')}'."
                    matched_tab = _find_open_browser_tab(target_tab, indexed_elements, sw, active_window)
                    if matched_tab:
                        _human_click(matched_tab["cx"], matched_tab["cy"], settle=0.25, button=Button.middle)
                        log_reversible_action("close_tab", f"Closed {matched_tab['text']}", "reopen_tab", "1")
                        return f"Closed background tab '{matched_tab['text']}'."

                # Click inside safe neutral page area or send hardware VK_CONTROL + VK_W so Chrome ALWAYS closes the tab!
                for _ in range(count):
                    _send_ctrl_shortcut("w")
                    time.sleep(0.18)
                log_reversible_action("close_tab", f"Closed {count} tab(s)", "reopen_tab", str(count))
                return f"Closed {count} tab(s) via hardware Ctrl+W."
            elif action == "new_tab":
                _send_ctrl_shortcut("t")
                return "Opened new tab."
            return f"Executed browser action '{action}'."

        elif skill == "press_shortcut":
            combo = str(args.get("keys", "")).strip().lower()
            if combo in ["play_pause", "media_play_pause", "pause", "play"]:
                # Hardware OS media play/pause interrupt (pauses background PW/YouTube/Spotify tabs)
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.keybd_event(0xB3, 0, 0x0002, 0)
            elif combo in ["space", "k"]:
                kb.press("k")
                kb.release("k")
            elif combo == "tab":
                kb.press(Key.tab)
                kb.release(Key.tab)
            elif combo == "enter":
                kb.press(Key.enter)
                kb.release(Key.enter)
            elif combo == "ctrl+t":
                _send_ctrl_shortcut("t")
            elif combo == "ctrl+w":
                _send_ctrl_shortcut("w")
            return f"Pressed shortcut '{combo}'."
        
        elif skill == "save_memory":
            fact = str(args.get("fact") or args.get("text") or args.get("message") or "").strip()
            key = str(args.get("key", "")).strip()
            res = save_memory_fact(fact, key)
            return f"Jarvis says: {res}"

        elif skill == "set_schedule":
            when = str(args.get("when", "+1m")).strip()
            command = str(args.get("command", "")).strip()
            return add_schedule(when, command)

        elif skill == "wait":
            seconds = min(10, max(1, int(args.get("seconds", 2))))
            time.sleep(seconds)
            return f"Waited {seconds} seconds."

        elif skill == "respond":
            message = (
                args.get("message")
                or args.get("response")
                or args.get("answer")
                or args.get("text")
                or skill_payload.get("thought")
                or "Task complete."
            )
            return f"Jarvis says: {str(message).strip()}"

        elif skill in ["read_screen_text", "scan_to_workspace"]:
            copied_text = _capture_active_screen_text()
            if not copied_text:
                return "Jarvis says: Screen text extraction failed or page is empty."

            append_workspace(active_window, copied_text)

            if skill == "scan_to_workspace":
                return f"Jarvis says: Pinned {len(copied_text)} characters from '{active_window}' to your research workspace."

            clipped = copied_text[:4500]
            return f"Jarvis says: [SYSTEM_SCREEN_DATA] {clipped}"

        elif skill == "clear_workspace":
            if not args.get("_confirmed"):
                return save_pending_confirmation(skill_payload, "Wiping the research workspace is permanent. Shall I proceed, sir?")
            clear_workspace_file()
            return "Jarvis says: Research workspace wiped clean for your new project."

        return f"Unknown skill '{skill}'."
    except Exception as e:
        return f"Skill execution error isolated: {e}"