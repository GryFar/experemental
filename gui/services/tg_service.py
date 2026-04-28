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
        summary_path = tg_rent_tracker._summary_path()
        current_signature: Optional[Tuple[int, int]]
        try:
            stat_result = summary_path.stat()
            current_signature = (stat_result.st_mtime_ns, stat_result.st_size)
        except Exception:
            current_signature = None

        if self._records_signature == current_signature:
            return list(self._records_cache)

        records = tg_rent_tracker.load_records()
        self._records_cache = list(records)
        self._records_signature = current_signature
        return records
