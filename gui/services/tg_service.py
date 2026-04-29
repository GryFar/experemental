import queue
from typing import Any, Dict, List, Optional, Tuple

import tg_rent_tracker


class TgService:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Dict[str, Any]]" = tg_rent_tracker.get_event_queue()
        self._records_cache: List[Dict[str, Any]] = []
        self._records_signature: Optional[Tuple[int, int]] = None

    def poll(self, max_items: int = 50) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for _ in range(max_items):
            try:
                events.append(self._queue.get_nowait())
            except Exception:
                break
        return events

    def status(self) -> Dict[str, Any]:
        return tg_rent_tracker.get_status()

    def records(self) -> List[Dict[str, Any]]:
        summary_path = None
        get_path_fn = getattr(tg_rent_tracker, "get_summary_path", None)
        if callable(get_path_fn):
            try:
                summary_path = get_path_fn()
            except Exception:
                pass
        if summary_path is None:
            private_fn = getattr(tg_rent_tracker, "_summary_path", None)
            if callable(private_fn):
                try:
                    summary_path = private_fn()
                except Exception:
                    pass
        current_signature = None
        if summary_path is not None:
            try:
                st = summary_path.stat()
                current_signature = (st.st_mtime_ns, st.st_size)
            except Exception:
                pass
        if current_signature is not None and self._records_signature == current_signature:
            return list(self._records_cache)
        try:
            records = tg_rent_tracker.load_records()
        except Exception:
            records = []
        self._records_cache = list(records)
        self._records_signature = current_signature
        return records
