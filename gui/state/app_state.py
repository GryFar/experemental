import hashlib
import json
import threading
import time
from typing import Any, Dict, List


def _event_id(event: Dict[str, Any]) -> str:
    eid = event.get("id") or event.get("event_id")
    if eid:
        return str(eid)
    key = json.dumps({k: event.get(k) for k in ("vehicle_key", "plate", "timestamp", "status", "total_sum")}, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._seen_ids: set = set()
        self._metrics: Dict[str, Any] = {}
        self._ui_prefs: Dict[str, Any] = {}
        self._errors: List[Dict[str, Any]] = []

    def add_events(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            for ev in events:
                eid = _event_id(ev)
                if eid not in self._seen_ids:
                    self._seen_ids.add(eid)
                    self._events.append(ev)
            if len(self._events) > 500:
                removed = self._events[:-500]
                self._events = self._events[-500:]
                self._seen_ids -= {_event_id(e) for e in removed}

    def set_metrics(self, metrics: Dict[str, Any]) -> None:
        with self._lock:
            self._metrics = dict(metrics or {})

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"events": list(self._events), "metrics": dict(self._metrics), "ui_prefs": dict(self._ui_prefs), "errors": list(self._errors)}

    def set_ui_prefs(self, prefs: Dict[str, Any]) -> None:
        with self._lock:
            self._ui_prefs = dict(prefs or {})

    def add_error(self, error: Dict[str, Any]) -> None:
        with self._lock:
            normalized = dict(error)
            if "timestamp" not in normalized:
                normalized["timestamp"] = time.time()
            self._errors.append(normalized)
            self._errors = self._errors[-200:]
