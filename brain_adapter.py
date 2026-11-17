# brain_adapter.py
import os
import io
import re
import json
import time
import base64
import ctypes
import urllib.parse
import requests
from abc import ABC, abstractmethod
from typing import List
from dotenv import load_dotenv
from contracts import ObservationPayload, BrainDecision
import skills
from skills import (
    load_instructions,
    load_memory,
    save_memory_fact,
    update_memory_state,
    add_schedule,
    ensure_aux_files,
    load_workspace,
    compute_spatial_zone,
    detect_browser_from_title,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

try:
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    ensure_aux_files()
except Exception:
    pass

KNOWN_APP_KEYWORDS = {
    "brave": "brave",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calculator",
    "calc": "calculator",
    "paint": "paint",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "explorer": "explorer",
    "file explorer": "explorer",
    "task manager": "task manager",
    "settings": "settings",
    "vscode": "vscode",
    "visual studio code": "vscode",
    "code": "code",
}

KNOWN_SITE_URLS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "myntra": "https://www.myntra.com",
    "wikipedia": "https://www.wikipedia.org",
    "pw": "https://www.pw.live",
    "physics wallah": "https://www.pw.live",
}


def normalize_spoken_goal(raw_goal: str) -> str:
    """
    Human Intent & Self-Correction Engine:
    1) Detects mid-sentence aborts ('close notepad... wait' -> 'cancel').
    2) Detects mid-sentence pivots ('close notepad, wait no, open brave instead' -> 'open brave').
    3) Strips Whisper phonetic glitches ('for men' -> 'for me', 'northpad' -> 'notepad') and polite tails.
    """
    g = (raw_goal or "").strip()
    if "[SYSTEM_SCREEN_DATA]" in g:
        return g

    g = g.strip(" .?!,;:\"'")

    # 1. Fix common Whisper Indian-English phonetic mishearings first
    phonetic_fixes = [
        (r"\bnorth\s*pad\b", "notepad"),
        (r"\bnode\s*pad\b", "notepad"),
        (r"\bnote\s+pad\b", "notepad"),
        (r"\blove\s+pad\b", "notepad"),
        (r"\bcalculater\b", "calculator"),
        (r"\bamazon\.com\b", "amazon"),
        (r"\bamazan\b", "amazon"),
        (r"\bfor\s+men\b(?=\s*(?:instead|please|now|sir|$))", "for me"),
        (r"\bman's\s+kutta\b", "men's kurtas"),
        (r"\bmen's\s+kutta\b", "men's kurtas"),
        (r"\bman's\s+kurtas\b", "men's kurtas"),
        (r"\bcurtas\b", "kurtas"),
    ]
    for pat, repl in phonetic_fixes:
        g = re.sub(pat, repl, g, flags=re.I)

    # 2. Check if the user aborted their own command at the very end ("close notepad wait", "open youtube nevermind")
    if re.search(r"\b(?:wait|hold\s+on|never\s*mind|cancel\s+that|don't|stop|no\s+wait|forget\s+it)\s*$", g, flags=re.I):
        return "cancel"

    # 3. Check for mid-sentence human self-correction pivot ("close notepad, wait, open brave instead")
    pivot_split = re.split(
        r"\b(?:wait\s+no|no\s+wait|wait\s+a\s+second|wait|actually|never\s*mind|forget\s+that|i\s+mean|i\s+meant|no\s+i\s+said|i\s+said)\b[\s,.:;-]*",
        g,
        flags=re.I,
    )
    if len(pivot_split) > 1 and len(pivot_split[-1].strip()) >= 3:
        pivoted = pivot_split[-1].strip(" .?!,;:")
        # Remove any trailing "not X" comparison (e.g. "I said notepad, not northpad")
        pivoted = re.sub(r"\s+not\s+[a-z0-9\s]+$", "", pivoted, flags=re.I).strip()
        if not re.match(r"^(?:open|launch|start|close|search|find|click|scroll|go|switch|pause|play)\b", pivoted, flags=re.I):
            # If user said "open northpad... no I said notepad", preserve the original verb ("open")
            first_verb_m = re.match(r"^(open|launch|start|close|search\s+for|search)\b", pivot_split[0].strip(), flags=re.I)
            if first_verb_m:
                pivoted = f"{first_verb_m.group(1)} {pivoted}"
        g = pivoted

    # 4. Strip conversational prefixes ("hey jarvis", "can you please", "anyplace", "no no no")
    g = re.sub(
        r"^(?:(?:no[\s,]*)+|hey\s+jarvis[\s,]*|jarvis[\s,]*|hey\s+guys[\s,]*|"
        r"can\s+you\s+please[\s,]*|could\s+you\s+please[\s,]*|would\s+you\s+please[\s,]*|"
        r"can\s+you[\s,]*|could\s+you[\s,]*|anyplace[\s,]*|any\s+place[\s,]*|"
        r"please[\s,]*|just[\s,]*)+",
        "",
        g,
        flags=re.I,
    ).strip(" .?!,;:\"'")

    # 5. Strip conversational suffixes ("for me", "for me instead", "instead", "right now", "please", "sir")
    g = re.sub(
        r"\s+(?:for\s+me\s+instead|for\s+me\s+please|for\s+me|instead\s+please|instead|"
        r"right\s+now|now\s+please|please|sir|jarvis|bro)+$",
        "",
        g,
        flags=re.I,
    ).strip(" .?!,;:\"'")

    return g


def _get_all_open_os_windows() -> list[str]:
    titles = []
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _):
            try:
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        t = buf.value.strip()
                        if t and t not in ["Program Manager", "Settings", "Microsoft Text Input Application"]:
                            if not any(ig in t.lower() for ig in ["jarvis — live session", "jarvis hud"]):
                                titles.append(t)
            except Exception:
                pass
            return True

        cb_func = WNDENUMPROC(_cb)
        user32.EnumWindows(cb_func, 0)
    except Exception:
        pass
    return titles[:12]


class BaseBrainProvider(ABC):
    @abstractmethod
    def decide(self, observation: ObservationPayload, history: List[str]) -> BrainDecision:
        pass


class DualHybridBrain(BaseBrainProvider):
    def __init__(self, gemini_api_key: str | None = None, default_mode: str = "LOCAL"):
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        self.mode = os.getenv("JARVIS_BRAIN_MODE", "LOCAL").upper().strip()
        self.gemini_api_key = (gemini_api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self.ollama_base = "http://127.0.0.1:11434"
        self.local_model = self._detect_local_model()

        mem = load_memory()
        self.session_history: list[str] = list(mem.get("session_history", []))
        self.last_active_site: str = str(mem.get("last_active_site", ""))
        self.last_active_browser: str = str(mem.get("last_active_browser", "brave"))

        print(
            f"[Layer 3 Hybrid Brain Init] Mode: {self.mode} ({self.local_model}) | "
            f"Sticky Browser: {self.last_active_browser} | Optic Nerve: {'ONLINE' if self.gemini_api_key else 'MISSING_KEY'}"
        )

    def _detect_local_model(self) -> str:
        try:
            res = requests.get(f"{self.ollama_base}/api/tags", timeout=3)
            res.raise_for_status()
            models = [m.get("name", "") for m in res.json().get("models", []) if m.get("name")]
            for candidate in ["qwen2.5:7b", "llama3.1", "qwen2.5", "llama3.2", "qwen", "gemma"]:
                match = next((m for m in models if candidate in m.lower()), None)
                if match:
                    return match
            if models:
                return models[0]
        except Exception as e:
            print(f"[Layer 3 Ollama Check] Ollama not detected ({e})")
        return "qwen2.5:7b"

    def _get_active_window_and_ui(self, observation: ObservationPayload) -> tuple[str, str, str]:
        active_window = "Desktop"
        ui_formatted = []
        open_tabs = []
        sw = getattr(observation, "screen_width", 1920) or 1920
        sh = getattr(observation, "screen_height", 1080) or 1080

        idx = 1
        for el in (observation.ui_elements or [])[:55]:
            if el.id == "ACTIVE_WINDOW":
                active_window = el.text
            elif el.text:
                bounds = getattr(el, "bounds", None) or [0, 0, sw, sh]
                zone, cx, cy = compute_spatial_zone(bounds, sw, sh)
                is_act = getattr(el, "is_active", False) or (isinstance(el, dict) and el.get("is_active", False))
                act_tag = " [ACTIVE_TAB]" if is_act else ""
                entry = f'#{idx} [{zone} | ({cx},{cy}) | {el.id}{act_tag}] "{el.text}"'
                ui_formatted.append(entry)
                if el.id.lower() == "tabitem" or cy < 55:
                    open_tabs.append(f'Tab #{idx}{act_tag}: "{el.text}" at ({cx},{cy})')
                idx += 1

        al = active_window.lower()
        detected_b = detect_browser_from_title(active_window)
        if detected_b:
            self.last_active_browser = detected_b
            update_memory_state(last_browser=detected_b)

        for site in ["amazon", "flipkart", "myntra", "youtube", "wikipedia", "github", "gmail"]:
            if site in al:
                self.last_active_site = site
                update_memory_state(last_site=site)
                break

        tabs_str = ", ".join(open_tabs) if open_tabs else "Use Hit-Test Scanner"
        return active_window, "\n  ".join(ui_formatted[:35]), tabs_str

    def _prepare_vision_screenshot(self, observation: ObservationPayload) -> str:
        b64_img = getattr(observation, "screenshot_base64", "") or ""
        if not b64_img or b64_img.startswith("ACTIVE_WINDOW"):
            return ""
        try:
            from PIL import Image
            raw_bytes = base64.b64decode(b64_img)
            img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            img.thumbnail((960, 540))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=68)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            return b64_img

    def _box2d_to_screen_xy(self, box_2d: list, sw: int, sh: int) -> tuple[int, int] | None:
        if not isinstance(box_2d, list) or len(box_2d) != 4:
            return None
        try:
            ymin, xmin, ymax, xmax = [float(v) for v in box_2d]
            cx = int(((xmin + xmax) / 2.0) / 1000.0 * sw)
            cy = int(((ymin + ymax) / 2.0) / 1000.0 * sh)
            if 0 <= cx <= sw and 0 <= cy <= sh:
                return cx, cy
        except Exception:
            pass
        return None

    def _should_use_optic_nerve(self, goal: str, active_window: str) -> bool:
        gl = normalize_spoken_goal(goal).lower()
        if "[system_screen_data]" in gl:
            return False

        if any(p in gl for p in [
            "tab", "tabs", "switch to", "go back", "refresh", "reload", "clear workspace",
            "pin ", "status report", "briefing", "syllabus", "exam", "undo", "add to cart",
            "add this", "pause", "stop video", "stop the video"
        ]):
            return False
        if gl.startswith(("open ", "launch ", "start ", "close ", "clear ", "delete ", "scroll", "scrool", "page", "wait", "remember ", "schedule ")):
            return False

        visual_triggers = [
            "click", "press", "tap", "select", "choose", "pick", "enlarge", "filter",
            "sign in", "sign up", "register", "continue", "submit", "login", "log in",
            "type ", "enter ", "fill ", "input ", "email", "password", "phone", "mobile", "name",
            "blue", "red", "green", "black", "white", "yellow", "pink", "orange", "purple",
            "brown", "maroon", "grey", "left", "right", "top", "bottom", "middle", "center",
            "first", "second", "third", "1st", "2nd", "3rd", "kurta", "product", "video",
            "thumbnail", "image", "photo", "button", "play"
        ]
        return any(t in gl for t in visual_triggers)

    def _run_optic_nerve(self, observation: ObservationPayload, goal: str, ui_list: str) -> dict:
        result = {
            "report_text": "",
            "active_site": self.last_active_site,
            "target_xy": None,
            "target_index": None,
            "search_xy": None,
            "target_label": "",
        }
        if not self.gemini_api_key:
            return result

        clean_b64 = self._prepare_vision_screenshot(observation)
        if not clean_b64:
            return result

        sw = getattr(observation, "screen_width", 1920) or 1920
        sh = getattr(observation, "screen_height", 1080) or 1080

        vision_prompt = f"""You are the OPTIC NERVE (Visual Sensor) for JARVIS. Locate the exact 2D bounding box [ymin, xmin, ymax, xmax] (0 to 1000 scale) for the user's command.

USER COMMAND: "{goal}"

CRITICAL VISUAL RULES:
1. NEVER select the website's top-left home logo (e.g., Amazon.in logo or YouTube logo) or top navbar (ymin < 180) when the user asks to click a product, kurta, clothes, or video!
2. Products and video thumbnails ALWAYS sit in the main content area (ymin >= 180).
3. If the user names a person, face, ordinal position, or color in a video/product (e.g., "click the first kurta", "click the messi video", "tap on the leftmost white kurta"), visually inspect the actual thumbnail photos/garment colors on screen and return the center box of that exact thumbnail or its title!
4. If the user wants to type into a form field (name, email, password, search), return the box of that exact input field.

Return ONLY valid JSON:
{{
  "active_site": "amazon|youtube|flipkart|google|other",
  "target_found": true,
  "target_label": "Short description of target located",
  "target_box_2d": [ymin, xmin, ymax, xmax]
}}"""

        gen_cfg = {"temperature": 0.0, "responseMimeType": "application/json"}

        for model_name in ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash"]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_api_key}"
            payload = {
                "contents": [{
                    "parts": [
                        {"text": vision_prompt},
                        {"inline_data": {"mime_type": "image/jpeg", "data": clean_b64}},
                    ]
                }],
                "generationConfig": gen_cfg,
            }
            try:
                t0 = time.time()
                res = requests.post(url, json=payload, timeout=7)
                if res.status_code != 200:
                    continue
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(raw.strip())
                elapsed = time.time() - t0

                site = str(data.get("active_site") or "").strip().lower()
                if site in ["amazon", "flipkart", "myntra", "youtube", "wikipedia", "github"]:
                    self.last_active_site = site
                    result["active_site"] = site

                if data.get("target_found") and data.get("target_box_2d"):
                    t_xy = self._box2d_to_screen_xy(data["target_box_2d"], sw, sh)
                    if t_xy:
                        result["target_xy"] = t_xy
                        result["target_label"] = str(data.get("target_label") or "")

                if result["target_xy"]:
                    result["report_text"] = (
                        f"Site={site} | Target '{result['target_label']}' located at EXACT pixels: "
                        f"x={result['target_xy'][0]}, y={result['target_xy'][1]}"
                    )
                else:
                    result["report_text"] = f"Site={site} | Visual Target Located: False"

                print(f"\n[Optic Nerve 👁️ {model_name} ({elapsed:.2f}s)] {result['report_text']}")
                return result
            except Exception as e:
                print(f"[Optic Nerve Warning ({model_name})] {e}")
                continue

        return result

    def _deterministic_fast_router(self, raw_goal: str, active_window: str, observation: ObservationPayload) -> list | None:
        g = normalize_spoken_goal(raw_goal)
        gl = g.lower()
        active_lower = (active_window or "").lower()

        if "[system_screen_data]" in gl or " and " in gl or " then " in gl:
            return None

        # 0.0s CONFIRMATION GATE, SELF-CANCEL, UNDO, STATUS REPORT & BRIEFING
        if gl in ["confirm", "yes", "yes proceed", "proceed", "do it", "approved"]:
            return [{"thought": "Confirming pending action.", "skill": "confirm_action", "args": {}, "speak": "Proceeding, sir."}]
        if gl in ["cancel", "abort", "no", "stop action", "nevermind", "forget it"]:
            return [{"thought": "Cancelling action.", "skill": "cancel_action", "args": {}, "speak": "Aborted, sir. Standing by."}]
        if gl in ["undo", "undo that", "undo last action", "reverse that"]:
            return [{"thought": "Reversing last action.", "skill": "undo_action", "args": {}, "speak": "Reversing your last action, sir."}]

        # 0.0s ADD TO CART / BUY NOW
        if re.search(r"\b(?:add\s+(?:this\s+|that\s+|the\s+|.*?\s+)?to\s+(?:my\s+)?(?:cart|bag)|buy\s+now|buy\s+this\s+now)\b", gl):
            is_buy = "buy now" in gl or "buy this now" in gl
            return [{
                "thought": "Hunting and clicking Add to Cart button.",
                "skill": "add_to_cart",
                "args": {"buy_now": is_buy},
                "speak": "Adding to cart, sir."
            }]

        if any(p in gl for p in ["status report", "system status", "system health", "battery status", "hardware status", "diagnostics"]):
            return [{"thought": "Running hardware telemetry.", "skill": "system_status", "args": {}, "speak": "Running system diagnostics, sir."}]

        if any(p in gl for p in ["daily briefing", "morning briefing", "brief me", "give me a briefing", "my progress report", "show my tracker"]):
            return [{"thought": "Compiling daily briefing.", "skill": "daily_briefing", "args": {}, "speak": "Compiling your briefing, sir."}]

        # 1. 0.0s RESEARCH WORKSPACE CONTROLS
        if any(p in gl for p in ["clear workspace", "reset workspace", "wipe workspace", "new research project"]):
            return [{"thought": "Clearing workspace.", "skill": "clear_workspace", "args": {}, "speak": "Confirming workspace wipe, sir."}]

        if any(p in gl for p in ["pin this", "pin page", "add to workspace", "scan to workspace", "save to workspace"]):
            return [{"thought": "Pinning screen.", "skill": "scan_to_workspace", "args": {}, "speak": "Pinning page to workspace, sir."}]

        if any(phrase in gl for phrase in ["summarize what", "summarize this", "read this page", "what is written"]):
            return [{"thought": "Extracting screen text.", "skill": "read_screen_text", "args": {}, "speak": "Scanning screen text, sir."}]

        # 2. 0.0s PRECISION TRACKPAD SCROLLING
        if re.match(r"^(?:scroll|scrool|page|glide)\b", gl):
            nums = re.findall(r"\d+", gl)
            if nums:
                amt = int(nums[0])
            elif any(w in gl for w in ["little", "slightly", "bit", "slow", "tiny"]):
                amt = 2
            elif any(w in gl for w in ["lot", "more", "fast", "far", "much"]):
                amt = 7
            else:
                amt = 4

            if "top" in gl:
                return [{"thought": "Scrolling top.", "skill": "scroll_page", "args": {"direction": "top"}, "speak": "Top of the page, sir."}]
            elif "bottom" in gl:
                return [{"thought": "Scrolling bottom.", "skill": "scroll_page", "args": {"direction": "bottom"}, "speak": "Bottom of the page."}]
            elif "up" in gl:
                return [{"thought": "Scrolling up.", "skill": "scroll_page", "args": {"direction": "up", "amount": amt}, "speak": "Scrolling up."}]
            else:
                return [{"thought": "Scrolling down.", "skill": "scroll_page", "args": {"direction": "down", "amount": amt}, "speak": "Scrolling down."}]

        # 3. 0.0s TAB CLOSING
        is_close_verb = any(v in gl for v in ["close", "clear", "delete", "remove", "kill"])
        if is_close_verb and ("other" in gl or "except" in gl or "all" in gl or "duplicate" in gl) and ("tab" in gl or "this" in gl):
            target_site = ""
            for s in ["amazon", "youtube", "flipkart", "google", "github", "wikipedia"]:
                if s in gl:
                    target_site = s
                    break
            return [{
                "thought": f"Closing other '{target_site or 'all'}' tabs.",
                "skill": "browser_nav",
                "args": {"action": "close_other_tabs", "target": target_site},
                "speak": "Clearing other tabs, sir."
            }]

        if gl in [
            "close this one", "close this", "close tab", "close this tab", "close current tab",
            "delete this", "delete this one", "delete tab", "delete this tab",
            "clear this", "clear this tab", "clear tab", "remove this tab"
        ]:
            return [{"thought": "Closing current tab.", "skill": "browser_nav", "args": {"action": "close_tab", "count": 1}, "speak": "Closing tab, sir."}]

        # 4. 0.0s SYSTEM-WIDE MEDIA CONTROLS
        if gl in [
            "pause", "play", "pause video", "pause the video", "pause the video that is playing",
            "play video", "play the video", "stop video", "stop the video", "pause audio", "resume video"
        ]:
            return [{"thought": "Toggling system media.", "skill": "press_shortcut", "args": {"keys": "media_play_pause"}, "speak": "Toggling media, sir."}]

        # 5. 0.0s BROWSER & TAB SWITCHING
        if any(p in gl for p in [
            "previous tab", "prev tab", "go to the previous tab", "go to previous tab",
            "switch to the previous tab", "switch to previous tab", "go back a tab", "last tab"
        ]):
            return [{"thought": "Switching to previous tab.", "skill": "browser_nav", "args": {"action": "prev_tab"}, "speak": "Previous tab, sir."}]

        if any(p in gl for p in [
            "next tab", "change tab", "go to the next tab", "go to next tab",
            "switch to the next tab", "switch to next tab"
        ]):
            return [{"thought": "Switching to next tab.", "skill": "browser_nav", "args": {"action": "next_tab"}, "speak": "Next tab, sir."}]

        switch_match = re.match(r"^(?:go\s+back\s+to|return\s+to|switch\s+to|back\s+to|go\s+to)\s+(.+?)(?:\s+tab|\s+window)?$", gl)
        if switch_match:
            target_name = switch_match.group(1).strip(" .?!,;:")
            target_name = re.sub(r"^(?:the|my)\s+", "", target_name).strip()
            if target_name in KNOWN_SITE_URLS:
                return [{"thought": f"Switching to {target_name}.", "skill": "browser_nav", "args": {"action": "switch_tab", "target": target_name}, "speak": f"Switching to {target_name}, sir."}]

        if gl in ["go back", "back", "navigate back", "previous page"]:
            return [{"thought": "Navigating back.", "skill": "browser_nav", "args": {"action": "back"}, "speak": "Going back."}]
        if gl in ["go forward", "forward", "next page"]:
            return [{"thought": "Navigating forward.", "skill": "browser_nav", "args": {"action": "forward"}, "speak": "Going forward."}]
        if gl in ["refresh", "reload", "refresh page", "reload page"]:
            return [{"thought": "Refreshing page.", "skill": "browser_nav", "args": {"action": "refresh"}, "speak": "Refreshing."}]

        browser = ""
        for b in ["brave", "edge", "chrome", "firefox"]:
            if re.search(rf"\b(in|from|on|using|with)\s+{b}\b", gl):
                browser = b
                break

        # 6. 0.0s SMART ENTITY OPEN / LAUNCH (Extracts exact app/site even from messy sentences!)
        open_match = re.search(r"\b(?:open|launch|start|go\s+to)\s+([a-z0-9\.\-\s']+)$", gl)
        if open_match and "tab" not in gl:
            raw_target = open_match.group(1).strip(" .?!,;:")
            raw_target = re.sub(r"\s+(in|from|on|using|with)\s+(edge|chrome|brave|firefox|youtube|amazon).*$", "", raw_target).strip(" .?!,;:")
            raw_target = re.sub(r"\s+(?:for\s+me|for\s+men|instead|please|now|right\s+now|sir|app|browser|window).*$", "", raw_target).strip(" .?!,;:")

            # Check if any known site or known app is named inside raw_target
            matched_site = next((s for s in KNOWN_SITE_URLS if re.search(rf"\b{re.escape(s)}\b", raw_target)), None)
            matched_app = next(( KNOWN_APP_KEYWORDS[k] for k in KNOWN_APP_KEYWORDS if re.search(rf"\b{re.escape(k)}\b", raw_target)), None)
            has_real_domain = bool(re.search(r"\.[a-z]{2,4}$", raw_target))

            if matched_site:
                url = KNOWN_SITE_URLS[matched_site]
                self.last_active_site = matched_site
                update_memory_state(last_site=matched_site)
                return [{"thought": f"Opening {matched_site}.", "skill": "open_website", "args": {"url": url, "browser": browser}, "speak": f"Opening {matched_site}, sir."}]
            elif has_real_domain:
                return [{"thought": f"Opening {raw_target}.", "skill": "open_website", "args": {"url": raw_target, "browser": browser}, "speak": f"Opening {raw_target}, sir."}]
            elif matched_app:
                return [{"thought": f"Launching '{matched_app}'.", "skill": "launch_app", "args": {"app_name": matched_app}, "speak": f"Opening {matched_app}, sir."}]
            elif len(raw_target.split()) <= 2:
                return [{"thought": f"Launching '{raw_target}'.", "skill": "launch_app", "args": {"app_name": raw_target}, "speak": f"Opening {raw_target}, sir."}]

        # 7. 0.0s INSTANT APP CLOSING (Matches known app names cleanly)
        close_match = re.search(r"\b(?:close|quit|exit|kill)\s+([a-z0-9\s]+)$", gl)
        if close_match and "tab" not in gl and "one" not in gl and "this" not in gl:
            raw_app = close_match.group(1).strip(" .?!,;:")
            matched_app = next((KNOWN_APP_KEYWORDS[k] for k in KNOWN_APP_KEYWORDS if re.search(rf"\b{re.escape(k)}\b", raw_app)), raw_app.split()[0])
            return [{"thought": f"Closing {matched_app}.", "skill": "close_app", "args": {"app_name": matched_app}, "speak": f"Closing {matched_app}, sir."}]

        # 8. 0.0s INSTANT CONTEXT-AWARE SEARCH ROUTING
        search_match = re.search(r"\b(?:search\s+for|search|find|look\s+for|shop\s+for)\s+(.+)$", g, flags=re.I)
        if search_match:
            raw_q = search_match.group(1).strip(" .?!,;:")
            q = re.sub(r"\b(?:on|in|from|at|using|with)\s+(?:google|youtube|amazon|flipkart|myntra|the\s+web|web|internet|edge|brave|chrome|firefox)\b", "", raw_q, flags=re.I).strip(" .?!,;:")
            q = re.sub(r"^(?:for\s+)", "", q, flags=re.I).strip(" .?!,;:") or raw_q

            explicit_yt = "youtube" in gl
            explicit_amz = "amazon" in gl
            explicit_fk = "flipkart" in gl
            explicit_web = any(w in gl for w in ["google", "on the web", "from the web", "on web"])

            on_yt = explicit_yt or (not explicit_amz and not explicit_fk and not explicit_web and ("youtube" in active_lower or self.last_active_site == "youtube"))
            on_amz = explicit_amz or (not explicit_yt and not explicit_fk and not explicit_web and ("amazon" in active_lower or self.last_active_site == "amazon"))
            on_fk = explicit_fk or (not explicit_yt and not explicit_amz and not explicit_web and ("flipkart" in active_lower or self.last_active_site == "flipkart"))

            if on_yt:
                return [{"thought": f"Searching YouTube for '{q}'.", "skill": "youtube_search", "args": {"query": q, "browser": browser}, "speak": f"Searching YouTube for {q}, sir."}]
            elif on_amz:
                url = f"https://www.amazon.in/s?k={urllib.parse.quote_plus(q)}"
                self.last_active_site = "amazon"
                update_memory_state(last_site="amazon")
                if "amazon" in active_lower and not browser:
                    return [{"thought": f"Searching Amazon in-page for '{q}'.", "skill": "fill_input", "args": {"target": "search", "text": q, "press_enter": True}, "speak": f"Searching Amazon for {q}, sir."}]
                return [{"thought": f"Searching Amazon for '{q}'.", "skill": "open_website", "args": {"url": url, "browser": browser}, "speak": f"Searching Amazon for {q}, sir."}]
            elif on_fk:
                url = f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(q)}"
                self.last_active_site = "flipkart"
                update_memory_state(last_site="flipkart")
                return [{"thought": f"Searching Flipkart for '{q}'.", "skill": "open_website", "args": {"url": url, "browser": browser}, "speak": f"Searching Flipkart for {q}, sir."}]
            else:
                return [{"thought": f"Searching Google for '{q}'.", "skill": "google_search", "args": {"query": q, "browser": browser}, "speak": f"Searching Google for {q}, sir."}]

        # 9. 0.0s WAIT, MEMORY & SCHEDULES
        if gl.startswith("wait"):
            nums = re.findall(r"\d+", gl)
            secs = int(nums[0]) if nums else 2
            return [{"thought": "Waiting.", "skill": "wait", "args": {"seconds": secs}, "speak": f"Waiting {secs} seconds."}]

        mem_match = re.match(
            r"^(?:remember\s+that|remember|save\s+to\s+memory\s+that|save\s+to\s+memory|save\s+in\s+memory|save\s+memory|memorize\s+that|memorize|note\s+that)\s*[:,-]?\s*(.+)$",
            g,
            flags=re.I,
        )
        if mem_match:
            fact = mem_match.group(1).strip()
            return [{"thought": f"Saving fact: {fact}", "skill": "save_memory", "args": {"fact": fact}, "speak": "Noted in your permanent file, sir."}]

        m_sched = re.match(
            r"^(?:schedule|remind\s+me)\s+(?:at|in|for)?\s*(\+?\d+\s*(?:m|min|mins|minute|minutes)|\d{1,2}:\d{2})\s+(?:to\s+)?(.+)$",
            gl,
            flags=re.I,
        )
        if m_sched:
            when_str = m_sched.group(1).strip()
            cmd_str = m_sched.group(2).strip()
            return [{"thought": f"Scheduled '{cmd_str}'.", "skill": "set_schedule", "args": {"when": when_str, "command": cmd_str}, "speak": f"Scheduled {cmd_str}."}]

        return None

    def _build_prompt(self, observation: ObservationPayload, history: List[str] | None = None, optic_info: dict | None = None) -> str:
        active_win, ui_list, open_tabs = self._get_active_window_and_ui(observation)
        all_os_windows = ", ".join(_get_all_open_os_windows()) or active_win
        goal_text = normalize_spoken_goal(str(observation.user_goal or ""))

        if "[SYSTEM_SCREEN_DATA]" in goal_text:
            return f"""You are JARVIS. The screen text has ALREADY been extracted and saved to the workspace below.
USER REQUEST & EXTRACTED TEXT:
{goal_text}

Provide a HIGHLY DETAILED, comprehensive summary or answer based on the text above, keeping a touch of your signature dry British wit.
Do NOT call 'read_screen_text' or 'save_memory'. You MUST use the 'respond' skill.
Reply ONLY with this JSON array format:
[
  {{"thought": "Summarizing screen data", "skill": "respond", "args": {{"message": "Your detailed summary/answer here"}}, "speak": "Analysis complete, sir."}}
]"""

        instructions = load_instructions()
        mem_data = load_memory()
        combined_hist = (history or []) + mem_data.get("session_history", [])[-6:]
        recent_turns = "\n".join(combined_hist[-6:]) if combined_hist else "No prior turns yet."

        is_ui_action = bool(optic_info and optic_info.get("report_text"))
        workspace_snippet = "Omitted during UI action." if is_ui_action else load_workspace()[-3000:]
        optic_block = f"\nOPTIC NERVE TELEMETRY:\n  {optic_info['report_text']}\n" if is_ui_action else ""

        return f"""You are JARVIS, Tony Stark's hyper-competent, dryly sarcastic British AI agent.

STANDING INSTRUCTIONS: {instructions}
USER MEMORY: {json.dumps({k: v for k, v in mem_data.items() if k != 'session_history'})}
RECENT SESSION HISTORY:
{recent_turns}

FOREGROUND WINDOW: "{active_win}" (Active Site: "{self.last_active_site}")
ALL RUNNING OS WINDOWS (System-Wide Awareness): {all_os_windows}
OPEN BROWSER TABS: {open_tabs}
{optic_block}
VISIBLE UI ELEMENTS:
  {ui_list}

WORKSPACE DATA:
{workspace_snippet}

USER COMMAND: "{goal_text}"

REASONING & DECISION RULES:
1. HUMAN SELF-CORRECTION & INTENT:
   - If the user changed their mind mid-sentence ("close notepad, wait open brave instead"), execute ONLY their final intended action ("launch_app" -> "brave").
2. TAB & WINDOW MANAGEMENT:
   - To switch to the previous tab: `"skill": "browser_nav", "args": {{"action": "prev_tab"}}`.
   - To switch to the next tab: `"skill": "browser_nav", "args": {{"action": "next_tab"}}`.
   - To switch to a specific tab (e.g. PW, YouTube, Amazon): `"skill": "browser_nav", "args": {{"action": "switch_tab", "target": "<keyword>"}}`.
   - To close/clear/delete all other tabs except the current one: `"skill": "browser_nav", "args": {{"action": "close_other_tabs", "target": "optional site"}}`.
   - To close/delete the current tab: `"skill": "browser_nav", "args": {{"action": "close_tab", "count": 1}}`.
3. GLOBAL MEDIA PLAYBACK:
   - To pause or play any video/audio playing in the foreground or background: `"skill": "press_shortcut", "args": {{"keys": "media_play_pause"}}`.
4. E-COMMERCE ADD TO CART:
   - If the user asks to add an item to cart/bag or buy now, use `"skill": "add_to_cart", "args": {{"buy_now": false}}`.
5. CLICKS & FORMS:
   - To click a product, video, or button: `"skill": "click_element", "args": {{"target": "item description", "position": "left|right|top|bottom (optional)"}}`. (Do NOT invent x or y coordinates!).
   - To fill a form or search box: `"skill": "fill_input", "args": {{"target": "search|email|name|password", "text": "value", "press_enter": true}}`.

AVAILABLE SKILLS:
- "add_to_cart": {{"buy_now": false}}
- "click_element": {{"target": "label", "index": 5, "position": "left|right"}}
- "fill_input": {{"target": "search|email|name|password", "text": "value", "press_enter": true}}
- "browser_nav": {{"action": "close_other_tabs|close_tab|switch_tab|next_tab|prev_tab|back|forward|refresh|new_tab", "target": "optional tab keyword", "count": 1}}
- "press_shortcut": {{"keys": "media_play_pause|space|tab|enter|ctrl+t|ctrl+w"}}
- "manage_tracker": {{"category": "exam|syllabus|project", "action": "add|complete|list", "subject": "Physics", "title": "Rotational Motion"}}
- "system_status": {{}}
- "daily_briefing": {{}}
- "undo_action": {{}}
- "open_website": {{"url": "https://..."}}
- "youtube_search": {{"query": "search query"}}
- "google_search": {{"query": "search query"}}
- "scroll_page": {{"direction": "down|up|top|bottom", "amount": 4}}
- "launch_app": {{"app_name": "brave|notepad|calculator|code"}}
- "close_app": {{"app_name": "notepad|brave|calculator"}}
- "read_screen_text": {{}}
- "scan_to_workspace": {{}}
- "clear_workspace": {{}}
- "save_memory": {{"fact": "3rd-person fact"}}
- "set_schedule": {{"when": "+5m or HH:MM", "command": "task"}}
- "respond": {{"message": "Detailed answer or witty remark"}}

Reply ONLY with a valid JSON ARRAY (keep "thought" under 5 words):
[
  {{"thought": "Reasoning", "skill": "skill_name", "args": {{}}, "speak": "Short witty remark"}}
]"""

    def _extract_json(self, raw_text: str) -> list:
        cleaned = (raw_text or "").strip()
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1:
            return json.loads(cleaned[start:end + 1])
        start_obj = cleaned.find("{")
        end_obj = cleaned.rfind("}")
        if start_obj != -1 and end_obj != -1:
            return [json.loads(cleaned[start_obj:end_obj + 1])]
        raise ValueError(f"No JSON array found in: {cleaned[:80]}")

    def _package_skill_decision(self, skill_list, engine_tag: str, user_goal: str = "", optic_info: dict | None = None) -> BrainDecision:
        if isinstance(skill_list, dict):
            skill_list = [skill_list]
        if not skill_list:
            raise ValueError("Empty skill array")

        optic_info = optic_info or {}
        combined_thought = " -> ".join([str(s.get("thought", "Action")) for s in skill_list if isinstance(s, dict)])
        combined_speak = " ".join([str(s.get("speak", "")) for s in skill_list if isinstance(s, dict)]).strip() or "Right away, sir."

        turn_nonce = int(time.time() * 1000) % 100000
        clean_payloads = []

        for s in skill_list:
            if not isinstance(s, dict):
                continue
            skill_name = str(s.get("skill", "wait")).strip().lower()
            raw_args = s.get("args")
            args = dict(raw_args) if isinstance(raw_args, dict) else {}

            args.pop("x", None)
            args.pop("y", None)
            args.pop("_vision_verified", None)

            for k in ["message", "response", "answer", "text", "fact", "query", "url", "app_name", "target", "tab", "count", "position", "index", "direction", "amount", "action", "keys", "when", "command", "press_enter", "category", "subject", "title", "date", "buy_now"]:
                if k in s and k not in args:
                    args[k] = s[k]

            if skill_name in ["click_element", "click", "tap", "press"]:
                if optic_info.get("target_xy"):
                    args["x"], args["y"] = optic_info["target_xy"]
                    args["_vision_verified"] = True
                if not args.get("target"):
                    args["target"] = user_goal

            elif skill_name in ["fill_input", "fill", "input"]:
                if optic_info.get("target_xy"):
                    args["x"], args["y"] = optic_info["target_xy"]
                    args["_vision_verified"] = True

            if skill_name in ["youtube_search", "open_website"] and "youtube" in str(args).lower():
                pushback_fn = getattr(skills, "get_pushback_context", None)
                pushback_info = pushback_fn() if callable(pushback_fn) else ""
                if pushback_info:
                    combined_speak = f"{combined_speak} Though may I gently remind you, sir, of your {pushback_info}."

            if skill_name == "respond" and not args.get("message"):
                args["message"] = str(args.get("response") or args.get("answer") or args.get("text") or s.get("speak") or s.get("thought") or "Completed, sir.")

            if skill_name == "save_memory":
                fact_val = args.get("fact") or args.get("text") or args.get("message")
                if fact_val:
                    args["fact"] = str(fact_val)
                    save_memory_fact(str(fact_val))
            elif skill_name == "set_schedule" and args.get("command"):
                add_schedule(str(args.get("when", "+1m")), str(args.get("command")))

            clean_payloads.append({
                "skill": skill_name,
                "args": args,
                "thought": str(s.get("thought", "")),
                "_nonce": turn_nonce,
            })

            if skill_name in ["read_screen_text", "scan_to_workspace"]:
                break

        if user_goal and "[SYSTEM_SCREEN_DATA]" not in user_goal:
            summary_actions = ", ".join([f"{p['skill']}({p['args']})" for p in clean_payloads])
            turn_record = f"User: {user_goal} -> Jarvis: {summary_actions}"
            self.session_history.append(turn_record)
            self.session_history = self.session_history[-12:]
            update_memory_state(turn_entry=turn_record)

        payload_str = json.dumps(clean_payloads)
        return BrainDecision(
            thought=f"[{engine_tag} #{turn_nonce}] Workflow: {combined_thought[:140]}",
            action_type="TYPE",
            text_to_type=f"[SKILL] {payload_str}",
            speak_out_loud=combined_speak,
        )

    def _call_local_ollama(self, observation: ObservationPayload, history: List[str] | None = None, optic_info: dict | None = None) -> BrainDecision:
        prompt = self._build_prompt(observation, history, optic_info)
        is_ui_turn = bool(optic_info and optic_info.get("report_text"))
        payload = {
            "model": self.local_model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0.0,
                "num_predict": 95 if is_ui_turn else 350,
                "num_ctx": 2048 if is_ui_turn else 4096,
            },
        }
        t0 = time.time()
        res = requests.post(f"{self.ollama_base}/api/generate", json=payload, timeout=90)
        if res.status_code == 404:
            self.local_model = self._detect_local_model()
            payload["model"] = self.local_model
            res = requests.post(f"{self.ollama_base}/api/generate", json=payload, timeout=90)
        res.raise_for_status()
        parsed = self._extract_json(res.json().get("response", ""))
        elapsed = time.time() - t0
        tag = f"EYES:Flash + BRAIN:{self.local_model} {elapsed:.2f}s" if is_ui_turn else f"LOCAL:{self.local_model} {elapsed:.2f}s"
        return self._package_skill_decision(parsed, tag, str(observation.user_goal or ""), optic_info)

    def _call_cloud_gemini(self, observation: ObservationPayload, history: List[str] | None = None, optic_info: dict | None = None) -> BrainDecision:
        if not self.gemini_api_key:
            load_dotenv(dotenv_path=ENV_PATH, override=True)
            self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not self.gemini_api_key:
            raise RuntimeError(f"GEMINI_API_KEY missing in {ENV_PATH}")

        prompt = self._build_prompt(observation, history, optic_info)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }
        last_err = None
        for model_name in ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash"]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_api_key}"
            try:
                t0 = time.time()
                res = requests.post(url, json=payload, timeout=30)
                if res.status_code != 200:
                    last_err = f"{model_name} HTTP {res.status_code}: {res.text[:120]}"
                    continue
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = self._extract_json(raw)
                elapsed = time.time() - t0
                return self._package_skill_decision(parsed, f"CLOUD:{model_name} {elapsed:.1f}s", str(observation.user_goal or ""), optic_info)
            except Exception as e:
                last_err = f"{model_name}: {e}"
                continue
        raise RuntimeError(f"Cloud Gemini failed ({last_err})")

    def decide(self, observation: ObservationPayload, history: List[str]) -> BrainDecision:
        try:
            raw_goal = str(observation.user_goal or "").strip()
            goal = normalize_spoken_goal(raw_goal)
            goal_lower = goal.lower()

            if goal_lower in ["switch to cloud", "use cloud", "cloud mode"]:
                self.mode = "CLOUD"
                return BrainDecision(thought="[HYBRID] Switched to CLOUD.", action_type="DONE", speak_out_loud="Cloud mode active, sir.")

            if goal_lower in ["switch to local", "use local", "local mode"]:
                self.mode = "LOCAL"
                return BrainDecision(thought="[HYBRID] Switched to LOCAL.", action_type="DONE", speak_out_loud="Local mode active, sir.")

            active_win, ui_list, _ = self._get_active_window_and_ui(observation)

            fast_skill = self._deterministic_fast_router(goal, active_win, observation)
            if fast_skill:
                return self._package_skill_decision(fast_skill, "FAST-ROUTER 0.0s", goal)

            optic_info = {}
            if self._should_use_optic_nerve(goal, active_win):
                optic_info = self._run_optic_nerve(observation, goal, ui_list)
                if optic_info.get("target_xy") and not any(w in goal_lower for w in ["type ", "enter ", "fill ", "input "]):
                    lbl = optic_info.get("target_label") or goal
                    direct_click = [{
                        "thought": f"Optic Nerve locked onto '{lbl}'.",
                        "skill": "click_element",
                        "args": {"target": lbl},
                        "speak": f"Selecting {lbl}, sir."
                    }]
                    return self._package_skill_decision(direct_click, "OPTIC-DIRECT 0.5s", goal, optic_info)

            chain = (
                [("LOCAL", self._call_local_ollama), ("CLOUD", self._call_cloud_gemini)]
                if self.mode == "LOCAL"
                else [("CLOUD", self._call_cloud_gemini), ("LOCAL", self._call_local_ollama)]
            )
            errors = []
            for name, fn in chain:
                try:
                    return fn(observation, history, optic_info)
                except Exception as e:
                    print(f"[Layer 3 {name} Error] {e}")
                    errors.append(f"{name}: {e}")

            return BrainDecision(
                thought=f"All brains failed ({' | '.join(errors)[:140]})",
                action_type="ERROR",
                speak_out_loud="Error.",
            )
        except Exception as e:
            return BrainDecision(thought=f"Isolated error: {e}", action_type="ERROR", speak_out_loud="Error.")


LocalOllamaBrain = DualHybridBrain
CloudGeminiBrain = DualHybridBrain