# test_phase1.py
import time
import requests

SERVER_URL = "http://127.0.0.1:8000/api/v1/step"

def run_test():
    print("=== STARTING PHASE-1 LAYER 2 INDEPENDENCE TEST ===")
    
    # Simulate 3 normal steps on a 1080x2400 phone screen
    for step in range(1, 4):
        payload = {
            "session_id": "test_sess_001",
            "device_type": "ANDROID",
            "screen_width": 1080,
            "screen_height": 2400,
            "user_goal": "Open WhatsApp and search Srijit",
            "step_number": step,
            "ui_elements": [
                {"id": "icon_wa", "text": "WhatsApp", "clickable": True, "bounds": [400, 1700, 680, 1900]}
            ],
            "kill_switch_triggered": False
        }
        
        res = requests.post(SERVER_URL, json=payload).json()
        print(f"\n[Step {step} Response]")
        print(f"  Thought : {res['thought']}")
        print(f"  Action  : {res['action_type']} at Pixels (X={res['pixel_x']}, Y={res['pixel_y']})")
        print(f"  Speak   : '{res['speak_out_loud']}'")
        print(f"  Status  : {res['status']}")
        
        # Respect the 2-second thermal cooldown!
        time.sleep(2.0)

    # Now test the Kill-Switch & Black Box Flight Recorder!
    print("\n=== TESTING HARDWARE KILL-SWITCH & BLACK BOX RECORDER ===")
    kill_payload = {
        "session_id": "test_sess_001",
        "device_type": "ANDROID",
        "screen_width": 1080,
        "screen_height": 2400,
        "user_goal": "Open WhatsApp and search Srijit",
        "step_number": 4,
        "kill_switch_triggered": True
    }
    res = requests.post(SERVER_URL, json=kill_payload).json()
    print(f"  Kill-Switch Status: {res['status']} | Speak: '{res['speak_out_loud']}'")
    print("  Check 'logs/telemetry_failures.jsonl' -> Your Black Box recorded the event!")

if __name__ == "__main__":
    run_test()