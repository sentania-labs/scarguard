import re
import sys

with open("services/backup/src/main.py", "r") as f:
    content = f.read()

# 1. Update run_backup_cycle
content = content.replace(
    'out = backup_database(db_name, db_path, compress=compress)',
    'out = backup_database(db_name, db_path, compress=compress, triggered_by=triggered_by)'
)

# 2. Add threading.Lock to serialize backup cycles
if 'backup_lock = threading.Lock()' not in content:
    content = content.replace(
        'def run_backup_cycle(',
        'backup_lock = threading.Lock()\n\ndef run_backup_cycle('
    )
    content = content.replace(
        'def run_backup_cycle(\n    cfg: dict[str, Any],\n    publisher: redis_lib.Redis | None,\n    *,\n    triggered_by: str = "schedule",\n) -> dict[str, Any]:\n    """Backup every database, apply retention, return a status summary."""',
        'def run_backup_cycle(\n    cfg: dict[str, Any],\n    publisher: redis_lib.Redis | None,\n    *,\n    triggered_by: str = "schedule",\n) -> dict[str, Any]:\n    """Backup every database, apply retention, return a status summary."""\n    if not backup_lock.acquire(blocking=False):\n        return {"phase": "skipped", "reason": "already running"}\n    try:'
    )
    # indent the rest of run_backup_cycle
    # Find the end of run_backup_cycle
    match = re.search(r'    return summary\n\n\ndef _publish_status', content, re.MULTILINE)
    
    start_idx = content.find('    compress = _compress(cfg)')
    end_idx = content.find('    return summary\n', start_idx) + len('    return summary\n')
    
    body = content[start_idx:end_idx]
    indented_body = "\n".join("    " + line if line else line for line in body.split("\n"))
    
    content = content[:start_idx] + indented_body + "    finally:\n        backup_lock.release()\n" + content[end_idx:]

with open("services/backup/src/main.py", "w") as f:
    f.write(content)
