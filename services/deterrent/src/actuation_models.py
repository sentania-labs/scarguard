"""Pydantic models for the deterrent service."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config models — parsed from the ``deterrent`` section of scarguard.yml
# ---------------------------------------------------------------------------

class TuyaCredentials(BaseModel):
    api_key: str
    api_secret: str
    api_region: str = "us"


class DeviceConfig(BaseModel):
    name: str
    device_id: str
    type: Literal["sprinkler", "light", "sound", "plug"]
    enabled: bool = True
    dp_code: str | None = None  # override the default DP code for on/off


class ActuationDefaults(BaseModel):
    device_count_range: list[int] = [1, 4]
    spray_duration_range: list[float] = [3.0, 8.0]
    inter_device_delay_range: list[float] = [1.0, 5.0]
    pre_delay_range: list[float] = [0.0, 3.0]
    cooldown_seconds: int = 60


class BatteryMonitorConfig(BaseModel):
    enabled: bool = True
    check_interval_hours: int = 24
    alert_threshold_percent: int = 20


class ActuationConfig(BaseModel):
    enabled: bool = False  # opt-in — disabled by default
    tuya: TuyaCredentials | None = None
    devices: list[DeviceConfig] = []
    defaults: ActuationDefaults = ActuationDefaults()
    battery_monitor: BatteryMonitorConfig = BatteryMonitorConfig()


# ---------------------------------------------------------------------------
# Event models — published to Redis ``scarguard:actuations``
# ---------------------------------------------------------------------------

class DeviceAction(BaseModel):
    device_name: str
    device_id: str
    device_type: str
    duration_sec: float
    delay_before_sec: float
    success: bool = False
    error: str | None = None


class ActuationEvent(BaseModel):
    timestamp: str  # ISO 8601
    trigger_class: str
    trigger_camera: str
    trigger_confidence: float
    pre_delay_sec: float
    actions: list[DeviceAction]
    total_duration_sec: float
