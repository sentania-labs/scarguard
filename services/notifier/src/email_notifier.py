"""Email dispatcher via SMTP (STARTTLS on port 587, or plain/SSL on others)."""

import logging
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from snapshot_utils import annotate_snapshot

logger = logging.getLogger(__name__)


def _to_local(iso_str: str, tz_name: str) -> str:
    """Convert a UTC ISO 8601 string to a formatted local-time string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, TypeError):
        tz = ZoneInfo("UTC")
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


def _build_feedback_links(base_url: str, token: str) -> str:
    """Build HTML feedback buttons for email."""
    url = f"{base_url.rstrip('/')}/feedback/{token}"
    return f"""\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
  <tr>
    <td align="center">
      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:0 4px;">
            <a href="{url}?v=correct"
               style="display:inline-block;padding:8px 16px;background:#3ecf8e;color:#000;
                      text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;">
              Correct</a>
          </td>
          <td style="padding:0 4px;">
            <a href="{url}?v=false_positive"
               style="display:inline-block;padding:8px 16px;background:#e53e3e;color:#fff;
                      text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;">
              False Positive</a>
          </td>
          <td style="padding:0 4px;">
            <a href="{url}?v=wrong_class"
               style="display:inline-block;padding:8px 16px;background:#ffb020;color:#000;
                      text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;">
              Wrong Class</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _build_html_body(
    class_name: str,
    confidence: float,
    camera_name: str,
    timestamp: str,
    has_snapshot: bool,
    base_url: str,
    feedback_token: str,
) -> str:
    """Build an HTML email body for a detection alert."""
    title = class_name.replace("_", " ").title()
    snapshot_html = ""
    if has_snapshot:
        snapshot_html = (
            '<img src="cid:snapshot" alt="Detection snapshot"'
            ' style="width:100%;max-width:640px;border-radius:8px;'
            'border:1px solid #2a2d3e;margin-bottom:16px;" />'
        )

    feedback_html = ""
    if base_url and feedback_token:
        feedback_html = _build_feedback_links(base_url, feedback_token)

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:24px 0;">
  <tr><td align="center">
    <table width="480" cellpadding="0" cellspacing="0" style="background:#1a1d2e;border-radius:12px;padding:24px;border:1px solid #2a2d3e;">
      <tr><td style="color:#e0e0e0;text-align:center;">
        <h2 style="margin:0 0 4px;color:#e0e0e0;font-size:18px;">ScarGuard</h2>
        <p style="margin:0 0 16px;color:#888;font-size:13px;">{title} Detected</p>
        {snapshot_html}
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
          <tr>
            <td style="color:#888;font-size:13px;padding:4px 0;">Camera</td>
            <td style="color:#e0e0e0;font-size:13px;padding:4px 0;text-align:right;"><strong>{camera_name}</strong></td>
          </tr>
          <tr>
            <td style="color:#888;font-size:13px;padding:4px 0;">Confidence</td>
            <td style="color:#e0e0e0;font-size:13px;padding:4px 0;text-align:right;"><strong>{confidence:.0%}</strong></td>
          </tr>
          <tr>
            <td style="color:#888;font-size:13px;padding:4px 0;">Time</td>
            <td style="color:#e0e0e0;font-size:13px;padding:4px 0;text-align:right;"><strong>{timestamp}</strong></td>
          </tr>
        </table>
        {feedback_html}
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


class EmailNotifier:
    def __init__(self, cfg: dict, tz_name: str = "UTC") -> None:
        self._name: str = cfg.get("name", "email")
        self._smtp_host: str = cfg["smtp_host"]
        self._smtp_port: int = int(cfg.get("smtp_port", 587))
        self._smtp_user: str = cfg.get("smtp_user", "")
        self._smtp_pass: str = cfg.get("smtp_pass", "")
        self._to_addresses: list[str] = cfg.get("to_addresses", [])
        self._include_snapshot: bool = cfg.get("include_snapshot", True)
        self._tz_name: str = tz_name

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        if not self._to_addresses:
            logger.warning("Email notifier: no to_addresses configured, skipping")
            return

        if event.get("_digest"):
            self._send_digest(event)
            return

        class_name = event["class_name"]
        confidence = event["confidence"]
        camera_name = event["camera_name"]
        timestamp = _to_local(event["timestamp"], self._tz_name)
        snapshot_path: str | None = event.get("snapshot_path")
        base_url: str = event.get("_base_url", "")
        feedback_token: str = event.get("feedback_token", "")

        subject = f"ScarGuard: {class_name.replace('_', ' ').title()} detected"

        # Plain text fallback
        plain_body = (
            f"A {class_name.replace('_', ' ')} was detected.\n\n"
            f"Camera:     {camera_name}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Time:       {timestamp}\n"
        )
        if base_url and feedback_token:
            fb_url = f"{base_url.rstrip('/')}/feedback/{feedback_token}"
            plain_body += (
                f"\nProvide feedback:\n"
                f"  Correct:        {fb_url}?v=correct\n"
                f"  False Positive: {fb_url}?v=false_positive\n"
                f"  Wrong Class:    {fb_url}?v=wrong_class\n"
            )

        # Get snapshot bytes
        snapshot_bytes: bytes | None = None
        if self._include_snapshot and snapshot_path:
            snapshot_bytes = annotate_snapshot(
                snapshot_path, event.get("bbox"), event.get("frame_size"),
            )

        # Build HTML body
        html_body = _build_html_body(
            class_name, confidence, camera_name, timestamp,
            has_snapshot=bool(snapshot_bytes),
            base_url=base_url,
            feedback_token=feedback_token,
        )

        # Construct MIME: related > alternative + inline image
        msg = MIMEMultipart("related")
        msg["From"] = self._smtp_user
        msg["To"] = ", ".join(self._to_addresses)
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(plain_body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        msg.attach(alt)

        if snapshot_bytes:
            img_part = MIMEBase("image", "jpeg")
            img_part.set_payload(snapshot_bytes)
            encoders.encode_base64(img_part)
            img_part.add_header("Content-ID", "<snapshot>")
            img_part.add_header(
                "Content-Disposition", "inline", filename="snapshot.jpg",
            )
            msg.attach(img_part)

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

    def _send_digest(self, report: dict) -> None:
        """Send a digest report as an HTML email."""
        from digest import format_email_html

        html = format_email_html(report)
        period = report.get("period_label", "Summary")
        subject = f"ScarGuard Digest — {period}"

        msg = MIMEMultipart("alternative")
        msg["From"] = self._smtp_user
        msg["To"] = ", ".join(self._to_addresses)
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))

        self._send_message(msg)
        logger.info("Email digest sent to %s", self._to_addresses)
