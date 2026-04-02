"""Digest report generation — assembles summary data for periodic notifications."""

import logging
from datetime import datetime, timedelta, timezone

import digest_db

logger = logging.getLogger(__name__)

# ── Stoplight thresholds (hardcoded) ─────────────────────────────────────────

_CPU_YELLOW = 80.0
_CPU_RED = 95.0
_GPU_TEMP_YELLOW = 75.0
_GPU_TEMP_RED = 85.0
_CAMERA_OFFLINE_YELLOW = 1  # any offline events = yellow
_CAMERA_OFFLINE_RED = 10  # sustained outages


def _stoplight(cpu: float | None, gpu_temp: float | None, offline_total: int) -> str:
    """Return overall health status: green, yellow, or red."""
    status = "green"

    if cpu is not None:
        if cpu > _CPU_RED:
            return "red"
        if cpu > _CPU_YELLOW:
            status = "yellow"

    if gpu_temp is not None:
        if gpu_temp > _GPU_TEMP_RED:
            return "red"
        if gpu_temp > _GPU_TEMP_YELLOW:
            status = "yellow"

    if offline_total >= _CAMERA_OFFLINE_RED:
        return "red"
    if offline_total >= _CAMERA_OFFLINE_YELLOW:
        status = "yellow"

    return status


def _period_hours(frequency: str) -> int:
    """Reporting window in hours for the given frequency."""
    if frequency == "weekly":
        return 7 * 24
    if frequency == "monthly":
        return 30 * 24
    return 24  # daily


def _format_duration(secs: float) -> str:
    """Format seconds into human-readable duration."""
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    hours = int(secs // 3600)
    mins = int((secs % 3600) // 60)
    return f"{hours}h {mins}m"


def generate(frequency: str) -> dict:
    """Generate a complete digest report.

    Returns a dict with all sections populated, suitable for formatting
    by each notifier type.
    """
    hours = _period_hours(frequency)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # ── Detection summary ────────────────────────────────────────────────
    total = digest_db.total_events(since)
    by_class = digest_db.count_events_by_class(since)
    by_camera = digest_db.count_events_by_camera(since)

    # Previous period comparison
    prev_since = since - timedelta(hours=hours)
    prev_total = digest_db.total_events(prev_since, until=since)
    if prev_total > 0:
        change_pct = round(((total - prev_total) / prev_total) * 100)
    elif total > 0:
        change_pct = 100
    else:
        change_pct = 0

    # ── Visit highlights ─────────────────────────────────────────────────
    visit_count = digest_db.total_visits(since)
    top = digest_db.top_visits(since, limit=3)
    top_formatted = [
        {
            "class": v["class_name"],
            "camera": v["camera_name"],
            "duration": _format_duration(v["duration_secs"]),
            "detections": v["detection_count"],
        }
        for v in top
    ]

    # ── Performance ──────────────────────────────────────────────────────
    metrics = digest_db.avg_metrics(since)
    offline = digest_db.camera_offline_count(since)
    offline_total = sum(offline.values())
    health_status = _stoplight(metrics["cpu_pct"], metrics["gpu_temp"], offline_total)

    # ── Storage ──────────────────────────────────────────────────────────
    storage = digest_db.storage_summary()

    # ── Training data ────────────────────────────────────────────────────
    protected = digest_db.protected_event_count()
    pruneable = digest_db.pruneable_event_count()
    feedback = digest_db.feedback_summary(since)
    fp_count = feedback.get("false_positive", 0)
    labeled_in_period = sum(v for k, v in feedback.items() if k != "unlabeled")
    fp_rate = round((fp_count / labeled_in_period) * 100, 1) if labeled_in_period > 0 else None

    report = {
        "_digest": True,
        "frequency": frequency,
        "period_label": f"Last {'24 hours' if frequency == 'daily' else '7 days' if frequency == 'weekly' else '30 days'}",
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "detections": {
            "total": total,
            "by_class": by_class,
            "by_camera": by_camera,
            "change_pct": change_pct,
        },

        "visits": {
            "total": visit_count,
            "top": top_formatted,
        },

        "performance": {
            "status": health_status,
            "avg_cpu_pct": metrics["cpu_pct"],
            "avg_gpu_pct": metrics["gpu_pct"],
            "avg_gpu_temp": metrics["gpu_temp"],
            "camera_offline_events": offline,
            "camera_offline_total": offline_total,
        },

        "storage": storage,

        "training": {
            "protected_events": protected,
            "pruneable_events": pruneable,
            "fp_rate_pct": fp_rate,
        },
    }

    logger.info(
        "Digest generated: %s — %d events, status=%s",
        frequency, total, health_status,
    )
    return report


def format_plain_text(report: dict) -> str:
    """Render digest as plain text (for ntfy and fallback)."""
    d = report["detections"]
    p = report["performance"]
    s = report["storage"]
    t = report["training"]
    v = report["visits"]

    lines = [
        f"ScarGuard Digest — {report['period_label']}",
        "",
        f"Health: {'OK' if p['status'] == 'green' else 'WARNING' if p['status'] == 'yellow' else 'CRITICAL'}",
        "",
        f"Detections: {d['total']} ({'+' if d['change_pct'] >= 0 else ''}{d['change_pct']}% vs prior period)",
    ]

    if d["by_class"]:
        for cls, cnt in d["by_class"].items():
            lines.append(f"  {cls}: {cnt}")

    lines.append(f"\nVisits: {v['total']}")
    for vt in v["top"]:
        lines.append(f"  {vt['class']} on {vt['camera']} — {vt['duration']} ({vt['detections']} detections)")

    lines.append("\nPerformance:")
    if p["avg_cpu_pct"] is not None:
        lines.append(f"  CPU: {p['avg_cpu_pct']:.0f}%")
    if p["avg_gpu_pct"] is not None:
        lines.append(f"  GPU: {p['avg_gpu_pct']:.0f}%")
    if p["avg_gpu_temp"] is not None:
        lines.append(f"  GPU Temp: {p['avg_gpu_temp']:.0f}C")
    if p["camera_offline_total"] > 0:
        lines.append(f"  Camera offline events: {p['camera_offline_total']}")

    lines.append(f"\nStorage: {s['total_mb']} MB total")
    lines.append(f"  Snapshots: {s['snapshots_mb']} MB | DB: {s['database_mb']} MB | Models: {s['models_mb']} MB")

    lines.append(f"\nTraining: {t['protected_events']} labeled (protected) / {t['pruneable_events']} pruneable")
    if t["fp_rate_pct"] is not None:
        lines.append(f"  False positive rate: {t['fp_rate_pct']}%")

    return "\n".join(lines)


def format_discord_embed(report: dict) -> dict:
    """Render digest as a Discord embed dict."""
    d = report["detections"]
    p = report["performance"]
    s = report["storage"]
    t = report["training"]
    v = report["visits"]

    color = {"green": 0x3ECF8E, "yellow": 0xFFB020, "red": 0xE53E3E}[p["status"]]
    status_emoji = {"green": "OK", "yellow": "WARNING", "red": "CRITICAL"}[p["status"]]

    # Detection breakdown
    class_lines = "\n".join(f"**{cls}**: {cnt}" for cls, cnt in d["by_class"].items()) or "None"
    change = f"{'+' if d['change_pct'] >= 0 else ''}{d['change_pct']}%"

    # Top visits
    visit_lines = "\n".join(
        f"{vt['class']} on {vt['camera']} — {vt['duration']}"
        for vt in v["top"]
    ) or "None"

    # Performance
    perf_parts = []
    if p["avg_cpu_pct"] is not None:
        perf_parts.append(f"CPU: {p['avg_cpu_pct']:.0f}%")
    if p["avg_gpu_pct"] is not None:
        perf_parts.append(f"GPU: {p['avg_gpu_pct']:.0f}%")
    if p["avg_gpu_temp"] is not None:
        perf_parts.append(f"Temp: {p['avg_gpu_temp']:.0f}C")
    perf_text = " | ".join(perf_parts) or "No data"

    fields = [
        {"name": f"Detections ({change} vs prior)", "value": class_lines, "inline": True},
        {"name": f"Visits ({v['total']})", "value": visit_lines, "inline": True},
        {"name": f"Performance ({status_emoji})", "value": perf_text, "inline": False},
        {
            "name": "Storage",
            "value": f"Total: {s['total_mb']} MB (Snap: {s['snapshots_mb']} | DB: {s['database_mb']} | Models: {s['models_mb']})",
            "inline": False,
        },
        {
            "name": "Training Data",
            "value": f"{t['protected_events']} labeled / {t['pruneable_events']} pruneable"
            + (f" | FP rate: {t['fp_rate_pct']}%" if t["fp_rate_pct"] is not None else ""),
            "inline": False,
        },
    ]

    return {
        "embeds": [{
            "title": f"ScarGuard Digest — {report['period_label']}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Generated {report['generated_at'][:19]}Z"},
        }],
    }


def format_email_html(report: dict) -> str:
    """Render digest as an HTML email body."""
    d = report["detections"]
    p = report["performance"]
    s = report["storage"]
    t = report["training"]
    v = report["visits"]

    status_color = {"green": "#3ECF8E", "yellow": "#FFB020", "red": "#E53E3E"}[p["status"]]
    status_label = {"green": "OK", "yellow": "WARNING", "red": "CRITICAL"}[p["status"]]
    change = f"{'+' if d['change_pct'] >= 0 else ''}{d['change_pct']}%"

    class_rows = "".join(
        f"<tr><td>{cls}</td><td style='text-align:right'>{cnt}</td></tr>"
        for cls, cnt in d["by_class"].items()
    ) or "<tr><td colspan='2'>No detections</td></tr>"

    visit_rows = "".join(
        f"<tr><td>{vt['class']}</td><td>{vt['camera']}</td><td>{vt['duration']}</td></tr>"
        for vt in v["top"]
    ) or "<tr><td colspan='3'>No visits</td></tr>"

    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#e0e0e0;background:#1a1a2e;padding:20px;border-radius:8px;">
      <h2 style="margin:0 0 4px 0;">ScarGuard Digest</h2>
      <p style="color:#888;margin:0 0 16px 0;">{report['period_label']}</p>

      <div style="background:{status_color};color:#fff;padding:8px 16px;border-radius:6px;font-weight:bold;margin-bottom:16px;">
        System Health: {status_label}
      </div>

      <h3 style="margin:16px 0 8px 0;">Detections: {d['total']} ({change} vs prior)</h3>
      <table style="width:100%;border-collapse:collapse;">
        <tr style="border-bottom:1px solid #333;"><th style="text-align:left;padding:4px;">Class</th><th style="text-align:right;padding:4px;">Count</th></tr>
        {class_rows}
      </table>

      <h3 style="margin:16px 0 8px 0;">Top Visits ({v['total']} total)</h3>
      <table style="width:100%;border-collapse:collapse;">
        <tr style="border-bottom:1px solid #333;"><th style="text-align:left;padding:4px;">Class</th><th style="text-align:left;padding:4px;">Camera</th><th style="text-align:left;padding:4px;">Duration</th></tr>
        {visit_rows}
      </table>

      <h3 style="margin:16px 0 8px 0;">Performance</h3>
      <p style="margin:4px 0;">
        {'CPU: ' + f"{p['avg_cpu_pct']:.0f}%" if p['avg_cpu_pct'] is not None else ''}
        {'&nbsp;|&nbsp;GPU: ' + f"{p['avg_gpu_pct']:.0f}%" if p['avg_gpu_pct'] is not None else ''}
        {'&nbsp;|&nbsp;Temp: ' + f"{p['avg_gpu_temp']:.0f}&deg;C" if p['avg_gpu_temp'] is not None else ''}
      </p>
      {f"<p style='color:#FFB020;'>Camera offline events: {p['camera_offline_total']}</p>" if p['camera_offline_total'] > 0 else ''}

      <h3 style="margin:16px 0 8px 0;">Storage</h3>
      <p style="margin:4px 0;">{s['total_mb']} MB total — Snapshots: {s['snapshots_mb']} MB | DB: {s['database_mb']} MB | Models: {s['models_mb']} MB</p>

      <h3 style="margin:16px 0 8px 0;">Training Data</h3>
      <p style="margin:4px 0;">{t['protected_events']} labeled (protected) / {t['pruneable_events']} pruneable
      {f" | FP rate: {t['fp_rate_pct']}%" if t['fp_rate_pct'] is not None else ''}</p>

      <hr style="border:none;border-top:1px solid #333;margin:16px 0;">
      <p style="color:#666;font-size:0.8em;margin:0;">Generated {report['generated_at'][:19]}Z</p>
    </div>
    """
