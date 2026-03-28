from datetime import datetime

from pydantic import BaseModel


class DetectionEvent(BaseModel):
    timestamp: datetime
    class_name: str
    confidence: float
    camera_name: str
    snapshot_path: str | None = None
    bbox: list[int] | None = None
    frame_size: list[int] | None = None
    feedback: str | None = None
    corrected_class: str | None = None


class VisitSession(BaseModel):
    camera_name: str
    class_name: str
    start_time: datetime
    end_time: datetime
    duration_secs: float
    detection_count: int
