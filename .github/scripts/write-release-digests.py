#!/usr/bin/env python3
"""Write the release-digests.json artifact."""
import json
import os

digests = {
    "tag": os.environ["TAG"],
    "commit": os.environ["COMMIT"],
    "images": {
        "web": os.environ.get("WEB", ""),
        "notifier": os.environ.get("NOTIFIER", ""),
        "deterrent": os.environ.get("DETERRENT", ""),
        "backup": os.environ.get("BACKUP", ""),
        "caddy": os.environ.get("CADDY", ""),
        "log-streamer": os.environ.get("LOG_STREAMER", ""),
        "training-controller": os.environ.get("TRAINING_CONTROLLER", ""),
        "off-watchdog": os.environ.get("OFF_WATCHDOG", ""),
        "detector": os.environ.get("DETECTOR", ""),
        "detector-x86": os.environ.get("DETECTOR_X86", ""),
        "trainer": os.environ.get("TRAINER", ""),
    }
}

with open("release-digests.json", "w") as f:
    json.dump(digests, f, indent=2)

print(json.dumps(digests, indent=2))
