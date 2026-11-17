# main.py
import os
import json
import time
import traceback
from datetime import datetime
from typing import Dict, List
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from contracts import ObservationPayload, ActionCommand, BrainDecision
from brain_adapter import BaseBrainProvider, LocalOllamaBrain

app = FastAPI(title="Jarvis Layer 2 - Nervous System", version="1.0.0")

# =====================================================================
# HARDWARE THERMAL & RUNAWAY SAFETY CONSTANTS
# =====================================================================
MIN_COOLDOWN_SECONDS = 1.5   # Matches contracts.py ge=1500 hardware rest
MAX_STEPS_PER_GOAL = 12      # Hard cutoff so an agent never runs forever
MAX_IDENTICAL_TAPS = 3       # Loop breaker if AI gets stuck on one spot

# =====================================================================
# PLUGGABLE BRAIN INSTANCE (100% Local Ollama Brain)
# =====================================================================
try:
    active_brain: BaseBrainProvider = LocalOllamaBrain()
except Exception as init_err:
    print(f"[Layer 2 Init Error] Failed to initialize LocalOllamaBrain: {init_err}")
    active_brain = None

# In-memory state buffers
session_history: Dict[str, List[str]] = {}
last_request_timestamp: Dict[str, float] = {}
latest_dashboard_state: Dict = {
    "session_id": "None",
    "device_type": "WAITING",
    "user_goal": "Waiting for first command...",
    "step_number": 0,
    "screen_width": 1080,
    "screen_height": 2400,
    "thought": "System idle (Local Llama ready).",
    "action_type": "WAIT",
    "pixel_x": None,
    "pixel_y": None,
    "screenshot_base64": None,
    "timestamp": "Never"
}

# =====================================================================
# LAYER 4 HOOK: BLACK BOX FLIGHT RECORDER
# =====================================================================
os.makedirs("logs", exist_ok=True)
BLACK_BOX_FILE = "logs/telemetry_failures.jsonl"


def record_black_box_event(reason: str, payload: ObservationPayload | None, decision_info: str):
    """Layer 4 Try-Except: Saves structured failure/kill-switch logs without crashing Layer 2."""
    try:
        os.makedirs("logs", exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "session_id": getattr(payload, "session_id", "unknown"),
            "device_type": getattr(payload, "device_type", "unknown"),
            "user_goal": getattr(payload, "user_goal", "unknown"),
            "step_number": getattr(payload, "step_number", 0),
            "ui_element_count": len(getattr(payload, "ui_elements", []) or []),
            "decision_info": str(decision_info)
        }
        with open(BLACK_BOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as log_err:
        print(f"[Layer 4 Warning] Failed to write Black Box log: {log_err}")


# =====================================================================
# COORDINATE NORMALIZER (0-1000 AI Scale -> Physical Device Pixels)
# =====================================================================
def to_Reference_pixels(norm_val: int | None, max_pixel: int) -> int | None:
    try:
        if norm_val is None:
            return None
        return int(round((float(norm_val) / 1000.0) * int(max_pixel)))
    except Exception:
        return None


def build_safe_error_command(sid: str, thought_msg: str, spoken_msg: str) -> ActionCommand:
    """Strictly adheres to contracts.py ActionCommand (wait_ms=2000 >= 1500)."""
    return ActionCommand(
        session_id=sid,
        status="STOPPED",
        thought=thought_msg,
        action_type="ERROR",
        pixel_x=None,
        pixel_y=None,
        swipe_end_x=None,
        swipe_end_y=None,
        text_to_type=None,
        speak_out_loud=spoken_msg,
        wait_ms=2000
    )


# =====================================================================
# CORE ROUTE: CONTRACT A (POST /api/v1/step)
# =====================================================================
@app.post("/api/v1/step", response_model=ActionCommand)
def process_step(obs: ObservationPayload):
    sid = getattr(obs, "session_id", "unknown_session")
    try:
        now = time.time()

        # 1. KILL-SWITCH CHECK (Immediate Halt)
        if obs.kill_switch_triggered:
            record_black_box_event("USER_KILL_SWITCH", obs, "User aborted execution via physical key.")
            return ActionCommand(
                session_id=sid,
                status="STOPPED",
                thought="Kill-switch pressed by user. Stopping immediately.",
                action_type="DONE",
                speak_out_loud="Stopped.",
                wait_ms=2000
            )

        # 2. THERMAL RATE-LIMIT GUARD (Prevents CPU/Network spam)
        last_time = last_request_timestamp.get(sid, 0.0)
        if (now - last_time) < MIN_COOLDOWN_SECONDS:
            return ActionCommand(
                session_id=sid,
                status="SAFETY_COOLDOWN",
                thought="Thermal guard triggered: Client polling too fast. Enforcing rest.",
                action_type="WAIT",
                speak_out_loud="",
                wait_ms=2000
            )
        last_request_timestamp[sid] = now

        # 3. HARD MAX-STEP GUARD (Prevents infinite runaway battery drain)
        if obs.step_number > MAX_STEPS_PER_GOAL:
            record_black_box_event("MAX_STEPS_EXCEEDED", obs, f"Exceeded {MAX_STEPS_PER_GOAL} steps.")
            return ActionCommand(
                session_id=sid,
                status="STOPPED",
                thought=f"Safety cutoff: Reached max limit of {MAX_STEPS_PER_GOAL} steps.",
                action_type="DONE",
                speak_out_loud="Maximum step limit reached. Stopping for safety.",
                wait_ms=2000
            )

        # Initialize rolling 5-step memory for this session
        if sid not in session_history:
            session_history[sid] = []

        if active_brain is None:
            record_black_box_event("BRAIN_UNINITIALIZED", obs, "active_brain is None")
            return build_safe_error_command(sid, "Layer 3 Brain is not initialized.", "Brain offline.")

        # 4. CALL LAYER 3 BRAIN SAFELY (Isolated inside try/except so Layer 2 never crashes)
        try:
            decision: BrainDecision = active_brain.decide(obs, session_history[sid][-5:])
        except Exception as e:
            record_black_box_event("BRAIN_EXCEPTION", obs, str(e))
            return build_safe_error_command(
                sid,
                f"Brain error isolated safely: {str(e)}",
                "Brain error encountered. Stopping safely."
            )

        # 5. CONVERT NORMALIZED (0-1000) COORDINATES TO DEVICE PIXELS
        px_x = to_Reference_pixels(decision.norm_x, obs.screen_width)
        px_y = to_Reference_pixels(decision.norm_y, obs.screen_height)
        end_x = to_Reference_pixels(decision.norm_end_x, obs.screen_width)
        end_y = to_Reference_pixels(decision.norm_end_y, obs.screen_height)

        # 6. STUCK LOOP DETECTOR (Checks if Brain repeats the exact same action 3x)
        action_signature = f"{decision.action_type}:{px_x}:{px_y}:{decision.text_to_type}"
        session_history[sid].append(action_signature)
        if len(session_history[sid]) >= MAX_IDENTICAL_TAPS:
            recent = session_history[sid][-MAX_IDENTICAL_TAPS:]
            if len(set(recent)) == 1 and decision.action_type not in ["WAIT", "DONE", "ERROR"]:
                record_black_box_event("INFINITE_LOOP_DETECTED", obs, f"Repeated {action_signature} 3 times.")
                return ActionCommand(
                    session_id=sid,
                    status="STOPPED",
                    thought="Loop guard triggered: Repeated identical action 3 times.",
                    action_type="DONE",
                    speak_out_loud="I seem to be stuck in a loop, so I am stopping.",
                    wait_ms=2000
                )

        # Determine status
        if decision.action_type == "ERROR":
            status = "STOPPED"
        elif decision.action_type == "DONE":
            status = "COMPLETED"
        else:
            status = "CONTINUE"

        # Update live dashboard state safely
        try:
            latest_dashboard_state.update({
                "session_id": sid,
                "device_type": obs.device_type,
                "user_goal": obs.user_goal,
                "step_number": obs.step_number,
                "screen_width": obs.screen_width,
                "screen_height": obs.screen_height,
                "thought": decision.thought,
                "action_type": decision.action_type,
                "pixel_x": px_x,
                "pixel_y": px_y,
                "screenshot_base64": None,
                "timestamp": datetime.now().strftime("%H:%M:%S")
            })
        except Exception as dash_err:
            print(f"[Layer 2 Dashboard Warning] {dash_err}")

        return ActionCommand(
            session_id=sid,
            status=status,
            thought=decision.thought,
            action_type=decision.action_type,
            pixel_x=px_x,
            pixel_y=px_y,
            swipe_end_x=end_x,
            swipe_end_y=end_y,
            text_to_type=decision.text_to_type,
            speak_out_loud=decision.speak_out_loud,
            wait_ms=2000
        )

    except Exception as fatal_err:
        tb = traceback.format_exc()
        print(f"[Layer 2 Master Catch]\n{tb}")
        record_black_box_event("LAYER2_MASTER_CATCH", obs, str(fatal_err))
        return build_safe_error_command(sid, f"Layer 2 isolated error: {fatal_err}", "Server error isolated.")


# =====================================================================
# LIVE MISSION CONTROL DASHBOARD (GET /dashboard)
# =====================================================================
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    try:
        s = latest_dashboard_state
        left_pct = round((s["pixel_x"] / s["screen_width"]) * 100, 1) if s["pixel_x"] else 50
        top_pct = round((s["pixel_y"] / s["screen_height"]) * 100, 1) if s["pixel_y"] else 50
        show_dot = "block" if s["pixel_x"] is not None else "none"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Jarvis Mission Control</title>
            <meta http-equiv="refresh" content="2">
            <style>
                body {{ background: #0d1117; color: #e6edf3; font-family: monospace; padding: 20px; display: flex; gap: 30px; }}
                .panel {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; flex: 1; }}
                .screen-box {{ position: relative; width: 270px; height: 540px; background: #21262d; border: 2px solid #58a6ff; border-radius: 16px; margin: auto; overflow: hidden; }}
                .crosshair {{ display: {show_dot}; position: absolute; left: {left_pct}%; top: {top_pct}%; width: 22px; height: 22px; background: rgba(46, 160, 67, 0.85); border: 3px solid #7ee787; border-radius: 50%; transform: translate(-50%, -50%); box-shadow: 0 0 12px #3fb950; }}
                .badge {{ background: #238636; padding: 4px 10px; border-radius: 6px; font-weight: bold; }}
                h2 {{ margin-top: 0; color: #58a6ff; }}
                .meta {{ line-height: 1.9; font-size: 14px; }}
            </style>
        </head>
        <body>
            <div class="panel" style="max-width: 320px; text-align: center;">
                <h2>EYE VIEW ({s['device_type']})</h2>
                <div class="screen-box">
                    <div class="crosshair" title="Target: ({s['pixel_x']}, {s['pixel_y']})"></div>
                </div>
                <p>Resolution: {s['screen_width']} x {s['screen_height']} px</p>
            </div>
            <div class="panel">
                <h2>NERVOUS SYSTEM TELEMETRY (LOCAL LLAMA)</h2>
                <div class="meta">
                    <p><strong>Last Update:</strong> {s['timestamp']} (Auto-refreshes every 2s)</p>
                    <p><strong>Session ID:</strong> {s['session_id']}</p>
                    <p><strong>User Goal:</strong> "{s['user_goal']}"</p>
                    <p><strong>Current Step:</strong> {s['step_number']} / {MAX_STEPS_PER_GOAL}</p>
                    <p><strong>Brain Thought:</strong> {s['thought']}</p>
                    <p><strong>Action Dispatched:</strong> <span class="badge">{s['action_type']}</span></p>
                    <p><strong>Target Device Pixels:</strong> X = {s['pixel_x']}, Y = {s['pixel_y']}</p>
                    <p><strong>Thermal Status:</strong> Cool (Enforced {MIN_COOLDOWN_SECONDS}s minimum step gap)</p>
                </div>
            </div>
        </body>
        </html>
        """
    except Exception as e:
        return f"<html><body><h2>Dashboard Error Isolated: {e}</h2></body></html>"