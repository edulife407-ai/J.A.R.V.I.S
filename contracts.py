# contracts.py
from typing import List, Optional, Literal
from pydantic import BaseModel, Field

# =====================================================================
# FIREWALL A1: Input Contract (Layer 1 Body -> Layer 2 Nervous System)
# =====================================================================
class UIElement(BaseModel):
    id: str = Field(default="", description="Resource ID or tag")
    text: str = Field(default="", description="Visible text or content description")
    clickable: bool = Field(default=True)
    bounds: List[int] = Field(..., min_length=4, max_length=4, description="[x1, y1, x2, y2] in device pixels")

class ObservationPayload(BaseModel):
    session_id: str = Field(..., min_length=1)
    device_type: Literal["ANDROID", "LAPTOP"]
    screen_width: int = Field(..., gt=100, le=7680)
    screen_height: int = Field(..., gt=100, le=4320)
    user_goal: str = Field(..., min_length=1)
    step_number: int = Field(..., ge=1)
    screenshot_base64: Optional[str] = Field(default=None, description="Compressed JPEG Base64 string")
    ui_elements: List[UIElement] = Field(default_factory=list)
    kill_switch_triggered: bool = Field(default=False)

# =====================================================================
# FIREWALL B: Brain Contract (Layer 2 <-> Layer 3 Brain Adapter)
# Note: The Brain ONLY ever sees a universal 0 to 1000 coordinate grid!
# =====================================================================
class BrainDecision(BaseModel):
    thought: str = Field(..., description="Short 1-line reasoning for the HUD")
    action_type: Literal["TAP", "SWIPE", "TYPE", "HOME", "BACK", "WAIT", "DONE", "ERROR"]
    norm_x: Optional[int] = Field(default=None, ge=0, le=1000)
    norm_y: Optional[int] = Field(default=None, ge=0, le=1000)
    norm_end_x: Optional[int] = Field(default=None, ge=0, le=1000)
    norm_end_y: Optional[int] = Field(default=None, ge=0, le=1000)
    text_to_type: Optional[str] = Field(default=None)
    speak_out_loud: str = Field(default="")

# =====================================================================
# FIREWALL A2: Output Contract (Layer 2 -> Layer 1 Body)
# Coordinates here are already converted to exact device pixels!
# =====================================================================
class ActionCommand(BaseModel):
    session_id: str
    status: Literal["CONTINUE", "STOPPED", "COMPLETED", "SAFETY_COOLDOWN"]
    thought: str
    action_type: Literal["TAP", "SWIPE", "TYPE", "HOME", "BACK", "WAIT", "DONE", "ERROR"]
    pixel_x: Optional[int] = None
    pixel_y: Optional[int] = None
    swipe_end_x: Optional[int] = None
    swipe_end_y: Optional[int] = None
    text_to_type: Optional[str] = None
    speak_out_loud: str = ""
    wait_ms: int = Field(default=2000, ge=1500, description="Enforced minimum 1.5s hardware rest")