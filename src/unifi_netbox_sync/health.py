from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def write_success_marker(path: str) -> None:
    """Record that a sync run finished with no errors.

    The Docker healthcheck treats this file's mtime as the last clean run.
    The write is atomic so a probe never observes a half-written file, and it
    is only called after ``run()`` returns without recorded errors — a hung
    HTTP call never reaches it, and a run that caught site or client errors
    does not pretend to have succeeded.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    os.replace(tmp, target)
