from datetime import datetime

from pydantic import BaseModel


class DetectionEvent(BaseModel):
    timestamp: datetime
    class_name: str
    confidence: float
    camera_name: str
    snapshot_path: str | None = None
