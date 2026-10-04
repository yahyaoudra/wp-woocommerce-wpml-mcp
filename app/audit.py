from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class AuditLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, *, target: str | int | None = None, dry_run: bool | None = None,
               payload: Any = None, result: Any = None, error: str | None = None) -> None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "target": target,
            "dry_run": dry_run,
            "payload": payload,
            "result": result,
            "error": error,
        }
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
