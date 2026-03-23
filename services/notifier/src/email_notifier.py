"""Email dispatcher via SMTP (STARTTLS on port 587, or plain/SSL on others)."""

import logging
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, cfg: dict) -> None:
        self._smtp_host: str = cfg["smtp_host"]
        self._smtp_port: int = int(cfg.get("smtp_port", 587))
        self._smtp_user: str = cfg.get("smtp_user", "")
        self._smtp_pass: str = cfg.get("smtp_pass", "")
        self._to_addresses: list[str] = cfg.get("to_addresses", [])
        self._include_snapshot: bool = cfg.get("include_snapshot", True)

    def send(self, event: dict) -> None:
        if not self._to_addresses:
            logger.warning("Email notifier: no to_addresses configured, skipping")
            return

        class_name = event["class_name"]
        confidence = event["confidence"]
        camera_name = event["camera_name"]
        timestamp = event["timestamp"]
        snapshot_path: str | None = event.get("snapshot_path")

        subject = f"ScarGuard: {class_name.replace('_', ' ').title()} detected"
        body = (
            f"A {class_name.replace('_', ' ')} was detected.\n\n"
            f"Camera:     {camera_name}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Time:       {timestamp}\n"
        )

        msg = MIMEMultipart()
        msg["From"] = self._smtp_user
        msg["To"] = ", ".join(self._to_addresses)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if self._include_snapshot and snapshot_path:
            _attach_snapshot(msg, snapshot_path)

        self._send_message(msg)
        logger.info("Email notification sent to %s", self._to_addresses)

    def _send_message(self, msg: MIMEMultipart) -> None:
        # Port 587 → STARTTLS. Any other port (e.g. 465) → plain connection
        # and let the caller configure SSL via a wrapper if needed.
        if self._smtp_port == 587:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if self._smtp_user and self._smtp_pass:
                    server.login(self._smtp_user, self._smtp_pass)
                server.sendmail(self._smtp_user, self._to_addresses, msg.as_string())
        elif self._smtp_port == 465:
            with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=15) as server:
                if self._smtp_user and self._smtp_pass:
                    server.login(self._smtp_user, self._smtp_pass)
                server.sendmail(self._smtp_user, self._to_addresses, msg.as_string())
        else:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as server:
                if self._smtp_user and self._smtp_pass:
                    server.login(self._smtp_user, self._smtp_pass)
                server.sendmail(self._smtp_user, self._to_addresses, msg.as_string())


def _attach_snapshot(msg: MIMEMultipart, path: str) -> None:
    try:
        data = Path(path).read_bytes()
        part = MIMEBase("image", "jpeg")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=Path(path).name,
        )
        msg.attach(part)
    except OSError:
        logger.warning("Snapshot not found or unreadable for email: %s", path)
