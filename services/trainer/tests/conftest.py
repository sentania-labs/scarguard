import sys
from pathlib import Path

# Repo checkout: services/trainer/tests -> repo root holds shared/.
# Service image: /app/tests -> /app holds shared/ and src/.
HERE = Path(__file__).resolve().parent
ROOT = next(
    (a for a in HERE.parents if (a / "shared" / "training_safety.py").is_file()), HERE.parent
)
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(ROOT / "shared"))
