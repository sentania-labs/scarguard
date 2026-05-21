"""Pydantic models for structured config validation (form-based editor)."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator, model_validator


class ScheduleConfig(BaseModel):
    enabled: bool = False
    arm_time: str = ""
    disarm_time: str = ""
    use_solar: bool = False
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("arm_time", "disarm_time")
    @classmethod
    def valid_time_format(cls, v: str) -> str:
        if not v:
            return v
        parts = v.strip().split(":")
        try:
            h, m = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            raise ValueError(f"Time must be in HH:MM format, got {v!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"Invalid time {v!r}")
        return f"{h:02d}:{m:02d}"


class AuthConfig(BaseModel):
    enabled: bool = True
    session_timeout_hours: int = 24
    max_login_attempts: int = 5
    lockout_duration_minutes: int = 15
    require_api_auth: bool = False
    nonadmin_rearm_minutes: int = 30

    @field_validator("nonadmin_rearm_minutes")
    @classmethod
    def rearm_minutes_range(cls, v: int) -> int:
        if not 0 <= v <= 1440:
            raise ValueError("nonadmin_rearm_minutes must be between 0 and 1440")
        return v

    @field_validator("session_timeout_hours")
    @classmethod
    def session_timeout_range(cls, v: int) -> int:
        if not 1 <= v <= 8760:
            raise ValueError("session_timeout_hours must be between 1 and 8760")
        return v

    @field_validator("max_login_attempts")
    @classmethod
    def max_attempts_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("max_login_attempts must be between 1 and 100")
        return v

    @field_validator("lockout_duration_minutes")
    @classmethod
    def lockout_range(cls, v: int) -> int:
        if not 1 <= v <= 1440:
            raise ValueError("lockout_duration_minutes must be between 1 and 1440")
        return v


class CameraHealthConfig(BaseModel):
    alert_threshold_minutes: int = 10
    debounce_seconds: int = 30

    @field_validator("alert_threshold_minutes")
    @classmethod
    def threshold_range(cls, v: int) -> int:
        if not 1 <= v <= 1440:
            raise ValueError("alert_threshold_minutes must be between 1 and 1440")
        return v

    @field_validator("debounce_seconds")
    @classmethod
    def debounce_range(cls, v: int) -> int:
        if not 5 <= v <= 300:
            raise ValueError("debounce_seconds must be between 5 and 300")
        return v


class BackupConfig(BaseModel):
    max_backups: int = 50
    debounce_seconds: int = 180

    @field_validator("max_backups")
    @classmethod
    def max_backups_range(cls, v: int) -> int:
        if not 5 <= v <= 500:
            raise ValueError("max_backups must be between 5 and 500")
        return v

    @field_validator("debounce_seconds")
    @classmethod
    def backup_debounce_range(cls, v: int) -> int:
        if not 30 <= v <= 600:
            raise ValueError("debounce_seconds must be between 30 and 600")
        return v


class SummaryReportConfig(BaseModel):
    enabled: bool = False
    frequency: Literal["daily", "weekly", "monthly"] = "daily"
    time: str = "07:00"
    channels: list[str] = []

    @field_validator("time")
    @classmethod
    def valid_time_format(cls, v: str) -> str:
        if not v:
            return "07:00"
        parts = v.strip().split(":")
        try:
            h, m = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            raise ValueError(f"Time must be in HH:MM format, got {v!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"Invalid time {v!r}")
        return f"{h:02d}:{m:02d}"


class ConfigApiConfig(BaseModel):
    """Feature flag for the optional config-API service split.

    When ``enabled`` is True the ``config-api`` compose profile should be
    active and Caddy routes config-write POSTs to the dedicated service
    instead of the web monolith.  Default is False (web handles everything).
    """

    enabled: bool = False


class SystemConfig(BaseModel):
    armed: bool = True
    log_level: str = "info"
    timezone: str = "UTC"
    retention_days: int = 90
    stats_interval: int = 5
    visit_timeout_seconds: int = 300
    training_nudge_threshold: int = 100
    schedule: ScheduleConfig = ScheduleConfig()
    auth: AuthConfig = AuthConfig()
    camera_health: CameraHealthConfig = CameraHealthConfig()
    backup: BackupConfig = BackupConfig()
    summary_report: SummaryReportConfig = SummaryReportConfig()
    config_api: ConfigApiConfig = ConfigApiConfig()

    @field_validator("retention_days")
    @classmethod
    def retention_days_range(cls, v: int) -> int:
        if v != 0 and not 1 <= v <= 365:
            raise ValueError("retention_days must be 0 (disabled) or between 1 and 365")
        return v

    @field_validator("visit_timeout_seconds")
    @classmethod
    def visit_timeout_range(cls, v: int) -> int:
        if not 60 <= v <= 3600:
            raise ValueError("visit_timeout_seconds must be between 60 and 3600")
        return v

    @field_validator("training_nudge_threshold")
    @classmethod
    def nudge_threshold_range(cls, v: int) -> int:
        if not 10 <= v <= 10000:
            raise ValueError("training_nudge_threshold must be between 10 and 10000")
        return v

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Unknown timezone: {v!r}")
        return v

    @field_validator("stats_interval")
    @classmethod
    def stats_interval_range(cls, v: int) -> int:
        if not 1 <= v <= 60:
            raise ValueError("stats_interval must be between 1 and 60 seconds")
        return v


class ExclusionZoneConfig(BaseModel):
    points: list[list[float]] = []
    label: str = ""
    enabled: bool = True
    zone_type: Literal["exclude", "include"] = "exclude"

    @model_validator(mode="before")
    @classmethod
    def migrate_rect_to_polygon(cls, values: dict) -> dict:
        """Convert legacy {x, y, w, h} rect format to polygon points."""
        if not isinstance(values, dict):
            return values
        if "points" not in values and all(k in values for k in ("x", "y", "w", "h")):
            x = float(values.pop("x"))
            y = float(values.pop("y"))
            w = float(values.pop("w"))
            h = float(values.pop("h"))
            values["points"] = [
                [x, y],
                [x + w, y],
                [x + w, y + h],
                [x, y + h],
            ]
        return values

    @field_validator("points")
    @classmethod
    def valid_polygon(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) < 3:
            raise ValueError("A polygon requires at least 3 vertices")
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"Point {i} must have exactly 2 coordinates, got {len(pt)}")
            if not (0.0 <= pt[0] <= 1.0 and 0.0 <= pt[1] <= 1.0):
                raise ValueError(
                    f"Point {i} coordinates must be between 0.0 and 1.0, "
                    f"got ({pt[0]}, {pt[1]})"
                )
        return v


class NotificationRuleConfig(BaseModel):
    """Maps a detected class (or "*" wildcard) to notification channel names."""

    class_name: str = "*"
    channels: list[str] = []


class DeterrentRuleConfig(BaseModel):
    """Maps a detected class (or "*" wildcard) to deterrent group names."""

    class_name: str = "*"
    groups: list[str] = []


class CameraConfig(BaseModel):
    name: str
    rtsp_url: str
    enabled: bool = True
    resolution: int = 720
    model_path: str | None = None
    detect_classes: list[str] | None = None
    confidence_threshold: float | None = None
    exclusion_zones: list[ExclusionZoneConfig] = []
    notification_rules: list[NotificationRuleConfig] = []
    deterrent_rules: list[DeterrentRuleConfig] = []

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Camera name must not be empty")
        return v.strip()

    @field_validator("rtsp_url")
    @classmethod
    def rtsp_url_format(cls, v: str) -> str:
        # Allow the redacted placeholder through — the save handler strips
        # it before merging so the existing secret is preserved.
        if v and v != "***REDACTED***" and not v.startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
        return v

    @field_validator("confidence_threshold")
    @classmethod
    def confidence_range(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence_threshold must be between 0.0 and 1.0")
        return v


class DetectionConfig(BaseModel):
    model_path: str = "/models/yolov8n.pt"
    confidence_threshold: float = 0.25
    target_classes: list[str] = []
    cooldown_seconds: int = 30
    frame_skip: int = 2

    @field_validator("confidence_threshold")
    @classmethod
    def conf_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence_threshold must be between 0.0 and 1.0")
        return v


class NotificationsConfig(BaseModel):
    channels: list[dict] = []


class TLSConfig(BaseModel):
    """TLS settings for the Caddy reverse proxy."""

    mode: Literal["off", "auto", "manual"] = "off"
    domain: str = ""
    cert_path: str = "/config/certs/cert.pem"
    key_path: str = "/config/certs/key.pem"


class TuyaCredentialsConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    api_region: str = "us"


class ActuationDeviceConfig(BaseModel):
    name: str
    device_id: str
    type: Literal["sprinkler", "light", "sound", "plug"] = "sprinkler"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Device name must not be empty")
        return v.strip()

    @field_validator("device_id")
    @classmethod
    def device_id_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Device ID must not be empty")
        return v.strip()


class ActuationDefaultsConfig(BaseModel):
    cooldown_seconds: int = 60
    device_count_range: list[int] = [1, 4]
    spray_duration_range: list[float] = [3.0, 8.0]
    inter_device_delay_range: list[float] = [1.0, 5.0]
    pre_delay_range: list[float] = [0.0, 3.0]

    @field_validator("cooldown_seconds")
    @classmethod
    def cooldown_range(cls, v: int) -> int:
        if not 5 <= v <= 3600:
            raise ValueError("cooldown_seconds must be between 5 and 3600")
        return v


class DeterrentGroupConfig(BaseModel):
    """A named subset of registered devices fired as a coordinated sequence.

    ``devices`` is a list of device names from the registry.  A device may
    appear in multiple groups.  Randomization ranges override
    ``deterrent.defaults`` for this group only; set any range to null (omit)
    to inherit.  ``cooldown_seconds`` gates repeat firings of this group
    only — the top-level ``deterrent.defaults.cooldown_seconds`` still
    enforces a global cross-group cooldown.
    """

    name: str
    devices: list[str] = []
    cooldown_seconds: int = 60
    device_count_range: list[int] | None = None
    spray_duration_range: list[float] | None = None
    inter_device_delay_range: list[float] | None = None
    pre_delay_range: list[float] | None = None

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Group name must not be empty")
        return v.strip()

    @field_validator("cooldown_seconds")
    @classmethod
    def cooldown_range(cls, v: int) -> int:
        if not 5 <= v <= 3600:
            raise ValueError("cooldown_seconds must be between 5 and 3600")
        return v


class DeterrentBatteryMonitorConfig(BaseModel):
    enabled: bool = True
    check_interval_hours: int = 24
    alert_threshold_percent: int = 20

    @field_validator("check_interval_hours")
    @classmethod
    def interval_range(cls, v: int) -> int:
        if not 1 <= v <= 168:
            raise ValueError("check_interval_hours must be between 1 and 168")
        return v

    @field_validator("alert_threshold_percent")
    @classmethod
    def threshold_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("alert_threshold_percent must be between 1 and 100")
        return v


class ActuationConfig(BaseModel):
    enabled: bool = False
    tuya: TuyaCredentialsConfig = TuyaCredentialsConfig()
    devices: list[ActuationDeviceConfig] = []
    groups: list[DeterrentGroupConfig] = []
    defaults: ActuationDefaultsConfig = ActuationDefaultsConfig()
    battery_monitor: DeterrentBatteryMonitorConfig = DeterrentBatteryMonitorConfig()


class StructuredConfigPayload(BaseModel):
    """Subset of scarguard.yml written by the structured form editor.

    Only the sections the form knows about.  Other top-level keys (redis,
    webhooks, etc.) are preserved unchanged from the existing config.
    """

    system: SystemConfig = SystemConfig()
    cameras: list[CameraConfig] = []
    detection: DetectionConfig = DetectionConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    tls: TLSConfig = TLSConfig()
    deterrent: ActuationConfig = ActuationConfig()
