import re
import sys

with open("services/backup/src/main.py", "r") as f:
    content = f.read()

# backup_database def
content = content.replace(
    'def backup_database(\n    db_name: str,\n    db_path: Path,\n    *,\n    compress: bool,\n) -> Path | None:',
    'def backup_database(\n    db_name: str,\n    db_path: Path,\n    *,\n    compress: bool,\n    triggered_by: str = "schedule",\n) -> Path | None:'
)

# final_path logic
content = content.replace(
    '    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")\n    suffix = ".db.gz" if compress else ".db"\n    final_path = target_dir / f"{timestamp}{suffix}"',
    '''    import uuid
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix = ".db.gz" if compress else ".db"
    
    name_base = f"{timestamp}-{triggered_by}" if triggered_by != "schedule" else timestamp
    final_path = target_dir / f"{name_base}{suffix}"
    if final_path.exists():
        final_path = target_dir / f"{name_base}-{uuid.uuid4().hex[:6]}{suffix}"'''
)

# quick_check
content = content.replace(
    '            try:\n                src.backup(dst)\n            finally:\n                dst.close()\n        finally:\n            src.close()',
    '''            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # quick_check successful snapshots
        dst_check = sqlite3.connect(tmp)
        try:
            cur = dst_check.execute("PRAGMA quick_check")
            row = cur.fetchone()
            if not row or row[0] != "ok":
                raise ValueError("backup integrity check failed")
        finally:
            dst_check.close()'''
)

# gzip temp file
content = content.replace(
    '            gz_tmp = final_path.with_suffix(final_path.suffix + ".partial")',
    '            gz_tmp = target_dir / f".tmp-{uuid.uuid4().hex}.partial"'
)

# prune_backups
old_prune = '''def prune_backups(db_name: str, daily: int, weekly: int) -> int:
    """Apply retention. Keep the *daily* most recent files plus *weekly*
    additional files spaced ~7 days apart. Returns count of files deleted.

    Sorting by filename works because filenames embed an ISO 8601-ish
    timestamp."""
    target_dir = BACKUP_ROOT / db_name
    if not target_dir.exists():
        return 0

    files = sorted(target_dir.glob("*.db*"), reverse=True)  # newest first
    keep: set[Path] = set()

    # Keep the N most recent as dailies.
    for f in files[:daily]:
        keep.add(f)

    # From the rest, sample one per week-ish based on filename date.
    if weekly > 0 and len(files) > daily:
        seen_weeks: set[str] = set()
        for f in files[daily:]:
            try:
                # Filename starts with YYYY-MM-DD; ISO week-ish bucket.
                date_part = f.name[:10]  # "2026-04-22"
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                week_key = dt.strftime("%G-W%V")
            except (ValueError, IndexError):
                continue
            if week_key in seen_weeks:
                continue
            seen_weeks.add(week_key)
            keep.add(f)
            if len(seen_weeks) >= weekly:
                break

    deleted = 0
    for f in files:
        if f not in keep:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                logger.exception("Could not delete old backup %s", f)
    if deleted:
        logger.info("Pruned %d old backups for %s", deleted, db_name)
    return deleted'''

new_prune = '''def prune_backups(db_name: str, daily: int, weekly: int) -> int:
    target_dir = BACKUP_ROOT / db_name
    if not target_dir.exists():
        return 0

    all_files = sorted(target_dir.glob("*.db*"), reverse=True)
    files = [f for f in all_files if not (f.name.endswith(".partial") or f.name.endswith(".tmp"))]
    
    manual_files = [f for f in files if "-manual" in f.name]
    schedule_files = [f for f in files if "-manual" not in f.name]

    keep: set[Path] = set()

    # Bounded manual-backup retention
    for f in manual_files[:10]:
        keep.add(f)

    for f in schedule_files[:daily]:
        keep.add(f)

    if weekly > 0 and len(schedule_files) > daily:
        seen_weeks: set[str] = set()
        for f in schedule_files[daily:]:
            try:
                date_part = f.name[:10]
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                week_key = dt.strftime("%G-W%V")
            except (ValueError, IndexError):
                continue
            if week_key in seen_weeks:
                continue
            seen_weeks.add(week_key)
            keep.add(f)
            if len(seen_weeks) >= weekly:
                break

    deleted = 0
    for f in files:
        if f not in keep:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                logger.exception("Could not delete old backup %s", f)
    if deleted:
        logger.info("Pruned %d old backups for %s", deleted, db_name)
    return deleted'''

content = content.replace(old_prune, new_prune)

with open("services/backup/src/main.py", "w") as f:
    f.write(content)
