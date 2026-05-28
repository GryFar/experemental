import asyncio
import importlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
import threading
import queue
import time
import shutil
import getpass
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tg_rent_tracker")
logger.addHandler(logging.NullHandler())

_ENABLED_ENV = "TG_TRACKER_ENABLED"
_SUMMARY_ENV = "TG_SUMMARY_PATH"
_SUMMARY_NAME = "rentals_summary.json"
_APP_NAME = "WiwangAutomation"
_USER_NAME = getpass.getuser()
_SUMMARY_PATH_CACHE: Optional[Path] = None
_SUMMARY_LOGGED = False


def _user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / _APP_NAME / _USER_NAME
    return Path.home() / ".config" / _APP_NAME / _USER_NAME


def _summary_path() -> Path:
    global _SUMMARY_PATH_CACHE, _SUMMARY_LOGGED

    if _SUMMARY_PATH_CACHE is not None:
        return _SUMMARY_PATH_CACHE

    env_path = os.getenv(_SUMMARY_ENV)
    if env_path:
        _SUMMARY_PATH_CACHE = Path(env_path)
        return _SUMMARY_PATH_CACHE

    session_env = os.getenv("TG_SESSION_PATH")
    if session_env:
        session_path = Path(session_env)
        base_dir = session_path if session_path.is_dir() else session_path.parent
    else:
        base_dir = _user_data_dir()

    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    summary_path = base_dir / _SUMMARY_NAME
    _SUMMARY_PATH_CACHE = summary_path
    if not _SUMMARY_LOGGED:
        _SUMMARY_LOGGED = True
        _log_info(f"tg_rent_tracker summary path defaulted to {summary_path}")
    return summary_path
_MAX_RECORDS = 1000

_RENT_OUT_MARKERS = ("RENT_OUT", "RENT OUT", "СДАН", "АРЕНДУ")
_RETURN_MARKERS = ("RENT_RETURN", "RENT RETURN", "RETURN", "ВЕРНУЛ", "ВОЗВРАТ", "ОТМЕН")

_PLATE_RE = re.compile(r"\bplate\s*[:=]\s*(?P<plate>\S+)", re.I)
_PLATE_RU_RE = re.compile(r"Номер\s+транспорта\s*[:=]\s*(?P<plate>\S+)", re.I)
_HOURS_RE = re.compile(r"\bhours?\s*[:=]\s*(?P<hours>\d+)", re.I)
_HOURS_RU_RE = re.compile(r"Длительность\s*[:=]\s*(?P<hours>\d+)", re.I)
_PRICE_PER_HOUR_RE = re.compile(
    r"(?P<price>\d[\d\s]*)\s*(?:/\s*h|per\s*hour|в\s*час)",
    re.I,
)
_PRICE_PER_HOUR_KV_RE = re.compile(
    r"(?:price_per_hour|price\s*per\s*hour|цена\s*за\s*час)\s*[:=]\s*(?P<price>\d[\d\s]*)",
    re.I,
)

_STATE_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_DISABLED = False
_STATUS: Dict[str, Any] = {"enabled": False, "state": "stopped", "last_event_ts": None, "last_error": ""}
_EVENT_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_SESSION_LOCK_RETRIES = 6
_SESSION_LOCK_SLEEP_S = 1.0
_TRACKER_SESSION_ENV = "TG_TRACKER_SESSION_PATH"
_TRACKER_SESSION_SUFFIX = ".tg_tracker"


def _log_info(message: str) -> None:
    try:
        logger.info(message)
    except Exception:
        pass


def _log_error(message: str) -> None:
    try:
        logger.error(message)
    except Exception:
        pass


def _is_enabled() -> bool:
    return os.getenv(_ENABLED_ENV) == "1"


def _load_summary(path: Path) -> List[Dict[str, Any]]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "[]")
            if isinstance(data, list):
                return data
    except Exception as exc:
        _log_error(f"tg_rent_tracker load failed: {exc}")
    return []


def _save_summary(path: Path, data: List[Dict[str, Any]]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _log_error(f"tg_rent_tracker save failed: {exc}")


def _prune_summary(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(records) <= _MAX_RECORDS:
        return records
    return records[-_MAX_RECORDS:]


def _parse_price(value: str) -> Optional[int]:
    try:
        cleaned = value.replace(" ", "").replace(",", "")
        return int(cleaned)
    except Exception:
        return None


def _extract_plate(text: str) -> Optional[str]:
    for regex in (_PLATE_RE, _PLATE_RU_RE):
        match = regex.search(text)
        if match:
            plate = match.group("plate").strip()
            if plate:
                return plate
    return None


def _extract_hours(text: str) -> Optional[int]:
    for regex in (_HOURS_RE, _HOURS_RU_RE):
        match = regex.search(text)
        if match:
            try:
                return int(match.group("hours"))
            except Exception:
                return None
    return None


def _extract_price_per_hour(text: str) -> Optional[int]:
    for regex in (_PRICE_PER_HOUR_KV_RE, _PRICE_PER_HOUR_RE):
        match = regex.search(text)
        if match:
            return _parse_price(match.group("price"))
    return None


def _is_rent_out(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in _RENT_OUT_MARKERS)


def _is_return_or_cancel(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in _RETURN_MARKERS)


def _append_record(record: Dict[str, Any]) -> None:
    summary = _load_summary(_summary_path())
    summary.append(record)
    summary = _prune_summary(summary)
    _save_summary(_summary_path(), summary)
    try:
        with _STATE_LOCK:
            _STATUS["last_event_ts"] = record.get("timestamp")
    except Exception:
        pass
    try:
        _EVENT_QUEUE.put_nowait(record)
    except Exception:
        pass


def _detect_source_session_path() -> Optional[Path]:
    env_path = os.getenv("TG_SESSION_PATH")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path
        _log_error("tg_rent_tracker session path missing")
        return None

    sessions = list(Path(".").glob("*.session"))
    if len(sessions) == 1:
        return sessions[0]
    if len(sessions) > 1:
        _log_error("tg_rent_tracker multiple .session files found; specify TG_SESSION_PATH")
        return None
    _log_info("tg_rent_tracker no .session file found")
    return None


def _derive_tracker_session_path(source: Path) -> Path:
    env_path = os.getenv(_TRACKER_SESSION_ENV)
    if env_path:
        return Path(env_path)
    if source.suffix == ".session":
        return source.with_name(source.stem + _TRACKER_SESSION_SUFFIX + source.suffix)
    return source.with_name(source.name + _TRACKER_SESSION_SUFFIX)


def _copy_session_file(source: Path, target: Path) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                if source.stat().st_mtime <= target.stat().st_mtime:
                    return True
            except Exception:
                pass
        shutil.copy2(str(source), str(target))
        return True
    except Exception as exc:
        _log_error(f"tg_rent_tracker session copy failed: {exc}")
        return False


def _prepare_tracker_session() -> Optional[str]:
    source = _detect_source_session_path()
    if source is None:
        return None
    tracker = _derive_tracker_session_path(source)
    if tracker == source:
        _log_error("tg_rent_tracker tracker session path equals source; set TG_TRACKER_SESSION_PATH")
        return None
    if tracker.exists():
        return str(tracker)
    if not _copy_session_file(source, tracker):
        return None
    return str(tracker)


def _get_telethon() -> Optional[Any]:
    spec = importlib.util.find_spec("telethon")
    if spec is None:
        _log_error("tg_rent_tracker telethon not installed")
        return None
    return importlib.import_module("telethon")


def get_status() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATUS)


def get_event_queue() -> "queue.Queue[Dict[str, Any]]":
    return _EVENT_QUEUE


def load_records() -> List[Dict[str, Any]]:
    return _load_summary(_summary_path())


def income_summary(now_ts: Optional[float] = None) -> Dict[str, float]:
    if now_ts is None:
        now_ts = __import__("time").time()
    day_sec = 24 * 3600
    week_sec = 7 * day_sec
    total_day = 0.0
    total_week = 0.0
    for rec in load_records():
        try:
            ts = rec.get("timestamp")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts)
            age = now_ts - dt.timestamp()
            total_sum = float(rec.get("total_sum") or 0)
            if age <= day_sec:
                total_day += total_sum
            if age <= week_sec:
                total_week += total_sum
        except Exception:
            continue
    return {"today": total_day, "week": total_week}


def active_rentals(now_ts: Optional[float] = None) -> Dict[str, Any]:
    if now_ts is None:
        now_ts = __import__("time").time()
    active = []
    for rec in load_records():
        try:
            ts = rec.get("timestamp")
            hours = float(rec.get("hours") or 0)
            plate = str(rec.get("plate") or "").strip()
            if not ts or not hours:
                continue
            start = datetime.fromisoformat(ts).timestamp()
            end = start + hours * 3600.0
            if now_ts < end:
                active.append({"plate": plate, "end_ts": end})
        except Exception:
            continue
    next_end = min([a["end_ts"] for a in active], default=None)
    return {"count": len(active), "next_end_ts": next_end, "active": active}


def vehicle_stats(plate: Optional[str], now_ts: Optional[float] = None) -> Dict[str, Any]:
    if now_ts is None:
        now_ts = __import__("time").time()
    day_sec = 24 * 3600
    week_sec = 7 * day_sec
    hours_day = 0.0
    hours_week = 0.0
    last_end = None
    active_for_vehicle = 0
    for rec in load_records():
        try:
            rec_plate = str(rec.get("plate") or "").strip()
            if plate and rec_plate != plate:
                continue
            ts = rec.get("timestamp")
            hours = float(rec.get("hours") or 0)
            if not ts or not hours:
                continue
            start = datetime.fromisoformat(ts).timestamp()
            end = start + hours * 3600.0
            age = now_ts - start
            if age <= day_sec:
                hours_day += hours
            if age <= week_sec:
                hours_week += hours
            if now_ts < end:
                active_for_vehicle += 1
            if last_end is None or end > last_end:
                last_end = end
        except Exception:
            continue
    return {
        "hours_today": hours_day,
        "hours_week": hours_week,
        "last_end_ts": last_end,
        "active_for_vehicle": active_for_vehicle,
    }


async def _listen_loop(api_id: int, api_hash: str, session_path: str) -> None:
    try:
        telethon = _get_telethon()
        if telethon is None:
            return
        TelegramClient = telethon.TelegramClient
        events = telethon.events
    except Exception as exc:
        _log_error(f"tg_rent_tracker telethon missing components: {exc}")
        return

    client = TelegramClient(session_path, api_id, api_hash)
    for attempt in range(_SESSION_LOCK_RETRIES):
        try:
            await client.start()
            break
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                _log_error(f"tg_rent_tracker client start failed: {exc}")
                return
            wait_s = _SESSION_LOCK_SLEEP_S * (attempt + 1)
            _log_error(f"tg_rent_tracker session locked; retry in {wait_s:.1f}s")
            await asyncio.sleep(wait_s)
        except Exception as exc:
            _log_error(f"tg_rent_tracker client start failed: {exc}")
            return
    else:
        _log_error("tg_rent_tracker session locked; retries exceeded")
        return

    @client.on(events.NewMessage())
    async def _handler(event):
        try:
            text = event.raw_text or ""
            if _is_return_or_cancel(text):
                return
            if not _is_rent_out(text):
                return

            plate = _extract_plate(text)
            hours = _extract_hours(text)
            price_per_hour = _extract_price_per_hour(text)

            if not plate or hours is None or price_per_hour is None:
                _log_info("tg_rent_tracker rent_out skipped (missing fields)")
                return

            total_sum = int(hours) * int(price_per_hour)
            record = {
                "plate": plate,
                "hours": int(hours),
                "price_per_hour": int(price_per_hour),
                "total_sum": total_sum,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "source": "TG",
                "vehicle_key": "",
            }
            _append_record(record)
        except Exception as exc:
            _log_error(f"tg_rent_tracker handler error: {exc}")

    try:
        await client.run_until_disconnected()
    except Exception as exc:
        _log_error(f"tg_rent_tracker run error: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def start(enabled: Optional[bool] = None) -> None:
    """
    Start the passive Telegram rent tracker.

    Enabled via TG_TRACKER_ENABLED=1. Uses an existing Telethon session file.
    On any error, logs and self-disables without raising exceptions.
    """
    global _THREAD, _DISABLED

    with _STATE_LOCK:
        if _DISABLED and enabled:
            _DISABLED = False
            _STATUS["last_error"] = ""
            _STATUS["state"] = "stopped"
        if _THREAD is not None or _DISABLED:
            return

    if enabled is None:
        enabled = _is_enabled()
    if not enabled:
        _log_info("tg_rent_tracker disabled")
        with _STATE_LOCK:
            _STATUS["enabled"] = False
            _STATUS["state"] = "disabled"
        return

    try:
        with _STATE_LOCK:
            _STATUS["enabled"] = True
            _STATUS["state"] = "starting"
        api_id_raw = os.getenv("TG_API_ID", "").strip()
        api_hash = os.getenv("TG_API_HASH", "").strip()
        api_id = int(api_id_raw or "0")
        if api_id <= 0 or not api_hash:
            _log_error("tg_rent_tracker missing TG_API_ID/TG_API_HASH")
            with _STATE_LOCK:
                _DISABLED = True
                _STATUS["state"] = "error"
                _STATUS["last_error"] = "missing api credentials"
            return

        session_path = _prepare_tracker_session()
        if not session_path:
            with _STATE_LOCK:
                _DISABLED = True
                _STATUS["state"] = "error"
                _STATUS["last_error"] = "missing session"
            return

        def _runner() -> None:
            global _DISABLED
            attempt = 0
            while True:
                try:
                    session_path = _prepare_tracker_session()
                    if not session_path:
                        raise RuntimeError("tracker session unavailable")
                    asyncio.run(_listen_loop(api_id, api_hash, session_path))
                    return
                except sqlite3.OperationalError as exc:
                    if "database is locked" not in str(exc).lower():
                        _log_error(f"tg_rent_tracker crash: {exc}")
                        break
                    attempt += 1
                    wait_s = _SESSION_LOCK_SLEEP_S * min(attempt, _SESSION_LOCK_RETRIES)
                    _log_error(f"tg_rent_tracker session locked; retry in {wait_s:.1f}s")
                    with _STATE_LOCK:
                        _STATUS["state"] = "retrying"
                        _STATUS["last_error"] = "database is locked"
                    time.sleep(wait_s)
                    continue
                except Exception as exc:
                    _log_error(f"tg_rent_tracker crash: {exc}")
                    break
            with _STATE_LOCK:
                _DISABLED = True
                _STATUS["state"] = "error"
                _STATUS["last_error"] = "tracker stopped"

        thread = threading.Thread(target=_runner, name="tg_rent_tracker", daemon=True)
        with _STATE_LOCK:
            _THREAD = thread
            _STATUS["state"] = "running"
        thread.start()
    except Exception as exc:
        _log_error(f"tg_rent_tracker start error: {exc}")
        with _STATE_LOCK:
            _DISABLED = True
            _STATUS["state"] = "error"
            _STATUS["last_error"] = str(exc)
