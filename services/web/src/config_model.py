"""Pydantic models for structured config validation (form-based editor)."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator


class SystemConfig(BaseModel):
    armed: bool = True
    log_level: str = "info"
    timezone: str = "UTC"
    snapshot_retention_days: int = 30

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Unknown timezone: {v!r}")
        return v


class CameraConfig(BaseModel):
    name: str
    rtsp_url: str
    enabled: bool = True
    resolution: int = 720

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Camera name must not be empty")
        return v.strip()

    @field_validator("rtsp_url")
    @classmethod
    def rtsp_url_format(cls, v: str) -> str:
        if v and not v.startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
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


class DiscordConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    mention_role: str = ""
    include_snapshot: bool = True


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    to_addresses: list[str] = []
    include_snapshot: bool = True

    @field_validator("smtp_port")
    @classmethod
    def port_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        return v


class NotificationsConfig(BaseModel):
    discord: DiscordConfig = DiscordConfig()
    email: EmailConfig = EmailConfig()


class StructuredConfigPayload(BaseModel):
    """Subset of scarguard.yml written by the structured form editor.

    Only the sections the form knows about.  Other top-level keys (redis,
    action_rules, webhooks, etc.) are preserved unchanged from the existing config.
    """

    system: SystemConfig = SystemConfig()
    cameras: list[CameraConfig] = []
    detection: DetectionConfig = DetectionConfig()
    notifications: NotificationsConfig = NotificationsConfig()
