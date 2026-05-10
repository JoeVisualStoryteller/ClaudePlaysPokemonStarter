import json
from datetime import datetime, timezone
from pathlib import Path


class EventStream:
    """Append-only JSONL event stream for overlays and run inspection."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type, **payload):
        if not self.path:
            return

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
