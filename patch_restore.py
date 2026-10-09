import sys

with open("services/backup/src/restore.py", "r") as f:
    content = f.read()

content = content.replace(
    '    target_existed = target.exists()',
    '    if pre_db.exists(): pre_db.unlink()\n    if pre_wal.exists(): pre_wal.unlink()\n    if pre_shm.exists(): pre_shm.unlink()\n\n    target_existed = target.exists()'
)

with open("services/backup/src/restore.py", "w") as f:
    f.write(content)
