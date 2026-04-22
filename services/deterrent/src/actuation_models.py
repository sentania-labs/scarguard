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


class DeterrentGroup(BaseModel):
    """A named subset of registered devices fired as a coordinated sequence.

    A group references devices from the top-level ``deterrent.devices``
    registry by ``name``.  A device may appear in multiple groups.  Any of
    the *_range fields can be ``None`` (omitted) to inherit from
    ``deterrent.defaults``.  ``cooldown_seconds`` is per-group — the global
    ``deterrent.defaults.cooldown_seconds`` still gates cross-group repeats.
    """

    name: str
    devices: list[str] = []
    cooldown_seconds: int = 60
    device_count_range: list[int] | None = None
    spray_duration_range: list[float] | None = None
    inter_device_delay_range: list[float] | None = None
    pre_delay_range: list[float] | None = None

    def effective_defaults(self, fallback: ActuationDefaults) -> ActuationDefaults:
        """Return an ActuationDefaults where any group-level None inherits *fallback*."""
        return ActuationDefaults(
            device_count_range=self.device_count_range or fallback.device_count_range,
            spray_duration_range=self.spray_duration_range or fallback.spray_duration_range,
            inter_device_delay_range=self.inter_device_delay_range or fallback.inter_device_delay_range,
            pre_delay_range=self.pre_delay_range or fallback.pre_delay_range,
            cooldown_seconds=self.cooldown_seconds,
        )


class BatteryMonitorConfig(BaseModel):
    enabled: bool = True
    check_interval_hours: int = 24
    alert_threshold_percent: int = 20


class ActuationConfig(BaseModel):
    enabled: bool = False  # opt-in — disabled by default
    tuya: TuyaCredentials | None = None
    devices: list[DeviceConfig] = []
    groups: list[DeterrentGroup] = []
    defaults: ActuationDefaults = ActuationDefaults()
    battery_monitor: BatteryMonitorConfig = BatteryMonitorConfig()
    # v1.14: periodic reconciliation polls device status and force-OFFs any
    # device that reports ON while not being actively driven. Catches stuck
    # states surviving a deterrent-service restart or a cloud ack loss.
    # Set 0 to disable.
    reconcile_interval_sec: int = 30


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
    # v0.13.3 latency instrumentation — time from sending the ON command to
    # Tuya Cloud to receiving a success response.  None if the ON call
    # raised before returning.
    cloud_ack_ms: float | None = None
    # v1.14 OFF reliability instrumentation. ``off_attempts`` is total OFF
    # cloud calls (1 = first-try success, >1 = retries). ``stuck`` is True
    # iff ON succeeded but every OFF attempt failed — device may be
    # physically still-on.
    off_attempts: int = 1
    stuck: bool = False


class ActuationEvent(BaseModel):
    timestamp: str  # ISO 8601
    trigger_class: str
    trigger_camera: str
    trigger_confidence: float
    # v0.13.3: which deterrent group fired this event.  Empty string for
    # legacy events or test-fires.
    group_name: str = ""
    pre_delay_sec: float
    actions: list[DeviceAction]
    total_duration_sec: float
    # v0.13.3 latency instrumentation.
    trigger_delay_ms: float | None = None  # detection timestamp → dequeue
    queue_depth: int | None = None         # queue size at dequeue moment
    # v1.14: trace ID for correlating actuation across logs, audit DB,
    # and stuck-event Redis channel.
    request_id: str = ""
    # v1.14: discriminates detection-driven vs test-fire vs force-off.
    event_type: str = "detection"
