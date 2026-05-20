"""Safe JSON serialisation for Jinja ``<script type="application/json">`` blocks.

``json.dumps`` does not escape ``</`` by default.  A value containing the
literal sequence ``</script>`` will close the enclosing script block and
allow HTML/JS injection.  ``safe_json_dumps`` escapes ``</`` → ``<\\/``
which is semantically identical in JSON but prevents the breakout.
"""

from __future__ import annotations

import json
from typing import Any


def safe_json_dumps(obj: Any, **kwargs: Any) -> str:
    return json.dumps(obj, **kwargs).replace("</", "<\\/")
