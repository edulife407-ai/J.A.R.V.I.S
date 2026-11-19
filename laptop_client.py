# laptop_client.py
import os
import re
import sys
import time
import ctypes
import base64
import io
import json
import threading
import itertools
import keyboard
import tkinter as tk
from tkinter import simpledialog
from contracts import ObservationPayload
from brain_adapter import DualHybridBrain, normalize_spoken_goal
from skills import execute_skill, pop_due_schedules, ensure_aux_files

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(0.15)
    HAS_UIA = True
except Exception:
    HAS_UIA = False

VOICE_PLUGIN = None
AGENT_EXEC_LOCK = threading.Lock()
TURN_CANCEL_EVENT = threading.Event()
ACTIVE_TURN_ID = 0
VOICE_TURN_COUNTER = 1

# Direct in-memory Brain (Zero localhost:8000 HTTP lag or 1.5s cooldown!)
JARVIS_BRAIN = DualHybridBrain(default_mode="CLOUD")


def cancel_active_turn():
    """Immediately aborts any in-flight agent turn so a barge-in or 'Wait!' stops execution in <10ms."""
    global ACTIVE_TURN_ID
    ACTIVE_TURN_ID += 1
    TURN_CANCEL_EVENT.set()


def try_attach_voice_module(handle_voice_cb):
    global VOICE_PLUGIN
    if "--no-voice" in sys.argv:
        print("🔌 [Voice Module Disabled via --no-voice] Running in Ctrl+Alt+J Hotkey-Only Mode.")
        return None
    try:
        from voice_module import JarvisVoicePlugin
        VOICE_PLUGIN = JarvisVoicePlugin(
            external_command_handler=handle_voice_cb,
            cancel_turn_callback=cancel_active_turn,
        )
        VOICE_PLUGIN.start_background_daemon()
        return VOICE_PLUGIN
    except ImportError:
        print("🔌 [Voice Module Not Found] 'voice_module.py' is detached. Running in Ctrl+Alt+J Hotkey-Only Mode.")
    except Exception as e:
        print(f"⚠️ [Voice Module Init Warning] ({e}) -> Falling back to Ctrl+Alt+J Hotkey Mode.")
    return None


PRIMARY_INTERACTIVE_TYPES = {
    "EditControl", "ButtonControl", "HyperlinkControl",
    "CheckBoxControl", "ComboBoxControl", "RadioButtonControl",
    "TabItemControl",
}


def loading_animation(stop_event: threading.Event):
    messages = [
        "Optic Nerve locking target...",
        "Neural Core computing...",
    ]
    spinner = itertools.cycle(["|", "/", "-", "\\"])
    i = 0
    while not stop_event.is_set() and not TURN_CANCEL_EVENT.is_set():
        msg = messages[(i // 15) % len(messages)]
        sys.stdout.write(f"\r[Jarvis] {next(spinner)} {msg}")
        sys.stdout.flush()
        time.sleep(0.08)
        i += 1
    sys.stdout.write("\r" + " " * 65 + "\r")
    sys.stdout.flush()


def get_screen_size() -> tuple[int, int]:
    try:
        user32 = ctypes.windll.user32
        return max(200, min(7680, int(user32.GetSystemMetrics(0)))), max(200, min(4320, int(user32.GetSystemMetrics(1))))
    except Exception:
        return 1920, 1080


def capture_compressed_screenshot() -> str | None:
    try:
        import mss
        from PIL import Image
        with mss.MSS() as sct:
            sct_img = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img.thumbnail((1280, 720))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def get_active_window_info() -> tuple[str, int]:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Desktop", 0
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if title:
                return title, hwnd
        return "Desktop", hwnd
    except Exception:
        return "Desktop", 0


def restore_window_focus(hwnd: int):
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        curr_fg = user32.GetForegroundWindow()
        if curr_fg == hwnd:
            return
        fg_thread = user32.GetWindowThreadProcessId(curr_fg, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        if fg_thread and target_thread and fg_thread != target_thread:
            user32.AttachThreadInput(fg_thread, target_thread, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(fg_thread, target_thread, False)
        else:
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
    except Exception:
        pass


def is_instant_os_command(goal: str) -> bool:
    gl = normalize_spoken_goal(goal).lower()
    if any(p in gl for p in ["what time", "what is the time", "current time", "today's date", "switch to", "go back to"]):
        return True
    # Text-only factual/math/live-web questions do not need a 450ms UIA DOM scan or screenshot
    if any(p in gl for p in ["aqi", "air quality", "weather", "temperature", "squared", "integral", "derivative", "who is", "what is", "what's", "explain"]) and not any(s in gl for s in ["screen", "this page", "written", "summarize"]):
        return True
    if gl in [
        "close this one", "close this", "close tab", "close this tab",
        "delete this", "delete this one", "delete tab", "delete this tab",
        "clear this", "clear tab", "clear this tab",
        "go back", "back", "go forward", "refresh", "reload",
        "next tab", "go to the next tab", "go to next tab", "switch to the next tab", "switch to next tab",
        "prev tab", "previous tab", "go to the previous tab", "go to previous tab",
        "switch to the previous tab", "switch to previous tab", "go back a tab", "last tab",
        "pause", "play", "pause video", "pause the video", "pause the video that is playing",
        "play video", "play the video", "stop video", "stop the video", "pause audio", "resume video",
        "status report", "system status", "system health", "battery status",
        "daily briefing", "morning briefing", "brief me", "undo", "undo that", "confirm", "yes", "cancel"
    ]:
        return True
    if re.search(r"\b(?:add\s+(?:this\s+|that\s+|the\s+|.*?\s+)?to\s+(?:my\s+)?(?:cart|bag)|buy\s+now)\b", gl):
        return True
    if re.match(r"^(?:open|launch|start|close|scroll|scrool|glide)\b", gl):
        return True
    return False


def _is_tab_selected(ctrl, window_title: str, tab_name: str) -> bool:
    try:
        sel_pat = ctrl.GetSelectionItemPattern()
        if sel_pat and sel_pat.IsSelected:
            return True
    except Exception:
        pass
    try:
        leg_pat = ctrl.GetLegacyIAccessiblePattern()
        if leg_pat and (int(leg_pat.State) & 0x2):
            return True
    except Exception:
        pass
    wt_low = (window_title or "").lower().strip()
    tn_low = (tab_name or "").lower().strip()
    if tn_low and len(tn_low) >= 3 and wt_low.startswith(tn_low[:15]):
        return True
    return False


def scan_screen_ui(screen_w: int, screen_h: int, fast_skip: bool = False) -> tuple[list[dict], str]:
    ui_elements = []
    live_win_title, _ = get_active_window_info()
    fg_name = live_win_title or "Desktop"

    ui_elements.append({
        "id": "ACTIVE_WINDOW",
        "text": fg_name[:80],
        "clickable": False,
        "bounds": [0, 0, screen_w, screen_h],
    })

    if not HAS_UIA or fast_skip:
        return ui_elements, fg_name

    t_start = time.perf_counter()
    tabs = []
    edits = []
    clickables = []
    seen_keys = set()

    try:
        with auto.UIAutomationInitializerInThread():
            fg = auto.GetForegroundControl()
            detected_name = (fg.Name or fg.ClassName or "").strip() if fg else ""
            if fg_name in ["Desktop", ""] and detected_name and detected_name.lower() not in ["tk", "jarvis", "python", "jarvis hud"]:
                fg_name = detected_name
                ui_elements[0]["text"] = fg_name[:80]

            if fg:
                queue = [(fg, 0)]
                while queue and (len(tabs) + len(edits) + len(clickables)) < 55:
                    if (time.perf_counter() - t_start) > 0.40:
                        break
                    ctrl, depth = queue.pop(0)
                    if depth > 14:
                        continue
                    try:
                        c_type = ctrl.ControlTypeName
                        c_name = " ".join((ctrl.Name or "").strip().split())
                        if c_type in PRIMARY_INTERACTIVE_TYPES and c_name and not ctrl.IsOffscreen:
                            r = ctrl.BoundingRectangle
                            if r.width() > 10 and r.height() > 10:
                                bounds = [
                                    max(0, int(r.left)),
                                    max(0, int(r.top)),
                                    min(screen_w, int(r.right)),
                                    min(screen_h, int(r.bottom)),
                                ]
                                dedup_key = (c_type, c_name[:40].lower(), bounds[0] // 15, bounds[1] // 15)
                                if dedup_key not in seen_keys:
                                    seen_keys.add(dedup_key)
                                    is_act = _is_tab_selected(ctrl, fg_name, c_name) if c_type == "TabItemControl" else False
                                    item = {
                                        "id": c_type.replace("Control", ""),
                                        "text": c_name[:65],
                                        "clickable": True,
                                        "bounds": bounds,
                                        "is_active": is_act,
                                    }
                                    if c_type == "TabItemControl":
                                        tabs.append(item)
                                    elif c_type in ["EditControl", "ComboBoxControl"]:
                                        edits.append(item)
                                    else:
                                        clickables.append(item)

                        if depth < 14:
                            for child in ctrl.GetChildren()[:30]:
                                queue.append((child, depth + 1))
                    except Exception:
                        continue
    except Exception:
        pass

    ui_elements.extend(tabs[:15])
    ui_elements.extend(edits[:10])
    ui_elements.extend(clickables[:30])
    return ui_elements, fg_name


def run_agent(user_goal: str, session_id: str, target_hwnd: int = 0) -> tuple[str, str, bool]:
    """
    Executes a turn directly in memory with mid-flight cancellation checks (`TURN_CANCEL_EVENT`).
    Returns `(last_status, spoken_reply, was_cancelled)`.
    """
    global ACTIVE_TURN_ID
    TURN_CANCEL_EVENT.clear()
    ACTIVE_TURN_ID += 1
    my_turn_id = ACTIVE_TURN_ID

    with AGENT_EXEC_LOCK:
        if TURN_CANCEL_EVENT.is_set() or my_turn_id != ACTIVE_TURN_ID:
            return "Cancelled.", "", True

        print(f"\n=== JARVIS EXECUTING: '{user_goal}' ===")
        current_goal_context = user_goal
        last_status = "Ready."
        spoken_reply = ""

        for step in range(1, 4):
            if TURN_CANCEL_EVENT.is_set() or my_turn_id != ACTIVE_TURN_ID:
                print("🛑 [Turn Aborted Mid-Flight by User]")
                return "Cancelled.", "", True

            if target_hwnd:
                restore_window_focus(target_hwnd)

            fast_skip = (step == 1) and is_instant_os_command(current_goal_context)
            if not fast_skip:
                time.sleep(0.05)

            screen_w, screen_h = get_screen_size()
            ui_elements, active_window = scan_screen_ui(screen_w, screen_h, fast_skip=fast_skip)
            screenshot_b64 = None if fast_skip else capture_compressed_screenshot()

            if step == 1:
                mode_lbl = "0.0s Fast-Path" if fast_skip else f"{len(ui_elements) - 1} DOM elements + Live Screenshot"
                print(f'[Active Window] "{active_window}" ({mode_lbl})')

            obs_payload = ObservationPayload(
                session_id=f"{session_id}_s{step}",
                device_type="LAPTOP",
                screen_width=screen_w,
                screen_height=screen_h,
                user_goal=current_goal_context,
                step_number=step,
                screenshot_base64=screenshot_b64 or f'ACTIVE_WINDOW: "{active_window}"',
                ui_elements=ui_elements,
                kill_switch_triggered=False,
            )

            try:
                stop_event = threading.Event()
                loader_thread = threading.Thread(target=loading_animation, args=(stop_event,))
                loader_thread.daemon = True
                loader_thread.start()

                try:
                    decision = JARVIS_BRAIN.decide(obs_payload, JARVIS_BRAIN.session_history[-5:])
                finally:
                    stop_event.set()
                    loader_thread.join()

                # Check if user said "Wait!" or barged in while the brain was thinking!
                if TURN_CANCEL_EVENT.is_set() or my_turn_id != ACTIVE_TURN_ID:
                    print("🛑 [Turn Aborted Before Action — Discarding Stale Brain Response]")
                    return "Cancelled.", "", True
            except Exception as e:
                print(f"[Brain Execution Error] {e}")
                last_status = "Brain Error."
                spoken_reply = "I encountered an error, sir."
                break

            thought_str = decision.thought or ""
            speak_str = decision.speak_out_loud or ""
            print(f"Step {step} Thought: {thought_str}")
            spoken_reply = speak_str
            if speak_str:
                print(f'  [Jarvis Voice] "{speak_str}"')
                last_status = speak_str

            text_to_type = str(decision.text_to_type or "").strip()
            tool_outputs = []
            needs_followup_step = False

            if text_to_type.startswith("[SKILL]"):
                raw_json = text_to_type[7:].strip()
                try:
                    skill_payloads = json.loads(raw_json)
                    if isinstance(skill_payloads, dict):
                        skill_payloads = [skill_payloads]

                    for sp in skill_payloads:
                        if TURN_CANCEL_EVENT.is_set() or my_turn_id != ACTIVE_TURN_ID:
                            print("🛑 [Skill Execution Aborted by User]")
                            return "Cancelled.", "", True

                        skill_name = str(sp.get("skill", "")).strip().lower()
                        if target_hwnd and skill_name not in [
                            "respond", "save_memory", "set_schedule", "wait",
                            "clear_workspace", "launch_app", "close_app",
                            "system_status", "daily_briefing", "manage_tracker"
                        ]:
                            restore_window_focus(target_hwnd)

                        print(f"  [Action] Executing '{skill_name}'...")
                        result = execute_skill(sp, active_window, ui_elements)
                        print(f"  [Result] {result}")
                        tool_outputs.append(result)
                        clean_res = result.replace("Jarvis says: ", "").strip()
                        last_status = clean_res

                        if result.startswith("Jarvis says:") and "[SYSTEM_SCREEN_DATA]" not in result:
                            spoken_reply = clean_res

                        if skill_name == "read_screen_text":
                            needs_followup_step = True
                        time.sleep(0.12)

                except Exception as e:
                    print(f"[Skill Parse Error] {e}")

            if needs_followup_step and tool_outputs:
                combined_feedback = " ".join(tool_outputs)
                current_goal_context = f"{user_goal}\n[SYSTEM_SCREEN_DATA]: {combined_feedback}"
                time.sleep(1.0)
            else:
                break

        print("Turn finished: COMPLETED")
        return last_status, spoken_reply, False


def handle_voice_command_from_plugin(voice_goal: str, gen_id: int, turn_emotion: str):
    global VOICE_TURN_COUNTER
    _, current_hwnd = get_active_window_info()
    session_id = f"voice_{int(time.time() * 1000)}_t{VOICE_TURN_COUNTER}"
    VOICE_TURN_COUNTER += 1

    _, spoken_reply, was_cancelled = run_agent(voice_goal, session_id=session_id, target_hwnd=current_hwnd)

    if not was_cancelled and VOICE_PLUGIN is not None and not VOICE_PLUGIN.detached:
        VOICE_PLUGIN.mouth.speak_turn_answer(spoken_reply, gen_id=gen_id, emotion=turn_emotion)


def schedule_daemon_loop():
    while True:
        try:
            due_tasks = pop_due_schedules()
            for cmd in due_tasks:
                print(f"\n[SCHEDULE TRIGGERED] Running: {cmd}")
                _, spoken, was_cancelled = run_agent(cmd, session_id="sched_" + str(int(time.time())))
                if not was_cancelled and VOICE_PLUGIN is not None and not VOICE_PLUGIN.detached:
                    VOICE_PLUGIN.mouth.speak_non_blocking(spoken)
        except Exception:
            pass
        time.sleep(10)


def get_session_command(target_hwnd: int, turn_num: int, last_status: str = "") -> str | None:
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        status_preview = (last_status[:95] + "...") if len(last_status) > 95 else last_status
        prompt_label = (
            f"[Turn {turn_num}] {status_preview}\n\nNext step (or Esc / 'done' to pause hotkey session):"
            if turn_num > 1
            else "Enter command for Jarvis (Ctrl+Alt+J Hotkey Mode — Voice Paused While Typing):"
        )

        goal = simpledialog.askstring("Jarvis — Live Session", prompt_label, parent=root)
        root.destroy()

        if goal and goal.strip():
            restore_window_focus(target_hwnd)
            return goal.strip()
        return None
    except Exception:
        return None


if __name__ == "__main__":
    ensure_aux_files()
    from hud_overlay import start_jarvis_hud
    start_jarvis_hud()

    threading.Thread(target=schedule_daemon_loop, daemon=True).start()

    print("=== JARVIS DUAL-MODE ENGINE (BIOMETRIC VOICE + CTRL+ALT+J HOTKEY) ===")
    try_attach_voice_module(handle_voice_command_from_plugin)

    def _on_toggle_voice():
        if VOICE_PLUGIN is not None:
            VOICE_PLUGIN.toggle_detach()
        else:
            print("\n🔌 [Voice Module Not Loaded] Attempting to hot-load 'voice_module.py'...")
            try_attach_voice_module(handle_voice_command_from_plugin)

    try:
        keyboard.add_hotkey("ctrl+alt+v", _on_toggle_voice)
    except Exception:
        pass

    while True:
        try:
            keyboard.wait("ctrl+alt+j")
            if VOICE_PLUGIN is not None:
                VOICE_PLUGIN.pause_for_hotkey()

            base_session = "sess_" + str(int(time.time() * 1000))
            turn = 1
            last_feedback = ""

            try:
                while True:
                    _, current_hwnd = get_active_window_info()
                    goal = get_session_command(current_hwnd, turn_num=turn, last_status=last_feedback)

                    if not goal or goal.lower() in ["done", "exit", "quit", "stop", "sleep", "close"]:
                        print("[Hotkey Session Paused] Returning to standby (Voice & Ctrl+Alt+J Ready).")
                        break

                    turn_session_id = f"{base_session}_t{turn}"
                    last_feedback, spoken_out, was_cancelled = run_agent(goal, session_id=turn_session_id, target_hwnd=current_hwnd)

                    if not was_cancelled and VOICE_PLUGIN is not None and not VOICE_PLUGIN.detached and spoken_out:
                        VOICE_PLUGIN.mouth.speak_non_blocking(spoken_out)

                    turn += 1
                    time.sleep(0.25)
            finally:
                if VOICE_PLUGIN is not None:
                    VOICE_PLUGIN.resume_after_hotkey()

        except KeyboardInterrupt:
            if VOICE_PLUGIN is not None:
                VOICE_PLUGIN.mouth.stop_immediately()
            break
        except Exception as e:
            print(f"[Daemon Error] {e}")
            time.sleep(1)