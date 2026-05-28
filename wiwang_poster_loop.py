# wiwang_poster_loop.py
# Main GUI + poster automation
# Key upgrades:
# - LIVE sliders for all timing-related settings (no restart)
# - Autosave with debounce to config.json
# - True infinite LOOP mode: NOT_FOUND items are rechecked forever (LoopManager)
# - Dedupe-by-photo-hash is OFF by default in loop_mode (so relist works)
# - Dark theme + live value labels
#
# Fixes (2025-12-20):
# - Verified paste: Ctrl+V is NOT assumed to work; we verify via Ctrl+A/Ctrl+C (when possible)
# - Retry focus + paste before falling back to typing
# - Scrollable "Tuning" tab (mousewheel + scrollbar)
#
# Hotkeys:
#   F12 -> Pause/Resume
#   F9  -> STOP (hard)
#
from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import getpass
import hashlib
import inspect
import json
import logging
import os
import shutil
import random
import re
import sqlite3
import sys
import threading
import time
import asyncio
import statistics
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import deque
import config_compat
import plate_reader
import plate_registry
import rental_limiter
import tg_rent_tracker
from gui.app_gui import AppGUI

# ── Item Sale Monitor (вкладка в Admin/App) ─────────────────────────────────
try:
    from item_sale_monitor import ItemSaleMonitor, TrackedItem
    from gui.views.item_sale_tab import ItemSaleTab
    _ITEM_MONITOR_AVAILABLE = True
except ImportError:
    _ITEM_MONITOR_AVAILABLE = False

# --- Optional Telegram (Telethon) ---
try:
    from telethon import TelegramClient, events
except Exception:
    TelegramClient = None
    events = None

# -------- optional deps --------
missing: List[str] = []

try:
    import pyautogui
except Exception:
    pyautogui = None
    missing.append("pyautogui")

try:
    import keyboard
except Exception:
    keyboard = None
    missing.append("keyboard")

try:
    import pyperclip
except Exception:
    pyperclip = None
    missing.append("pyperclip")

try:
    from pynput.keyboard import Controller, Key, Listener
except Exception:
    Controller = None
    Key = None
    Listener = None
    missing.append("pynput")

# OpenCV optional (for confidence in locateOnScreen)
cv2 = None
HAS_OPENCV = False
try:
    import cv2  # type: ignore
    HAS_OPENCV = True
except Exception:
    cv2 = None
    HAS_OPENCV = False

# numpy optional (for fast scan)
try:
    import numpy as np  # type: ignore
except Exception:
    np = None

# OCR optional (plate text validation)
try:
    import pytesseract  # type: ignore
except Exception:
    pytesseract = None


# -------- GUI --------
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

# Pillow (for region drawer screenshot preview)
try:
    from PIL import ImageTk  # type: ignore
except Exception:
    ImageTk = None


# ------------------ Safe popup charts helper ------------------
def open_charts_popup(app):
    """
    Safe handler for the 'Charts' button.
    Avoids AttributeError if app.open_charts is missing due to indentation/migration issues.
    """
    # If the app already provides a method, use it.
    fn = getattr(app, "open_charts", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            pass

    # Fallback: show a simple window with guidance.
    try:
        import tkinter as tk
        from tkinter import ttk
        win = tk.Toplevel(app)
        win.title("Stats charts")
        win.geometry("780x520")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Charts popup is unavailable in this build.", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frm, text="Use the inline charts on the Stats page (bottom panel).").pack(anchor="w", pady=(6,0))
        ttk.Label(frm, text="If you still want the old popup charts, I can re-enable them safely.").pack(anchor="w", pady=(6,0))
        ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e", pady=(18,0))
        return
    except Exception:
        return
# --------------------------------------------------------------



from loop_manager import LoopManager, PostResult

SCRIPT_VERSION = "2026-01-02.v8_7_4"


def _clamp_conf(cfg: Dict[str, Any], key: str = "confidence") -> float:
    """Clamp confidence value into [confidence_min, confidence_max].

    We keep this tiny + dependency-free because it's used very early (FASTSCAN + locate).
    """
    try:
        c = float(cfg.get(key, 0.91))
    except Exception:
        c = 0.91
    try:
        cmin = float(cfg.get("confidence_min", 0.70))
    except Exception:
        cmin = 0.70
    try:
        cmax = float(cfg.get("confidence_max", 0.99))
    except Exception:
        cmax = 0.99
    if cmin > cmax:
        cmin, cmax = cmax, cmin
    if c < cmin:
        c = cmin
    if c > cmax:
        c = cmax
    return float(c)


# ================== Admin (optional) ==================
AUTO_ELEVATE = False  # True only if you really need UAC on each start

# ── Debug mode global state ──────────────────────────────────────────────────
# _DEBUG_CFG_PROVIDER is set once in main() so that click_xy (which doesn't
# receive cfg) can read debug_mode / debug_click_delay at runtime.
_DEBUG_CFG_PROVIDER: Optional[Callable[[], Dict[str, Any]]] = None
_DEBUG_LAST_CLICK_TS: float = 0.0  # monotonic ts of last click_xy execution


def _park_mouse_for_scan(cfg: Dict[str, Any]) -> None:
    """Park mouse to prevent hover highlight altering card appearance during FASTSCAN."""
    try:
        if not cfg.get("park_mouse_during_scan", True):
            return
        xy = cfg.get("park_mouse_xy", [10, 10])
        if not isinstance(xy, (list, tuple)) or len(xy) < 2:
            xy = [10, 10]
        x = int(float(xy[0]))
        y = int(float(xy[1]))
        # Avoid (0,0) to not trigger PyAutoGUI failsafe.
        if x <= 0 and y <= 0:
            x, y = 10, 10
        pyautogui.moveTo(x, y)
    except Exception:
        return

def locate_center_vehicle(cfg: Dict[str, Any], img_path: Path, region: Optional[Tuple[int, int, int, int]] = None) -> Optional[Tuple[int, int]]:
    """Vehicle finder with multi-confidence fallback to reduce NOT FOUND loops.
    - First tries cfg['vehicle_confidence'] (default 0.93) or global confidence, whichever is higher.
    - Then (if enabled) steps down confidence a bit and retries (same screenshot context).
    """
    if pyautogui is None:
        return None
    grayscale_default = bool(cfg.get("grayscale", True))
    base_conf = _clamp_conf(cfg)
    try:
        veh_conf = float(cfg.get("vehicle_confidence", 0.93))
    except Exception:
        veh_conf = 0.93
    conf_hi = max(base_conf, veh_conf)

    # Build confidence attempts
    confs = [conf_hi]
    if HAS_OPENCV and bool(cfg.get("use_confidence", True)) and bool(cfg.get("vehicle_conf_multi", True)):
        # step down a bit; this is what usually turns 'found on 3rd sweep' into 'found now'
        for delta in (0.03, 0.06, 0.09):
            c = conf_hi - delta
            if c < float(cfg.get("confidence_min", 0.70)):
                break
            if c not in confs:
                confs.append(c)

    # Optional low fallback (kept for compatibility)
    if bool(cfg.get("vehicle_conf_low_fallback", False)) and base_conf not in confs:
        confs.append(base_conf)

    try:
        for gray in (grayscale_default, False) if bool(cfg.get("vehicle_try_color_fallback", True)) else (grayscale_default,):
            for conf in confs:
                if HAS_OPENCV and bool(cfg.get("use_confidence", True)):
                    if region is None:
                        box = pyautogui.locateOnScreen(str(img_path), confidence=float(conf), grayscale=gray)
                    else:
                        box = pyautogui.locateOnScreen(str(img_path), region=region, confidence=float(conf), grayscale=gray)
                else:
                    if region is None:
                        box = pyautogui.locateOnScreen(str(img_path), grayscale=gray)
                    else:
                        box = pyautogui.locateOnScreen(str(img_path), region=region, grayscale=gray)
                if box:
                    c = pyautogui.center(box)
                    return int(c.x), int(c.y)
        return None
    except Exception:
        return None

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def try_elevate_if_needed() -> None:
    if not AUTO_ELEVATE:
        return
    if not sys.platform.startswith("win"):
        return
    if "__file__" not in globals():
        return
    if is_admin():
        return
    try:
        params = f'"{Path(__file__).resolve()}"'
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)
    except Exception:
        return


try_elevate_if_needed()

# ================== App paths ==================
APP_NAME = "WiwangAutomation"
USER_NAME = getpass.getuser()


def user_data_dir() -> Path:
    """Where we store config/logs/etc.

    Portable-first: use a local ./sale folder next to the script (best for C:\\sale).
    Override with WIWANG_USER_DIR env var if needed.
    """
    # Explicit override (absolute or relative).
    env_dir = os.environ.get("WIWANG_USER_DIR")
    if env_dir:
        try:
            p = Path(env_dir).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass

    # Portable mode: <script_dir>/sale (preferred).
    try:
        base_dir = Path(__file__).resolve().parent
    except Exception:
        base_dir = Path.cwd()

    try:
        portable = base_dir / "sale"
        portable.mkdir(parents=True, exist_ok=True)
        return portable
    except Exception:
        pass

    # Fallback: OS config dir.
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME / USER_NAME
    return Path.home() / ".config" / APP_NAME / USER_NAME



USER_DIR = user_data_dir()

def _resolve_user_path(p: Any) -> Optional[str]:
    """Resolve a user-provided path safely.

    Supports absolute paths and paths relative to USER_DIR. Expands ~ and %VAR%.
    Returns a string path (or None) to keep older call-sites simple.
    """
    try:
        if not p:
            return None
        s = str(p).strip()
        if not s:
            return None
        # Expand env/user tokens
        try:
            s = os.path.expandvars(os.path.expanduser(s))
        except Exception:
            pass
        # If relative -> make it relative to USER_DIR
        try:
            if not os.path.isabs(s):
                s = str((USER_DIR / s).resolve())
        except Exception:
            # Fallback join
            try:
                s = str(USER_DIR / s)
            except Exception:
                pass
        return s
    except Exception:
        return None

USER_DIR.mkdir(parents=True, exist_ok=True)

PLATE_BLACKLIST_DIR = USER_DIR / "plate_blacklist"
PLATE_BLACKLIST_DIR.mkdir(parents=True, exist_ok=True)

VEHICLE_BLACKLIST_DIR = USER_DIR / "vehicle_blacklist"
VEHICLE_BLACKLIST_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = USER_DIR / "config.json"
PROCESSED_PATH = USER_DIR / "processed.json"
PHOTO_HASHES_PATH = USER_DIR / "photo_hashes.json"
STATS_PATH = USER_DIR / "stats.json"
LOG_PATH = USER_DIR / "script_debug.log"
ANCHOR_FORM_PATH = USER_DIR / "anchor_form.png"
PLATE_LABEL_ANCHOR_PATH = USER_DIR / "plate_label_anchor.png"
ARCHIVE_CSV_PATH = USER_DIR / "archive.csv"

PICK_ATTEMPTS_CSV_PATH = USER_DIR / "pick_attempts.csv"
PRICE_DECISIONS_CSV_PATH = USER_DIR / "price_decisions.csv"
EDIT_LOG_CSV_PATH = USER_DIR / "edit_log.csv"
PRICE_SUGGESTIONS_CSV_PATH = USER_DIR / "price_suggestions.csv"

MAX_USER_FILE_BYTES = 100 * 1024 * 1024
TEXT_LIKE_EXTENSIONS = {
    ".log",
    ".txt",
    ".csv",
    ".json",
    ".ndjson",
    ".tsv",
}
TEXT_LIKE_NAMES = {
    "script_debug.log",
    "archive.csv",
    "processed.json",
    "photo_hashes.json",
    "stats.json",
    "rentals_summary.json",
    "plate_map.json",
}

def _is_text_like(path: Path) -> bool:
    try:
        if path.name in TEXT_LIKE_NAMES:
            return True
        return path.suffix.lower() in TEXT_LIKE_EXTENSIONS
    except Exception:
        return False

def _truncate_file_tail(path: Path, max_bytes: int) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size <= max_bytes:
                return False
            fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read(max_bytes)
        with path.open("wb") as fh:
            fh.write(data)
        return True
    except Exception:
        return False

def _enforce_user_file_limits(max_bytes: int = MAX_USER_FILE_BYTES) -> None:
    try:
        for p in USER_DIR.rglob("*"):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except Exception:
                continue
            if size <= max_bytes:
                continue
            if _is_text_like(p):
                if _truncate_file_tail(p, max_bytes):
                    print(f"[BOOT] file trimmed to {max_bytes} bytes: {p}")
                continue
            try:
                p.unlink()
                print(f"[BOOT] large file removed (> {max_bytes} bytes): {p}")
            except Exception:
                pass
    except Exception:
        pass

# ---------------- logging ----------------
LOG_LOCK = threading.Lock()
LOG_QUEUE: "queue.Queue[str]" = __import__("queue").Queue()
LOG_SETTINGS: Dict[str, Any] = {
    "min_level": "INFO",
    "include_caller": True,
    "include_thread": True,
    "include_process": True,
    "include_time_ms": True,
    "trace_calls": False,
    "trace_returns": False,
    "trace_exceptions": False,
    "trace_modules": [
        "wiwang_poster_loop",
        "loop_manager",
        "config_compat",
        "plate_reader",
        "plate_registry",
        "rental_limiter",
        "tg_rent_tracker",
        "gui",
    ],
    "trace_max_repr": 2000,
    "trace_args": True,
    "trace_ignore": {
        "log",
        "_trace_callback",
        "_safe_repr",
        "_format_call_args",
        "_apply_log_settings",
        "_setup_logging_bridge",
    },
    "trace_ignore_modules": {"config_compat"},
    "file_flush": True,
    "dedupe_enabled": True,
    "dedupe_window_s": 0.5,
    "dedupe_levels": {"TRACE", "DEBUG"},
    "dedupe_key_mode": "message",
    "quiet_mode": True,
    "quiet_level": "INFO",
}
LOG_TRACE_LOCAL = threading.local()
LOG_DEDUPE_STATE: Dict[str, Any] = {
    "key": None,
    "ts": 0.0,
}

METRICS_LOCK = threading.Lock()
METRICS_STATE: Dict[str, Any] = {
    "candidates_found": 0,
    "blocked_never_rent": 0,
    "blocked_over_limit": 0,
    "blocked_no_plate": 0,
    "blocked_low_confidence": 0,
    "blocked_unknown_plate": 0,
    "last_decision_reason": "",
    "last_decision_at": None,
    "fastscan_last_ts": 0.0,
    "fastscan_visible": 0,
    "fastscan_free": 0,
}

LOG_LEVEL_ORDER = {
    "TRACE": 10,
    "DEBUG": 20,
    "INFO": 30,
    "WARN": 40,
    "ERROR": 50,
}


def _safe_repr(value: Any, max_len: Optional[int] = None) -> str:
    try:
        text = repr(value)
    except Exception:
        text = "<unrepr>"
    if max_len is None:
        max_len = int(LOG_SETTINGS.get("trace_max_repr", 2000))
    if max_len and len(text) > max_len:
        return text[:max_len] + "...(truncated)"
    return text


def _format_call_args(frame: Any) -> str:
    try:
        arg_info = inspect.getargvalues(frame)
        parts = []
        for name in arg_info.args:
            if name in arg_info.locals:
                parts.append(f"{name}={_safe_repr(arg_info.locals.get(name))}")
        if arg_info.varargs and arg_info.varargs in arg_info.locals:
            parts.append(f"*{arg_info.varargs}={_safe_repr(arg_info.locals.get(arg_info.varargs))}")
        if arg_info.keywords and arg_info.keywords in arg_info.locals:
            parts.append(f"**{arg_info.keywords}={_safe_repr(arg_info.locals.get(arg_info.keywords))}")
        return ", ".join(parts)
    except Exception:
        return ""


def _get_caller_info(skip: int = 2) -> str:
    try:
        frame = inspect.currentframe()
        for _ in range(skip):
            if frame is None:
                break
            frame = frame.f_back
        if frame is None:
            return ""
        filename = os.path.basename(frame.f_code.co_filename or "")
        return f"{filename}:{frame.f_lineno}:{frame.f_code.co_name}"
    except Exception:
        return ""


def _format_log_line(
    msg: str,
    level: str,
    caller: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    if LOG_SETTINGS.get("include_time_ms", True):
        ts = f"{ts}.{int((time.time() % 1) * 1000):03d}"
    parts = [f"[{ts}]"]
    level = (level or "INFO").upper()
    parts.append(f"[{level}]")
    if LOG_SETTINGS.get("include_process", True):
        parts.append(f"[pid={os.getpid()}]")
    if LOG_SETTINGS.get("include_thread", True):
        t = threading.current_thread()
        parts.append(f"[thread={t.name}:{t.ident}]")
    if LOG_SETTINGS.get("include_caller", True):
        if not caller:
            caller = _get_caller_info(skip=3)
        if caller:
            parts.append(f"[{caller}]")
    line = " ".join(parts) + f" {msg}"
    if context:
        extra = " ".join(f"{k}={_safe_repr(v)}" for k, v in context.items())
        if extra:
            line = f"{line} | {extra}"
    return line


def log(
    msg: str,
    level: str = "INFO",
    *,
    context: Optional[Dict[str, Any]] = None,
    caller: Optional[str] = None,
) -> None:
    min_level = LOG_LEVEL_ORDER.get(str(LOG_SETTINGS.get("min_level", "TRACE")).upper(), 10)
    cur_level = LOG_LEVEL_ORDER.get(str(level).upper(), 30)
    if cur_level < min_level:
        return
    msg_text = str(msg)
    level_text = str(level).upper()
    line = _format_log_line(msg_text, level_text, caller=caller, context=context)
    if LOG_SETTINGS.get("dedupe_enabled", False):
        dedupe_levels = LOG_SETTINGS.get("dedupe_levels", {"TRACE", "DEBUG"})
        if not dedupe_levels or level_text in dedupe_levels:
            window_s = float(LOG_SETTINGS.get("dedupe_window_s", 0.0) or 0.0)
            if window_s > 0:
                key_mode = str(LOG_SETTINGS.get("dedupe_key_mode", "line")).lower()
                if key_mode == "message":
                    context_items = None
                    if context:
                        context_items = tuple(sorted((k, _safe_repr(v)) for k, v in context.items()))
                    dedupe_key = (level_text, caller or "", msg_text, context_items)
                else:
                    dedupe_key = line
                now = time.time()
                last_key = LOG_DEDUPE_STATE.get("key")
                last_ts = float(LOG_DEDUPE_STATE.get("ts", 0.0) or 0.0)
                if dedupe_key == last_key and (now - last_ts) <= window_s:
                    LOG_DEDUPE_STATE["ts"] = now
                    return
                LOG_DEDUPE_STATE["key"] = dedupe_key
                LOG_DEDUPE_STATE["ts"] = now
    print(line)
    with LOG_LOCK:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                if LOG_SETTINGS.get("file_flush", True):
                    f.flush()
                    os.fsync(f.fileno())
        except Exception:
            pass
    try:
        LOG_QUEUE.put_nowait(line)
    except Exception:
        pass


def _trace_callback(frame: Any, event: str, arg: Any):
    if not LOG_SETTINGS.get("trace_calls", False):
        return None
    if getattr(LOG_TRACE_LOCAL, "busy", False):
        return _trace_callback
    if event not in ("call", "return", "exception"):
        return _trace_callback
    mod = frame.f_globals.get("__name__", "")
    trace_modules = LOG_SETTINGS.get("trace_modules", [])
    if trace_modules:
        if not any(str(mod).startswith(str(prefix)) for prefix in trace_modules):
            return _trace_callback
    trace_ignore_modules = LOG_SETTINGS.get("trace_ignore_modules", set())
    if trace_ignore_modules and any(str(mod).startswith(str(prefix)) for prefix in trace_ignore_modules):
        return _trace_callback
    func = frame.f_code.co_name
    if mod == "gui.components.metric_card" and func == "_step":
        return _trace_callback
    if func in LOG_SETTINGS.get("trace_ignore", set()):
        return _trace_callback
    LOG_TRACE_LOCAL.busy = True
    try:
        caller = f"{mod}:{frame.f_lineno}:{func}"
        if event == "call":
            args_text = _format_call_args(frame) if LOG_SETTINGS.get("trace_args", True) else ""
            log(f"TRACE CALL {mod}.{func}({args_text})", level="TRACE", caller=caller)
        elif event == "return":
            if LOG_SETTINGS.get("trace_returns", False):
                log(f"TRACE RETURN {mod}.{func} -> {_safe_repr(arg)}", level="TRACE", caller=caller)
        elif event == "exception":
            if LOG_SETTINGS.get("trace_exceptions", True):
                exc_type, exc_value, _tb = arg
                log(
                    f"TRACE EXC {mod}.{func} -> {getattr(exc_type, '__name__', exc_type)}: {_safe_repr(exc_value)}",
                    level="TRACE",
                    caller=caller,
                )
    finally:
        LOG_TRACE_LOCAL.busy = False
    return _trace_callback


class _LogBridgeHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            log(msg, level=record.levelname, caller=f"{record.name}:{record.lineno}:{record.funcName}")
        except Exception:
            pass


def _setup_logging_bridge() -> None:
    try:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        if not any(isinstance(h, _LogBridgeHandler) for h in root.handlers):
            handler = _LogBridgeHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(handler)
    except Exception:
        pass


def _apply_log_settings(cfg: Dict[str, Any]) -> None:
    try:
        LOG_SETTINGS["min_level"] = str(cfg.get("log_level", LOG_SETTINGS.get("min_level", "TRACE"))).upper()
        LOG_SETTINGS["include_caller"] = bool(cfg.get("log_include_caller", True))
        LOG_SETTINGS["include_thread"] = bool(cfg.get("log_include_thread", True))
        LOG_SETTINGS["include_process"] = bool(cfg.get("log_include_process", True))
        LOG_SETTINGS["include_time_ms"] = bool(cfg.get("log_include_time_ms", True))
        LOG_SETTINGS["trace_calls"] = bool(cfg.get("log_trace_calls", True))
        LOG_SETTINGS["trace_returns"] = bool(cfg.get("log_trace_returns", True))
        LOG_SETTINGS["trace_exceptions"] = bool(cfg.get("log_trace_exceptions", True))
        LOG_SETTINGS["trace_args"] = bool(cfg.get("log_trace_args", True))
        LOG_SETTINGS["file_flush"] = bool(cfg.get("log_file_flush", True))
        LOG_SETTINGS["trace_max_repr"] = int(cfg.get("log_trace_max_repr", LOG_SETTINGS.get("trace_max_repr", 2000)))
        LOG_SETTINGS["dedupe_enabled"] = bool(cfg.get("log_dedupe_enabled", True))
        LOG_SETTINGS["dedupe_window_s"] = float(cfg.get("log_dedupe_window_s", 0.5))
        LOG_SETTINGS["dedupe_key_mode"] = str(cfg.get("log_dedupe_key_mode", "message")).lower()
        LOG_SETTINGS["quiet_mode"] = bool(cfg.get("log_quiet_mode", False))
        LOG_SETTINGS["quiet_level"] = str(cfg.get("log_quiet_level", "INFO")).upper()
        modules = cfg.get("log_trace_modules", LOG_SETTINGS.get("trace_modules"))
        if isinstance(modules, (list, tuple)):
            LOG_SETTINGS["trace_modules"] = list(modules)
        ignores = cfg.get("log_trace_ignore", [])
        if isinstance(ignores, (list, tuple, set)):
            LOG_SETTINGS["trace_ignore"] = set(ignores)
        ignore_modules = cfg.get("log_trace_ignore_modules", LOG_SETTINGS.get("trace_ignore_modules"))
        if isinstance(ignore_modules, (list, tuple, set)):
            LOG_SETTINGS["trace_ignore_modules"] = {str(prefix) for prefix in ignore_modules}
        dedupe_levels = cfg.get("log_dedupe_levels", LOG_SETTINGS.get("dedupe_levels"))
        if isinstance(dedupe_levels, (list, tuple, set)):
            LOG_SETTINGS["dedupe_levels"] = {str(level).upper() for level in dedupe_levels}
        if LOG_SETTINGS.get("quiet_mode", False):
            LOG_SETTINGS["min_level"] = str(LOG_SETTINGS.get("quiet_level", "INFO")).upper()
            LOG_SETTINGS["trace_calls"] = False
            LOG_SETTINGS["trace_returns"] = False
            LOG_SETTINGS["trace_exceptions"] = False
            LOG_SETTINGS["trace_args"] = False
    except Exception as e:
        log(f"LOG: apply settings failed: {e}", level="WARN")
    try:
        _setup_logging_bridge()
    except Exception:
        pass
    if LOG_SETTINGS.get("trace_calls", False):
        try:
            sys.setprofile(_trace_callback)
            threading.setprofile(_trace_callback)
        except Exception as e:
            log(f"LOG: trace install failed: {e}", level="WARN")
    else:
        try:
            sys.setprofile(None)
            threading.setprofile(None)
        except Exception:
            pass


def _install_exception_hooks() -> None:
    def _excepthook(exc_type, exc_value, exc_tb):
        try:
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            log(f"UNHANDLED EXCEPTION:\n{tb}", level="ERROR")
        except Exception:
            pass

    def _thread_excepthook(args):
        try:
            tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            log(f"THREAD EXCEPTION ({args.thread.name}):\n{tb}", level="ERROR")
        except Exception:
            pass

    try:
        sys.excepthook = _excepthook
    except Exception:
        pass
    try:
        if hasattr(threading, "excepthook"):
            threading.excepthook = _thread_excepthook  # type: ignore
    except Exception:
        pass


_setup_logging_bridge()
_install_exception_hooks()


def _metrics_inc(key: str, count: int = 1) -> None:
    with METRICS_LOCK:
        try:
            METRICS_STATE[key] = int(METRICS_STATE.get(key, 0)) + int(count)
        except Exception:
            METRICS_STATE[key] = int(count)


def _metrics_set(key: str, value: Any) -> None:
    with METRICS_LOCK:
        METRICS_STATE[key] = value


def append_archive_row(stem: str, price_raw: str, price_value: float) -> None:
    headers = ["timestamp", "stem", "price_raw", "price_value"]
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stem": stem,
        "price_raw": (price_raw or "").strip(),
        "price_value": float(price_value),
    }
    exists = ARCHIVE_CSV_PATH.exists()
    try:
        with open(ARCHIVE_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log(f"archive.csv write error: {e}")


def append_pick_attempt_row(stem: str, attempts: int, success: bool, reason: str = "") -> None:
    headers = ["timestamp", "stem", "attempts", "success", "reason"]
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stem": stem,
        "attempts": int(attempts),
        "success": 1 if bool(success) else 0,
        "reason": str(reason or ""),
    }
    try:
        exists = PICK_ATTEMPTS_CSV_PATH.exists()
        with open(PICK_ATTEMPTS_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log(f"pick_attempts.csv write error: {e}")



# ================== Defaults ==================
DEFAULT_COORDS: Dict[str, List[int]] = {
    "create": [1750, 45],
    "create_category": [0, 0],  # попап "Выберите категорию" (новый интерфейс). 0,0 = отключено.
    "create_rent": [780, 215],
    "form_back": [0, 0],
    "comment": [380, 610],
    "price": [675, 785],
    "create_ad": [965, 840],
    "select_time": [741, 545],
    "pay_card": [1082, 749],
    "files_prev": [886, 312],
    "files": [808, 468],
    "select_path": [924, 55],
    "open_file": [1748, 994],
    "file_one": [234, 180],
    "file_two": [339, 180],
    "file_three": [450, 180],
    "my_ads": [126, 881],
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "car_dir": r"C:\sale\car",
    "blacklist_vehicles": [],
    "vehicle_blacklist_capture_threshold": 0.80,
    "plate_blacklist": [],
    "plate_blacklist_confidence": 0.94,
    # ── Debug mode ──────────────────────────────────────────────────────────────
    # When debug_slow_mode=True every click_xy call:
    #   1) logs the action with caller info
    #   2) waits at least debug_slow_delay seconds BEFORE the click
    #   3) briefly highlights the cursor position (move → pause → click)
    # This makes it easy to visually follow what the bot is doing.
    # Toggle via GUI slider in Tuning tab.
    "debug_log_clicks": True,       # log every click_xy call to the console
    "debug_highlight_cursor": True,  # wiggle cursor at target before clicking
    # mode
    "loop_mode": True,
    "ok_retry_delay_loop": 240.0,  # in loop mode, re-check a vehicle only after this cooldown if POST OK
    "post_ok_server_cooldown": 5.0,  # seconds to wait after POST before navigating back (server rate limit)
    "rental_hours_append": "0",  # appended to existing hours value (e.g. field has '5', append '0' = '50')

    "dedupe_policy": "off",  # off | on_success | always
    "dedupe_force_in_loop": False,
    # debug slow mode
    "debug_slow_mode": False,
    "debug_slow_delay": 1.5,
    # image recognition
    "use_confidence": True,
    "confidence": 0.91,
    "confidence_min": 0.70,
    "confidence_max": 0.99,
    "grayscale": True,
    # master speed
    "speed": 1.35,
    # hotkeys
    "hotkey_f12_action": "stop",  # stop | pause
    # delays (BASE, scaled by speed)
    "ui_delay_short": 0.25,
    "ui_delay_medium": 0.70,
    "ui_delay_long": 1.40,
    "after_vehicle_click_delay": 2.8,
    # anchor
    "form_anchor_enabled": True,
    "form_anchor_timeout": 8.0,
    "form_anchor_poll": 0.35,
    # vehicle list readiness after navigation/post
    "ensure_vehicle_list_ready": True,
    "vehicle_list_ready_timeout": 8.0,
    "vehicle_list_ready_poll": 0.6,
    "vehicle_list_ready_cache_max_age": 1.2,
    "vehicle_list_ready_sample": 6,
    "vehicle_list_ready_initial_delay": 1.6,
    "post_ok_after_create_rent_delay": 1.2,
    "vehicle_list_ready_force_refresh": True,
    # typing
    "type_char_delay": 0.02,
    "type_line_delay": 0.06,
    "use_clipboard_paste": False,
    # NEW: safest mode regarding RU/EN
    "ignore_keyboard_layout": True,
    # NEW: focus + paste reliability
    "field_clicks": 2,
    "field_focus_delay": 0.12,
    "field_retries": 2,
    "field_reclick_before_paste": True,
    "field_reclick_after_clear": True,
    "field_force_cyrillic_layout": True,
    "paste_retries": 2,
    "paste_retry_delay": 0.10,
    "paste_verify": True,
    "paste_verify_delay": 0.08,
    "paste_allow_unverified": True,
    "paste_unverified_delay": 0.05,
    "paste_use_shift_insert": True,
    "paste_use_shift_insert_after_ctrl_v": False,
    "file_dialog_use_shift_insert": True,
    "layout_switch_on_fallback": True,
    "layout_switch_hotkey": ["alt", "shift"],
    "layout_switch_cyrillic_only": True,
    "layout_switch_use_win_api": True,
    "layout_switch_target_hkl": "00000419",
    "layout_switch_latin_on_fallback": True,
    "layout_switch_latin_ascii_only": True,
    "layout_switch_latin_target_hkl": "00000409",
    "force_layout_typed_input": True,
    # file dialog delays
    "file_dialog_open_delay": 4.0,
    "file_dialog_first_path_extra_pause": 6.0,
    "file_dialog_reuse_folder": True,
    "file_dialog_reuse_delay": 0.18,
    "file_dialog_after_click_path": 0.90,
    "file_dialog_force_click_type_path": True,  # focus by clicks and type path (no clipboard)
    "file_dialog_clicks_before_type": 2,
    "file_dialog_after_clear_path": 0.25,
    "file_dialog_after_type_path": 0.60,
    "file_dialog_force_address_hotkeys_before_type": True,  # Alt+D/Ctrl+L before typing path
    "file_dialog_clear_backspace_count": 120,
    "file_dialog_clear_backspace_delay": 0.005,
    "file_dialog_use_unicode_input": False,
    "file_dialog_use_winapi_settext_path": True,  # try WinAPI SetText for address bar path
    "file_dialog_winapi_enter_delay": 0.55,  # delay before Enter after WinAPI SetText
    "file_dialog_winapi_after_enter_delay": 0.90,  # delay after Enter after WinAPI SetText
    "file_dialog_winapi_enter_twice": True,  # send Enter twice after WinAPI SetText
    "file_dialog_winapi_folder_wait": 1.6,  # wait after WinAPI Enter to let folder load
    "file_dialog_winapi_clear_selection": True,  # clear address bar selection after WinAPI SetText
    "file_dialog_winapi_before_pick_delay": 1.8,  # wait before pick_file after WinAPI SetText
    "file_dialog_winapi_focus_before_enter": True,  # refocus address bar before Enter
    "file_dialog_after_enter_path": 2.40,
    "file_dialog_enter_twice": False,
    "file_dialog_force_click_type_verify": True,  # verify click-typed path via address bar copy
    "file_dialog_force_click_type_fallback_paste": True,  # paste path if click-typed verify fails
    "file_dialog_path_pre_type_delay": 0.40,
    "file_dialog_path_post_type_delay": 0.40,
    "file_dialog_path_post_paste_delay": 0.40,
    "file_dialog_after_navigate_before_pick": 1.60,
    "file_dialog_after_pick_click": 0.80,
    "file_dialog_after_click_open": 0.40,
    "file_dialog_after_open": 2.00,
    "file_dialog_after_open_confirm": 1.00,
    "file_dialog_verify_path": True,   # verify address bar after typing (Ctrl+C compare)
    "file_dialog_verify_strict": True,  # abort if path mismatch after retries
    "file_dialog_sanity_check_path": True,  # even if verify is off, retry once on detected mismatch
    "file_dialog_force_direct_write": False,  # avoid hotkeys; prefer click+type
    "file_dialog_prefer_clipboard": False,  # avoid clipboard in dialog path entry
    "file_dialog_clipboard_retries": 2,
    "file_dialog_allow_fallback_typing": False,  # allow layout-sensitive typing if clipboard fails
    "file_dialog_retry_clipboard_on_verify_fail": True,
    "file_dialog_use_filename_field": False,  # avoid filename field hotkeys
    "file_dialog_filename_first_only": True,
    "file_dialog_filename_skip_navigate": True,
    "file_dialog_filename_prefer_only": True,  # try filename field first, before click fallback
    "file_dialog_filename_allow_hotkey_fallback": False,  # allow filename field even without coords (hotkey)
    "file_dialog_filename_use_hotkey": False,  # use hotkey for filename field focus
    "file_dialog_filename_hotkey": ["alt", "n"],
    "file_dialog_filename_coords": None,  # click coords for filename input (recommended)
    "file_dialog_filename_clicks": 2,
    "file_dialog_filename_focus_delay": 0.06,
    "file_dialog_filename_label_texts": ["File name", "Имя файла"],  # label texts to find filename field
    "file_dialog_filename_tab_focus_enabled": True,  # focus filename field via Tab when coords missing
    "file_dialog_filename_tab_count": 6,  # tab presses to reach filename field
    "file_dialog_filename_tab_reverse": False,  # use Shift+Tab instead of Tab
    "file_dialog_filename_tab_focus_click_coords": None,  # optional pre-click before tabbing
    "file_dialog_filename_clear_before_paste": True,
    "file_dialog_filename_post_paste_delay": 0.12,
    "file_dialog_filename_confirm_enter": True,
    "file_dialog_filename_use_winapi_settext": True,  # try WinAPI SetText on focused filename field
    "file_dialog_filename_use_unicode_input": True,  # type path via unicode input in filename field
    "file_dialog_filename_verify": True,  # verify filename field text via copy
    "file_dialog_allow_layout_sensitive_write": False,  # allow pyautogui.write even when ignore_keyboard_layout=True
    "file_dialog_allow_layout_insensitive_write_ascii": True,  # allow direct write for ASCII-only paths (paired with Latin switch)
    "file_dialog_force_paste_no_verify": False,  # avoid Ctrl+V path paste
    "file_dialog_select_path_clicks": 2,  # extra clicks to ensure address bar focus before paste
    "file_dialog_force_latin_layout": True,  # switch to Latin layout before typing ASCII path
    "file_dialog_save_debug": True,  # save screenshot when path verification fails/unavailable
    "file_dialog_debug_dir": None,  # directory for dialog debug screenshots
    "file_dialog_verify_on_reuse": True,  # verify dialog path when reusing folder
    "file_dialog_force_navigate_on_unknown": True,  # navigate if path verify unavailable
    # posting interval
    "post_interval_min": 0.8,
    "post_interval_max": 1.6,
    # retries + errors
    "not_found_retry_delay": 8.0,
    "not_found_loop_delay": 5.0,  # seconds before rechecking a NOT_FOUND vehicle in loop mode
    "plate_mismatch_retry_delay": 25.0,
    "error_retry_delay": 3.0,
    "blocked_retry_delay": 20.0,
    "cycle_sleep": 0.8,
    # start nudge
    "start_nudge_enabled": True,
    "start_nudge_move_duration": 0.18,
    "start_nudge_delay": 0.10,
    # Auto-bump "My Ads"
    "bump_enabled": True,
    "bump_idle_after": 90.0,
    "bump_cooldown": 90.0,
    "bump_enter_delay": 1.20,
    "bump_click_delay": 0.60,
    "bump_back_delay": 0.55,
    "bump_grid_cols": 5,
    "bump_grid_rows": 3,
    "bump_grid_x0": 464,
    "bump_grid_y0": 211,
    "bump_grid_dx": 314,
    "bump_grid_dy": 290,

    # fast scan (1 screenshot -> matchTemplate for all templates)
    "fast_scan_enabled": True,
    "fast_scan_expand_region": True,
    "fast_scan_fallback_min": 0.70,
    "fast_scan_rebuild_on_miss": True,  # if True: rebuild FASTSCAN on miss
    "fast_scan_rebuild_on_miss_single": False,  # if True: rebuild only current template on miss (opt-in)
    "fast_scan_prebuild_current": True,  # prebuild cache for current template before search
    "fast_scan_prebuild_cache_max_age": 1.0,  # seconds; 0 -> always rebuild for current template
    "fast_scan_prebuild_fullscreen_first": True,  # prebuild current template in fullscreen before region scan
    "fast_scan_prebuild_fullscreen_on_miss": True,  # retry prebuild in fullscreen if region scan has no hits
    "fast_scan_template_scales_single": [0.70, 0.80, 0.90, 1.00, 1.10, 1.25, 1.40],
    "fast_scan_template_scales_multi": [1.00],
    "fast_scan_use_legacy_engine": True,
    "fast_scan_visible_only": True,
    "fast_scan_visible_idle_sleep": 1.0,
    "fast_scan_visible_fallback_every_s": 12.0,
    "fast_scan_pre_sweep_enabled": True,  # scan ALL templates in one screenshot at sweep start
    "fast_scan_template_bg_gray": 30,
    "fast_scan_alpha_mask_thr": 10,
    "vehicle_region_expand_down": 520,
    "vehicle_region_expand_up": 0,
    "fast_scan_multiscale": True,
    "fast_scan_scales": [0.92, 0.84, 1.08, 1.16],
    "fast_scan_topk": 3,
    "fast_scan_fullscreen_min_templates": 3,
    "fast_scan_fullscreen_on_miss": True,
    "fast_scan_fullscreen_on_miss_after": 2,
    "vehicle_locate_fullscreen_on_miss": True,
    "vehicle_locate_fullscreen_tries": 2,

    "vehicle_region": None,   # [x,y,w,h] or null (recommended to set via GUI)
    "vehicle_click_min": 0.78,
    "fast_scan_min_dist": 32,
    "fast_scan_parallel": True,
    "fast_scan_workers": 0,
    # plate validation (to avoid wrong-car posts / identical first photo cases)
    "plate_region": None,     # [x,y,180,40] or null
    "plate_w": 320,
    "plate_h": 60,
    "plate_confidence": 0.82,
    # NEW: robust plate extraction anchored on the label "Гос.Номер:"
    "plate_label_confidence": 0.45,
    "plate_label_match_scales": [0.9, 1.0, 1.1],
    "plate_value_gap": 6,
    "plate_value_pad_y": 3,
    "plate_value_max_w": None,
    "plate_value_trim": True,
    "plate_value_trim_thr": 18,
    "plate_value_trim_fallback": False,
    "plate_value_fallback_right_ratio": 0.62,
    "plate_value_fallback_pad_y": 3,
    "plate_label_anchor_region": None,
    # plate read (OCR from UI)
    "plate_read_attempts": 2,
    "plate_read_retry_delay": 0.35,
    "plate_read_prompt_on_fail": True,
    "plate_read_ocr_scale": 3,
    "plate_read_ocr_pad": 2,
    "plate_read_psm_list": ["7", "6"],
    "plate_read_thresholds": [120, 140, 160],
    "plate_read_value_crop_fallback": True,
    "plate_read_save_value_crop_debug": True,
    "plate_read_save_autocrop_debug": False,
    "plate_read_full_line_fallback": True,
    "plate_read_save_line_debug": False,
    "plate_read_loose_enabled": True,
    "plate_read_loose_psm_list": ["7", "8", "6", "11"],
    "plate_read_loose_lang": None,
    "plate_read_loose_config": "",
    "plate_read_ocr_data_enabled": True,
    "plate_read_ocr_data_psm_list": ["6", "7", "11"],
    "plate_read_ocr_data_config": "",
    "plate_read_use_value_crop_first": True,
    "plate_read_manual_cache_ttl": 120,
    "plate_read_side_crop_enabled": True,
    "plate_read_side_crop_ratio": 0.55,
    "plate_read_autocrop_enabled": True,
    "plate_read_autocrop_min_area_ratio": 0.012,
    "plate_read_autocrop_min_height_ratio": 0.45,
    "plate_read_autocrop_pad": 2,
    # limits / rules
    "limits": {
        "hard_block_no_plate": False,
        "hard_block_unknown_plate": False,
        "use_tg_active_truth": False,
        "use_fastscan_truth": False,
        "max_active_rentals_per_vehicle": None,
        "cooldown_minutes_after_return": None,
        "max_daily_hours": None,
        "max_weekly_hours": None,
        "min_plate_confidence": None,
    },
    "metrics_gui": {
        "enabled": True,
    },
    "plate_registry_cfg": {
        "enabled": False,
    },
    "plate_registry_enforce_match": True,
    "plate_registry_auto_register": True,
    "tg_tracker_cfg": {
        "enabled": False,
    },
    "admin_gate": {
        "enabled": False,
        "salt": "",
        "password_hash": "",
        "min_length": 8,
    },
    "ui_prefs": {
        "search": "",
        "remember_until_close": False,
    },
    # watchdog (auto refresh if UI stuck)
    "watchdog_enabled": True,
    "stall_region": None,     # [x,y,w,h] or null (set via GUI)
    "stall_timeout_s": 25,
    "stall_w": 260,
    "stall_h": 120,
    # refresh behavior
    "refresh_on_empty_sweep": True,
    # file dialog optimization
    "file_dialog_set_path_each_file": False,  # if False: type folder path only for photo #1
    # bump options
    "bump_use_points": False,
    "bump_points": [],
    "bump_pages": 2,
    "bump_scroll_pixels": 260,
    "bump_scroll_delay": 0.25,
    "fast_scan_compete_enabled": True,  # reject false-positive template hits
    "fast_scan_compete_margin": 0.035,
    "fast_scan_compete_w": 520,
    "fast_scan_compete_h": 360,
    "fast_scan_compete_min_best": 0.80,
    # vehicle list autoscroll on miss (opt-in)
    "vehicle_autoscroll_on_miss": False,
    "vehicle_autoscroll_steps": 10,
    "vehicle_autoscroll_pixels": 480,
    "vehicle_autoscroll_delay": 0.16,
    "vehicle_autoscroll_reset": True,

    # logging (max detail by default)
    "log_level": "TRACE",
    "log_include_caller": True,
    "log_include_thread": True,
    "log_include_process": True,
    "log_include_time_ms": True,
    "log_file_flush": True,
    "log_trace_calls": True,
    "log_trace_returns": True,
    "log_trace_exceptions": True,
    "log_trace_args": True,
    "log_trace_max_repr": 2000,
    "log_dedupe_enabled": True,
    "log_dedupe_window_s": 0.5,
    "log_dedupe_levels": ["TRACE", "DEBUG"],
    "log_dedupe_key_mode": "message",
    "log_quiet_mode": True,
    "log_quiet_level": "INFO",
    "log_trace_modules": [
        "wiwang_poster_loop",
        "loop_manager",
        "config_compat",
        "plate_reader",
        "plate_registry",
        "rental_limiter",
        "tg_rent_tracker",
        "gui",
    ],
    "log_trace_ignore_modules": [
        "config_compat",
    ],
    "log_trace_ignore": [
        "log",
        "_trace_callback",
        "_safe_repr",
        "_format_call_args",
        "_apply_log_settings",
        "_setup_logging_bridge",
    ],

    # coords
    "coords": DEFAULT_COORDS,

    # Telegram tracker (optional)
    "telegram": {
        "enabled": False,
        "api_id": 0,
        "api_hash": "",
        "chat_title_contains": "Majestic",
        "session_name": "majestic_session",
        "output_csv": "rentals.csv",
        "output_json": "rentals_summary.json"
    },
}


class ConfigManager:
    @staticmethod
    def load() -> Dict[str, Any]:
        base = config_compat.load_config(CONFIG_PATH, log_fn=log)
        cfg = config_compat.apply_defaults(base, DEFAULT_CONFIG)
        _configure_runtime_env(cfg)
        try:
            keys = list(cfg.keys()) if isinstance(cfg, dict) else []
            log(f"CONFIG: loaded keys={len(keys)}")
            detected = config_compat.detect_keys(cfg)
            log(f"CONFIG: detected fastscan keys: {detected.get('fastscan', [])}")
            log(f"CONFIG: detected tg keys: {detected.get('tg', [])}")
            log(
                "LOG: settings applied",
                level="DEBUG",
                context={
                    "min_level": LOG_SETTINGS.get("min_level"),
                    "trace_calls": LOG_SETTINGS.get("trace_calls"),
                    "trace_returns": LOG_SETTINGS.get("trace_returns"),
                    "trace_exceptions": LOG_SETTINGS.get("trace_exceptions"),
                    "trace_modules": LOG_SETTINGS.get("trace_modules"),
                },
            )
        except Exception:
            pass
        return cfg

    @staticmethod
    def save(cfg: Dict[str, Any], changed_keys: Optional[Set[str]] = None) -> None:
        ok, before, after = config_compat.save_config_preserve(
            CONFIG_PATH,
            cfg,
            log_fn=log,
            changed_keys=changed_keys,
            allow_empty_lists=True,
        )
        if ok:
            added = max(0, after - before)
            log(f"CONFIG: saving merge-preserve, keys before={before} after={after} (added {added})")


# ---------------- persistence ----------------
def load_processed() -> Set[str]:
    try:
        if PROCESSED_PATH.exists():
            data = json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception as e:
        log(f"processed load error: {e}")
    return set()

# ---------------- MSK scheduling + price decisions ----------------
# --- Time zones (robust on Windows without tzdata) ---
# Prefer ZoneInfo when tzdata is available. If not, fall back to fixed MSK offset (UTC+3).
# Moscow has no DST, so fixed +03:00 is fine for MSK slotting.
MSK_TZ = ZoneInfo("Europe/Moscow")

import datetime as _dt
# Use New York as "local" for interpreting historical naive timestamps (matches your timezone setting)
LOCAL_TZ = ZoneInfo("America/New_York")

SLOT_HOURS = {"low": 10.0, "mid": 6.0, "high": 4.0, "gold": 4.0}

def to_msk(dt_naive: datetime) -> datetime:
    """Interpret naive dt as LOCAL_TZ and convert to MSK_TZ."""
    try:
        return dt_naive.replace(tzinfo=LOCAL_TZ).astimezone(MSK_TZ)
    except Exception:
        return dt_naive.replace(tzinfo=MSK_TZ)

def msk_daytype(dt_msk: datetime) -> str:
    return "weekend" if dt_msk.weekday() >= 5 else "weekday"

def msk_slot(dt_msk: datetime) -> str:
    """
    Slots per your logic (MSK):
      low:  02:00–12:00
      mid:  12:00–18:00
      high: 18:00–22:00
      gold: 22:00–02:00 (wrap)
    """
    hh = dt_msk.hour + dt_msk.minute / 60.0
    if 2.0 <= hh < 12.0:
        return "low"
    if 12.0 <= hh < 18.0:
        return "mid"
    if 18.0 <= hh < 22.0:
        return "high"
    return "gold"

def default_schedule() -> Dict[str, Any]:
    return {
        "mode": "manual",  # manual | schedule | auto_suggest
        "weekday": {"low": 0.95, "mid": 1.00, "high": 1.08, "gold": 1.12},
        "weekend": {"low": 1.00, "mid": 1.06, "high": 1.12, "gold": 1.18},
        "limits": {"min": 0.70, "max": 1.40, "step": 0.03, "min_events": 3, "threshold": 0.12},
    }

def load_schedule(folder: Path) -> Dict[str, Any]:
    p = folder / "schedule.json"
    base = default_schedule()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                base["mode"] = str(d.get("mode", base["mode"]))
                for k in ("weekday", "weekend"):
                    if isinstance(d.get(k), dict):
                        for slot in base[k]:
                            if slot in d[k]:
                                try:
                                    base[k][slot] = float(d[k][slot])
                                except Exception:
                                    pass
                if isinstance(d.get("limits"), dict):
                    for lk in base["limits"]:
                        if lk in d["limits"]:
                            base["limits"][lk] = d["limits"][lk]
        except Exception as e:
            log(f"schedule load error in {folder}: {e}")
    return base

def save_schedule(folder: Path, schedule: Dict[str, Any]) -> None:
    p = folder / "schedule.json"
    try:
        p.write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"schedule save error: {e}")


def compute_effective_price(cfg: Dict[str, Any], folder: Path, base_price_raw: str) -> Tuple[str, float, Dict[str, Any]]:
    """Return (price_raw_to_post, price_value_number, debug_info).
    If schedule mode is active for this vehicle, apply MSK slot multipliers from schedule.json.
    """
    base_raw = (base_price_raw or "").strip()
    base_val = parse_price_to_number(base_raw) if base_raw else 0.0
    info: Dict[str, Any] = {"base_raw": base_raw, "base_val": float(base_val)}
    try:
        sched = load_schedule(folder)
    except Exception:
        sched = default_schedule()
    mode = str(sched.get("mode", "manual") or "manual").strip().lower()
    info["mode"] = mode
    # Manual mode (or invalid schedule) still must be a clean integer string.
    if mode not in ("schedule", "auto_suggest") or base_val <= 0:
        out_raw, out_int = price_to_int_up(base_raw or "0")
        info["final_val"] = int(out_int)
        return out_raw, float(out_int), info

    dt_msk = to_msk(datetime.now())
    daytype = msk_daytype(dt_msk)
    slot = msk_slot(dt_msk)
    info["daytype"] = daytype
    info["slot"] = slot
    try:
        mult = float(sched.get(daytype, {}).get(slot, 1.0))
    except Exception:
        mult = 1.0
    # clamp to limits (defensive)
    try:
        lim = sched.get("limits", {}) if isinstance(sched.get("limits"), dict) else {}
        mn = float(lim.get("min", 0.70))
        mx = float(lim.get("max", 1.40))
        if mx < mn:
            mn, mx = mx, mn
        mult = max(mn, min(mx, mult))
    except Exception:
        pass
    info["mult"] = float(mult)

    # UI can't handle decimals ("." becomes a digit). Always round UP.
    final_val = int(math.ceil(float(base_val) * float(mult)))
    if final_val < 0:
        final_val = 0
    info["final_val"] = final_val

    # log decision for stats/heatmaps
    try:
        dt_msk = info.get("dt_msk")
        if dt_msk is None:
            dt_msk = to_msk(datetime.now())
        append_price_decision_row(
            folder.name,
            base_raw or "0",
            float(base_val),
            str(final_val),
            float(final_val),
            str(info.get("mode", "manual")),
            str(info.get("daytype", "")),
            str(info.get("slot", "")),
            float(info.get("mult", 1.0)),
            dt_msk,
        )
    except Exception:
        pass

    return str(final_val), float(final_val), info

def append_price_decision_row(
    stem: str,
    base_raw: str,
    base_value: float,
    chosen_raw: str,
    chosen_value: float,
    mode: str,
    daytype: str,
    slot: str,
    mult: float,
    dt_msk: datetime,
) -> None:
    headers = [
        "timestamp_local",
        "timestamp_msk",
        "stem",
        "mode",
        "daytype",
        "slot",
        "multiplier",
        "base_raw",
        "base_value",
        "chosen_raw",
        "chosen_value",
    ]
    row = {
        "timestamp_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_msk": dt_msk.strftime("%Y-%m-%d %H:%M:%S"),
        "stem": stem,
        "mode": mode,
        "daytype": daytype,
        "slot": slot,
        "multiplier": float(mult),
        "base_raw": (base_raw or "").strip(),
        "base_value": float(base_value),
        "chosen_raw": (chosen_raw or "").strip(),
        "chosen_value": float(chosen_value),
    }
    exists = PRICE_DECISIONS_CSV_PATH.exists()
    try:
        with open(PRICE_DECISIONS_CSV_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log(f"price_decisions write error: {e}")

# ---------------- UI: tooltips ----------------
class Tooltip:
    """Simple hover tooltip. Use \n for line breaks."""
    def __init__(self, widget, text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after = None
        self._tip = None
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _e=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        try:
            if self._after is not None:
                self.widget.after_cancel(self._after)
        except Exception:
            pass
        self._after = None

    def _show(self):
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tw.attributes("-topmost", True)
            lbl = tk.Label(
                tw,
                text=self.text,
                justify="left",
                bg="#1f2430",
                fg="#e6e6e6",
                relief="solid",
                borderwidth=1,
                padx=10,
                pady=8,
                font=("Segoe UI", 9),
            )
            lbl.pack()
            self._tip = tw
        except Exception:
            self._tip = None

    def _hide(self):
        try:
            if self._tip is not None:
                self._tip.destroy()
        except Exception:
            pass
        self._tip = None


def save_processed(s: Set[str]) -> None:
    try:
        PROCESSED_PATH.write_text(json.dumps(sorted(s), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"processed save error: {e}")


def load_photo_hashes() -> Dict[str, str]:
    try:
        if PHOTO_HASHES_PATH.exists():
            data = json.loads(PHOTO_HASHES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        log(f"photo_hashes load error: {e}")
    return {}


def save_photo_hashes(d: Dict[str, str]) -> None:
    try:
        PHOTO_HASHES_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"photo_hashes save error: {e}")


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_price_to_number(price_text: str) -> float:
    s = (price_text or "").strip()
    if not s:
        return 0.0
    s = s.replace("\u00A0", " ").strip()
    s = re.sub(r"[^\d,.\s-]", "", s)
    s = s.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    if s.count(".") > 1:
        parts = s.split(".")
        s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except Exception:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else 0.0


def price_to_int_up(price_text: str) -> Tuple[str, int]:
    """Normalize any price text to an integer string WITHOUT separators.

    Critical: the target UI treats '.' as a digit, so decimals become catastrophic
    (e.g. 4749.05 -> 474905). We always round UP to the next integer.
    """
    try:
        val = float(parse_price_to_number(price_text))
    except Exception:
        val = 0.0
    if not (val > 0):
        return "0", 0
    iv = int(math.ceil(val))
    if iv < 0:
        iv = 0
    return str(iv), iv



class RegionDrawer(tk.Toplevel):
    """Fullscreen region selector using a screenshot background."""
    def __init__(self, master, title: str, on_done, fixed_size: Optional[Tuple[int,int]]=None):
        super().__init__(master)
        self.title(title)
        self.attributes("-topmost", True)
        self.attributes("-fullscreen", True)
        self.configure(bg="black")
        self.on_done = on_done
        self.fixed_size = fixed_size

        if pyautogui is None or ImageTk is None:
            raise RuntimeError("pyautogui + Pillow(ImageTk) required")

        shot = pyautogui.screenshot()
        self._img_tk = ImageTk.PhotoImage(shot)

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self._img_tk, anchor="nw")

        self.rect_id = None
        self.start = None

        self.canvas.bind("<ButtonPress-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Return>", lambda e: self._confirm())

        self.info = tk.Label(self, text="Выдели область. ENTER=сохранить, ESC=отмена", fg="white", bg="#000000",
                             font=("Segoe UI", 12, "bold"))
        self.info.place(x=20, y=20)

    def _on_down(self, e):
        self.start = (e.x, e.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        if self.fixed_size:
            w, h = self.fixed_size
            self.rect_id = self.canvas.create_rectangle(e.x, e.y, e.x+w, e.y+h, outline="#00ffcc", width=3)
        else:
            self.rect_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ffcc", width=3)

    def _on_drag(self, e):
        if not self.start or not self.rect_id:
            return
        x0, y0 = self.start
        if self.fixed_size:
            w, h = self.fixed_size
            self.canvas.coords(self.rect_id, x0, y0, x0+w, y0+h)
        else:
            self.canvas.coords(self.rect_id, x0, y0, e.x, e.y)

    def _cancel(self):
        self.on_done(None)
        self.destroy()

    def _confirm(self):
        if not self.rect_id:
            return
        x1, y1, x2, y2 = self.canvas.coords(self.rect_id)
        x = int(min(x1, x2))
        y = int(min(y1, y2))
        w = int(abs(x2-x1))
        h = int(abs(y2-y1))
        if self.fixed_size:
            w, h = self.fixed_size
        if w < 5 or h < 5:
            return
        self.on_done([x, y, w, h])
        self.destroy()


class Stats:
    _lock = threading.Lock()

    @staticmethod
    def load() -> Dict[str, Any]:
        with Stats._lock:
            if STATS_PATH.exists():
                try:
                    d = json.loads(STATS_PATH.read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        d.setdefault("items", {})
                        d.setdefault("total_posts", 0)
                        d.setdefault("total_revenue", 0.0)
                        return d
                except Exception as e:
                    log(f"stats load error: {e}")
            return {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_posts": 0,
                "total_revenue": 0.0,
                "items": {},
            }

    @staticmethod
    def save(d: Dict[str, Any]) -> None:
        with Stats._lock:
            d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            STATS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def record_post(stem: str, price_value: float) -> None:
        d = Stats.load()
        items = d.get("items", {})
        rec = items.get(stem) or {"posts": 0, "revenue": 0.0, "last_price": 0.0, "last_posted_at": ""}
        rec["posts"] = int(rec.get("posts", 0)) + 1
        rec["revenue"] = float(rec.get("revenue", 0.0)) + float(price_value)
        rec["last_price"] = float(price_value)
        rec["last_posted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        items[stem] = rec
        d["items"] = items
        d["total_posts"] = int(d.get("total_posts", 0)) + 1
        d["total_revenue"] = float(d.get("total_revenue", 0.0)) + float(price_value)
        log(f"POST OK: {stem} | ${float(price_value):.2f}")
        Stats.save(d)


    @staticmethod
    def record_tg_rent(stem: str, revenue: int, hours: int, renter: str, plate: str, car_raw: str = "") -> None:
        """Persist Telegram rent stats into the main stats.json item (non-destructive)."""
        try:
            stem = str(stem or "").strip()
            if not stem:
                return
            d = Stats.load()
            items = d.setdefault("items", {})
            it = items.get(stem) or {}
            it["tg_events"] = int(it.get("tg_events", 0) or 0) + 1
            it["tg_revenue"] = int(it.get("tg_revenue", 0) or 0) + int(revenue)
            it["tg_hours"] = int(it.get("tg_hours", 0) or 0) + int(hours)
            it["tg_last_renter"] = str(renter or "")
            it["tg_last_plate"] = str(plate or "")
            if car_raw:
                it["tg_last_car_raw"] = str(car_raw)
            items[stem] = it
            Stats.save(d)
        except Exception:
            pass

# ---------------- pause/stop primitives ----------------
def wait_if_paused(run_event: threading.Event, stop_event: threading.Event) -> None:
    while not stop_event.is_set() and not run_event.is_set():
        time.sleep(0.10)


def sleep_coop(seconds: float, run_event: threading.Event, stop_event: threading.Event) -> None:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end and not stop_event.is_set():
        if not run_event.is_set():
            wait_if_paused(run_event, stop_event)
            if stop_event.is_set():
                return
        time.sleep(0.05)


keyboard_controller = Controller() if Controller is not None else None

# ---------------- debug slow mode (global ref) ----
from debug_state import DEBUG_SLOW as _DEBUG_SLOW

# ---------------- pyautogui safety ----------------
SAFE_MARGIN = 8


def init_pyautogui() -> None:
    if pyautogui is None:
        return
    try:
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.0
    except Exception:
        pass


def _speed(cfg: Dict[str, Any]) -> float:
    try:
        s = float(cfg.get("speed", 1.0))
        return max(0.25, min(4.0, s))
    except Exception:
        return 1.0


def d(cfg: Dict[str, Any], key: str, default: float) -> float:
    base = float(cfg.get(key, default))
    val = max(0.0, base / _speed(cfg))
    if cfg.get("debug_slow_mode", False):
        val = max(val, float(cfg.get("debug_slow_delay", 1.5)))
    return val


def safe_xy(x: int, y: int) -> Tuple[int, int]:
    if pyautogui is None:
        return x, y
    w, h = pyautogui.size()
    x2 = max(SAFE_MARGIN, min(int(x), w - SAFE_MARGIN))
    y2 = max(SAFE_MARGIN, min(int(y), h - SAFE_MARGIN))
    return x2, y2


def nudge_focus_to_create(cfg: Dict[str, Any]) -> bool:
    """Move mouse toward the game window to grab focus.

    NOTE: We intentionally do NOT click the 'Создать' button here.
    LoopManager calls enter_create_rent immediately after start, so
    clicking here would produce a double-click that opens AND closes
    the 'Выберите категорию' popup.
    """
    if pyautogui is None:
        return False
    try:
        init_pyautogui()
        coords = (cfg.get("coords") or {})
        xy = coords.get("create") or DEFAULT_COORDS["create"]
        x, y = safe_xy(int(xy[0]), int(xy[1]))
        dur = float(cfg.get("start_nudge_move_duration", 0.18))
        # Just move the mouse toward the game window area.
        # Do NOT click anything — enter_create_rent handles all navigation.
        pyautogui.moveTo(x, y, duration=max(0.0, dur))
        time.sleep(float(cfg.get("start_nudge_delay", 0.10)))
        return True
    except Exception as e:
        log(f"nudge_focus error: {e}")
        return False


def click_xy(
    xy: List[int],
    run_event: Optional[threading.Event] = None,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    global _DEBUG_LAST_CLICK_TS
    if pyautogui is None:
        return False
    if run_event is not None and stop_event is not None:
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return False
    try:
        x, y = safe_xy(int(xy[0]), int(xy[1]))

        # ── Debug mode: slow down + log + highlight ─────────────────────────────
        _dbg_cfg: Dict[str, Any] = {}
        try:
            if _DEBUG_CFG_PROVIDER is not None:
                _dbg_cfg = _DEBUG_CFG_PROVIDER()
        except Exception:
            pass
        _dbg_on = bool(_dbg_cfg.get("debug_slow_mode", False))

        if _dbg_on:
            # 1) Enforce minimum delay between clicks
            min_delay = float(_dbg_cfg.get("debug_slow_delay", 1.5))
            now = time.monotonic()
            elapsed = now - _DEBUG_LAST_CLICK_TS
            if elapsed < min_delay:
                wait_s = min_delay - elapsed
                if bool(_dbg_cfg.get("debug_log_clicks", True)):
                    log(f"\u2591DEBUG\u2591 click_xy({x},{y}) waiting {wait_s:.2f}s (min_delay={min_delay}s)")
                time.sleep(wait_s)

            # 2) Log the click with caller info
            if bool(_dbg_cfg.get("debug_log_clicks", True)):
                try:
                    caller = _get_caller_info(skip=2)
                except Exception:
                    caller = "?"
                log(f"\u2591DEBUG\u2591 click_xy({x},{y}) from {caller}")

            # 3) Highlight: move to target, wiggle, pause so user can see
            if bool(_dbg_cfg.get("debug_highlight_cursor", True)):
                pyautogui.moveTo(x, y, duration=0.25)
                # small wiggle so cursor is clearly visible
                for _dw in (6, -6, 4, -4):
                    try:
                        pyautogui.moveRel(_dw, 0, duration=0.04)
                    except Exception:
                        pass
                pyautogui.moveTo(x, y, duration=0.08)
                time.sleep(0.20)  # brief pause before actual click

        # ── actual click ──────────────────────────────────────────────
        if not _dbg_on:
            pyautogui.moveTo(x, y, duration=0.05)
        pyautogui.mouseDown()
        time.sleep(random.randint(10, 20) / 1000)
        pyautogui.mouseUp()

        _DEBUG_LAST_CLICK_TS = time.monotonic()
        return True
    except Exception as e:
        log(f"click error {xy}: {e}")
        return False


def clear_focused_field() -> None:
    if pyautogui is None:
        return
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.03)
    pyautogui.press("backspace")


def _norm_text(s: str) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def _safe_clip_get() -> str:
    if pyperclip is None:
        return ""
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def _safe_clip_set(s: str) -> bool:
    if pyperclip is None:
        return False
    try:
        pyperclip.copy(s)
        return True
    except Exception:
        return False


def _type_text_unicode(text: str, char_delay: float) -> bool:
    if not sys.platform.startswith("win"):
        return False
    if not text:
        return True
    try:
        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _INPUT(ctypes.Structure):
            class _I(ctypes.Union):
                _fields_ = [("ki", _KEYBDINPUT)]
            _anonymous_ = ("i",)
            _fields_ = [("type", ctypes.c_ulong), ("i", _I)]

        def _send(char_code: int, flags: int) -> None:
            extra = ctypes.c_ulong(0)
            ki = _KEYBDINPUT(0, char_code, flags, 0, ctypes.pointer(extra))
            inp = _INPUT(1, _INPUT._I(ki=ki))
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

        for ch in text:
            code = ord(ch)
            _send(code, 0x0004)  # KEYEVENTF_UNICODE
            _send(code, 0x0004 | 0x0002)  # KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            if char_delay > 0:
                time.sleep(char_delay)
        return True
    except Exception:
        return False


def paste_text_verified(text: str, cfg: Dict[str, Any]) -> bool:
    """
    Tries to paste via clipboard and (optionally) verifies that the target actually received it.
    This avoids false-positive "success" when Ctrl+V is ignored by the UI.

    Returns True only if we are reasonably sure text is inserted.
    """
    if pyautogui is None or pyperclip is None:
        return False

    text_n = _norm_text(text)
    old_clip = _safe_clip_get()

    verify = bool(cfg.get("paste_verify", True))
    verify_delay = float(cfg.get("paste_verify_delay", 0.08)) / _speed(cfg)

    retries = int(cfg.get("paste_retries", 2))
    retry_delay = float(cfg.get("paste_retry_delay", 0.10)) / _speed(cfg)

    try:
        for _ in range(max(1, retries)):
            sentinel = f"__WV_VERIFY_{time.time_ns()}__"
            # Set clipboard to desired text and paste
            if not _safe_clip_set(text_n):
                return False

            pyautogui.hotkey("ctrl", "v")
            time.sleep(max(0.01, verify_delay))

            if not verify:
                # If user disables verify, we still do NOT claim success blindly;
                # but in that mode we consider it "best effort".
                return True

            # Verify by copying all text back
            # If ctrl+c is ignored, clipboard stays sentinel and we detect it.
            _safe_clip_set(sentinel)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(max(0.01, verify_delay))
            pyautogui.hotkey("ctrl", "c")
            time.sleep(max(0.01, verify_delay))

            got = _safe_clip_get()
            got_n = _norm_text(got)

            if got_n == text_n:
                return True

            # If copy seems blocked (still sentinel), don't trust it, retry.
            if got_n == sentinel:
                time.sleep(max(0.01, retry_delay))
                continue

            # If it's different, retry (maybe focus issue)
            time.sleep(max(0.01, retry_delay))

        return False
    finally:
        # restore user's clipboard
        _safe_clip_set(old_clip)


def paste_text_unverified(text: str, cfg: Dict[str, Any]) -> bool:
    """
    Paste via clipboard without Ctrl+C verification.
    Useful for fields that block copy (layout-safe, best effort).
    """
    if pyautogui is None or pyperclip is None:
        return False

    text_n = _norm_text(text)
    old_clip = _safe_clip_get()
    delay = float(cfg.get("paste_unverified_delay", 0.05)) / _speed(cfg)
    try:
        if not _safe_clip_set(text_n):
            return False
        pyautogui.hotkey("ctrl", "v")
        if bool(cfg.get("paste_use_shift_insert", True)) and bool(cfg.get("paste_use_shift_insert_after_ctrl_v", False)):
            pyautogui.hotkey("shift", "insert")
        time.sleep(max(0.01, delay))
        return True
    finally:
        _safe_clip_set(old_clip)


def read_focused_field_text(cfg: Dict[str, Any]) -> Optional[str]:
    """
    Try to read focused field text via Ctrl+A/Ctrl+C.
    Returns text on success, None if copy is blocked/unreliable.
    """
    if pyautogui is None or pyperclip is None:
        return None

    old_clip = _safe_clip_get()
    verify_delay = float(cfg.get("paste_verify_delay", 0.08)) / _speed(cfg)
    sentinel = f"__WV_VERIFY_{time.time_ns()}__"
    try:
        _safe_clip_set(sentinel)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(max(0.01, verify_delay))
        pyautogui.hotkey("ctrl", "c")
        time.sleep(max(0.01, verify_delay))
        got = _safe_clip_get()
        got_n = _norm_text(got)
        if got_n == sentinel:
            return None
        return got
    finally:
        _safe_clip_set(old_clip)


def type_text_fallback(text: str, cfg: Dict[str, Any],
                       run_event: threading.Event, stop_event: threading.Event,
                       layout: Optional[str] = None,
                       char_delay_override: Optional[float] = None,
                       line_delay_override: Optional[float] = None) -> None:
    """
    Fallback typing. WARNING: respects current OS keyboard layout.
    Used only when verified paste fails.
    """
    if text is None:
        text = ""
    text = _norm_text(text)

    char_delay = float(char_delay_override) if char_delay_override is not None else float(cfg.get("type_char_delay", 0.02))
    line_delay = float(line_delay_override) if line_delay_override is not None else float(cfg.get("type_line_delay", 0.06))
    char_delay = char_delay / _speed(cfg)
    line_delay = line_delay / _speed(cfg)

    if layout == "latin":
        _ensure_latin_layout(cfg, text)
    else:
        _ensure_cyrillic_layout(cfg, text)

    has_cyr = bool(re.search(r"[А-Яа-яЁё]", text or ""))
    # Note: auto-switching layout for the game CEF window is unreliable.
    # The user must ensure Russian layout is active before starting the bot.
    # Characters that need clipboard paste (keyboard gives wrong chars in any layout)
    punct_chars = set('.,-!?@#$%&*()_+=:;"\'/<>[]{}|~`^\\')

    try:
        if keyboard_controller is not None and Key is not None:
            for ch in text:
                wait_if_paused(run_event, stop_event)
                if stop_event.is_set():
                    break
                try:
                    if ch == "\n":
                        keyboard_controller.press(Key.enter)
                        keyboard_controller.release(Key.enter)
                        time.sleep(line_delay)
                    elif ch in punct_chars:
                        # Punctuation: try keyboard_controller.type() first (Unicode),
                        # then clipboard fallback, then pyautogui.write()
                        typed = False
                        try:
                            keyboard_controller.type(ch)
                            typed = True
                        except Exception:
                            pass
                        if not typed:
                            try:
                                if _safe_clip_set(ch) and pyautogui is not None:
                                    pyautogui.hotkey('ctrl', 'v')
                                    typed = True
                            except Exception:
                                pass
                        if not typed and pyautogui is not None:
                            try:
                                pyautogui.write(ch)
                            except Exception:
                                pass
                        time.sleep(char_delay)
                    elif ch == " ":
                        keyboard_controller.press(Key.space)
                        keyboard_controller.release(Key.space)
                        time.sleep(char_delay)
                    else:
                        keyboard_controller.press(ch)
                        keyboard_controller.release(ch)
                        time.sleep(char_delay)
                except Exception:
                    pass
        else:
            if pyautogui is None:
                return
            for ch in text:
                wait_if_paused(run_event, stop_event)
                if stop_event.is_set():
                    break
                pyautogui.write(ch, interval=char_delay)
                if ch == "\n":
                    time.sleep(line_delay)
    finally:
        pass


def type_text_stable(text: str, cfg: Dict[str, Any],
                     run_event: threading.Event, stop_event: threading.Event) -> None:
    """
    Primary strategy:
    - If ignore_keyboard_layout=True -> try VERIFIED paste first.
      If that fails -> fallback typing.
    - Else: optional clipboard for long text, otherwise typing.
    """
    if text is None:
        text = ""
    text = _norm_text(text)

    if bool(cfg.get("force_layout_typed_input", False)):
        type_text_fallback(text, cfg, run_event, stop_event)
        return

    if bool(cfg.get("ignore_keyboard_layout", True)):
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return
        ok = paste_text_verified(text, cfg)
        if ok:
            return
        if bool(cfg.get("paste_allow_unverified", True)) and paste_text_unverified(text, cfg):
            return
        # fallback (layout switch before typing)
        type_text_fallback(text, cfg, run_event, stop_event)
        return

    # old behavior
    if bool(cfg.get("use_clipboard_paste", False)) and len(text) >= 30:
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return
        if paste_text_verified(text, cfg):
            return

    type_text_fallback(text, cfg, run_event, stop_event)


def _force_layout_switch(cfg: Dict[str, Any]) -> None:
    try:
        keys = cfg.get("layout_switch_hotkey", ["alt", "shift"])
        if pyautogui is not None and isinstance(keys, (list, tuple)) and keys:
            pyautogui.hotkey(*[str(k).lower() for k in keys])
            time.sleep(0.03)
    except Exception:
        pass


def _get_active_keyboard_layout() -> Optional[int]:
    if not sys.platform.startswith("win"):
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        return int(user32.GetKeyboardLayout(tid))
    except Exception:
        return None


def _activate_keyboard_layout(layout_hex: str) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        user32 = ctypes.windll.user32
        hkl = user32.LoadKeyboardLayoutW(str(layout_hex), 0x00000001)
        if not hkl:
            return False
        user32.ActivateKeyboardLayout(hkl, 0)
        return True
    except Exception:
        return False


def _ensure_cyrillic_layout(cfg: Dict[str, Any], text: str) -> None:
    if not bool(cfg.get("layout_switch_on_fallback", True)):
        return
    only_cyr = bool(cfg.get("layout_switch_cyrillic_only", True))
    has_cyr = bool(re.search(r"[А-Яа-яЁё]", text or ""))
    if only_cyr and not has_cyr:
        return
    if bool(cfg.get("layout_switch_use_win_api", True)):
        target_hex = str(cfg.get("layout_switch_target_hkl", "00000419"))
        try:
            target_lang = int(target_hex, 16) & 0xFFFF
        except Exception:
            target_lang = 0x0419
        current = _get_active_keyboard_layout()
        if current is not None and (current & 0xFFFF) == target_lang:
            return
        if _activate_keyboard_layout(target_hex):
            time.sleep(0.03)
            return
    _force_layout_switch(cfg)


def _ensure_latin_layout(cfg: Dict[str, Any], text: str) -> None:
    if not bool(cfg.get("layout_switch_latin_on_fallback", True)):
        return
    only_ascii = bool(cfg.get("layout_switch_latin_ascii_only", True))
    if only_ascii and not all(ord(ch) < 128 for ch in (text or "")):
        return
    if bool(cfg.get("layout_switch_use_win_api", True)):
        target_hex = str(cfg.get("layout_switch_latin_target_hkl", "00000409"))
        try:
            target_lang = int(target_hex, 16) & 0xFFFF
        except Exception:
            target_lang = 0x0409
        current = _get_active_keyboard_layout()
        if current is not None and (current & 0xFFFF) == target_lang:
            return
        if _activate_keyboard_layout(target_hex):
            time.sleep(0.03)
            return
    _force_layout_switch(cfg)


def write_field(coords: List[int], text: str, cfg: Dict[str, Any],
                run_event: threading.Event, stop_event: threading.Event,
                field_name: str = "field") -> bool:
    """
    Focus -> clear -> insert text (verified paste if enabled) with retries.
    """
    if pyautogui is None:
        return False

    clicks = int(cfg.get("field_clicks", 2))
    focus_delay = float(cfg.get("field_focus_delay", 0.12)) / _speed(cfg)
    retries = int(cfg.get("field_retries", 2))

    def _retype_with_fallback() -> None:
        clear_focused_field()
        sleep_coop(max(0.01, 0.06 / _speed(cfg)), run_event, stop_event)
        if isinstance(text, str) and text.isdigit():
            try:
                pyautogui.write(text, interval=0.01)
                return
            except Exception:
                pass
        type_text_fallback(_norm_text(text), cfg, run_event, stop_event)

    def _verify_written() -> Optional[bool]:
        if not bool(cfg.get("field_verify_after_write", True)):
            return True
        got = read_focused_field_text(cfg)
        if got is None:
            if bool(cfg.get("field_verify_accept_copy_blocked", True)):
                log(f"{field_name}: field verify copy-blocked -> accept")
                return True
            log(f"{field_name}: field verify copy-blocked -> reject")
            return False
        got_n = _norm_text(got)
        expected_n = _norm_text(text)
        if got_n == expected_n:
            return True
        log(f"{field_name}: field verify mismatch (got={len(got_n)} exp={len(expected_n)})")
        return False

    for attempt in range(max(1, retries)):
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return False

        # focus
        for _ in range(max(1, clicks)):
            click_xy(coords, run_event, stop_event)
            sleep_coop(max(0.01, focus_delay), run_event, stop_event)
            if stop_event.is_set():
                return False

        # clear
        clear_focused_field()
        sleep_coop(max(0.01, 0.06 / _speed(cfg)), run_event, stop_event)
        if bool(cfg.get("field_reclick_after_clear", True)):
            click_xy(coords, run_event, stop_event)
            sleep_coop(max(0.01, 0.05 / _speed(cfg)), run_event, stop_event)

        # insert
        if bool(cfg.get("field_force_cyrillic_layout", True)):
            _ensure_cyrillic_layout(cfg, _norm_text(text))
        if bool(cfg.get("force_layout_typed_input", False)):
            nt = _norm_text(text)
            type_text_fallback(nt, cfg, run_event, stop_event)
            # ------- verify + fallback -------
            if stop_event.is_set():
                return False
            verify = _verify_written()
            if verify is True:
                return True
            # Typed input may have failed (focus lost / CEF lag).  Try paste.
            log(f"{field_name}: force_typed verify failed -> paste fallback (attempt {attempt+1})")
            for _ in range(max(1, clicks)):
                click_xy(coords, run_event, stop_event)
                sleep_coop(max(0.01, focus_delay), run_event, stop_event)
            clear_focused_field()
            sleep_coop(max(0.01, 0.06 / _speed(cfg)), run_event, stop_event)
            if bool(cfg.get("field_reclick_after_clear", True)):
                click_xy(coords, run_event, stop_event)
                sleep_coop(max(0.01, 0.05 / _speed(cfg)), run_event, stop_event)
            if pyperclip is not None and pyautogui is not None:
                ok = paste_text_verified(nt, cfg)
                if ok:
                    verify = _verify_written()
                    if verify is True:
                        return True
                if bool(cfg.get("paste_allow_unverified", True)):
                    if paste_text_unverified(nt, cfg):
                        verify = _verify_written()
                        if verify is True:
                            return True
            # Last resort: re-type with longer delays
            log(f"{field_name}: paste fallback also failed -> retype with extra delay (attempt {attempt+1})")
            for _ in range(max(1, clicks)):
                click_xy(coords, run_event, stop_event)
                sleep_coop(max(0.02, focus_delay * 2), run_event, stop_event)
            clear_focused_field()
            sleep_coop(max(0.02, 0.12 / _speed(cfg)), run_event, stop_event)
            click_xy(coords, run_event, stop_event)
            sleep_coop(max(0.02, 0.10 / _speed(cfg)), run_event, stop_event)
            type_text_fallback(nt, cfg, run_event, stop_event)
            verify = _verify_written()
            if verify is True:
                return True
            if verify is None and attempt >= retries - 1:
                if bool(cfg.get("field_verify_accept_copy_blocked", True)):
                    log(f"{field_name}: force_typed final retype, copy blocked -> accept")
                    return True
            if attempt < retries - 1:
                log(f"{field_name}: force_typed all methods failed attempt {attempt+1} -> retry")
                sleep_coop(max(0.01, 0.15 / _speed(cfg)), run_event, stop_event)
                continue
            log(f"{field_name}: force_typed ALL attempts exhausted -> fail")
            return False

        if bool(cfg.get("ignore_keyboard_layout", True)) and pyautogui is not None and pyperclip is not None:
            if bool(cfg.get("field_reclick_before_paste", True)):
                click_xy(coords, run_event, stop_event)
                sleep_coop(max(0.01, 0.05 / _speed(cfg)), run_event, stop_event)
            ok = paste_text_verified(_norm_text(text), cfg)
            if ok:
                verify = _verify_written()
                if verify is True:
                    return True
                if verify is False:
                    _retype_with_fallback()
                    verify = _verify_written()
                    if verify is True:
                        return True

            if bool(cfg.get("paste_allow_unverified", True)):
                if paste_text_unverified(_norm_text(text), cfg):
                    verify = _verify_written()
                    if verify is True:
                        return True
                    if verify is False:
                        _retype_with_fallback()
                        verify = _verify_written()
                        if verify is True:
                            return True

            if isinstance(text, str) and text.isdigit():
                try:
                    pyautogui.write(text, interval=0.01)
                    verify = _verify_written()
                    if verify is True:
                        return True
                except Exception:
                    pass

            # If paste failed, retry with refocus once more in next loop
            if attempt < retries - 1:
                sleep_coop(max(0.01, 0.10 / _speed(cfg)), run_event, stop_event)
                continue

            # Final fallback (layout-sensitive)
            type_text_fallback(_norm_text(text), cfg, run_event, stop_event)
            verify = _verify_written()
            return verify is True and not stop_event.is_set()

        # normal typing path
        if bool(cfg.get("field_reclick_after_clear", True)):
            click_xy(coords, run_event, stop_event)
            sleep_coop(max(0.01, 0.05 / _speed(cfg)), run_event, stop_event)
        type_text_stable(text, cfg, run_event, stop_event)
        verify = _verify_written()
        return verify is True and not stop_event.is_set()

    return not stop_event.is_set()



# ---------------- Fast scan cache (1 screenshot -> matchTemplate for all templates) ----------------
FAST_SCAN_LOCK = threading.Lock()
FAST_SCAN_CACHE: Dict[str, Any] = {"ts": 0.0, "region": None, "pos": {}, "meta": {}}  # stem -> list[(x,y,score)]

# Navigation / refresh throttling
NAV_STATE: Dict[str, Any] = {"last_enter": 0.0, "in_create_rent": False}
FORCE_REFRESH_STATE: Dict[str, Any] = {"last_force": 0.0, "fail_streak": 0}



def _region_to_tuple(r) -> Optional[Tuple[int, int, int, int]]:
    if r is None:
        return None
    try:
        if isinstance(r, (list, tuple)) and len(r) == 4:
            return int(r[0]), int(r[1]), int(r[2]), int(r[3])
    except Exception:
        return None
    return None


def _limits_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    limits = {}
    try:
        if isinstance(cfg.get("limits"), dict):
            limits.update(cfg.get("limits"))
    except Exception:
        pass
    return limits


def _fast_scan_build_legacy(cfg: Dict[str, Any],
                            templates: List[Path],
                            use_fullscreen: bool = False,
                            region: Optional[Tuple[int, int, int, int]] = None) -> Dict[str, List[Tuple[int, int, float]]]:
    """Legacy fast-scan engine (no parallel, no template scaling)."""
    if pyautogui is None or (not HAS_OPENCV) or (cv2 is None) or (np is None):
        return {}
    if not bool(cfg.get("fast_scan_enabled", True)):
        return {}

    if use_fullscreen:
        region = None
    elif region is not None:
        region = _region_to_tuple(region)
    else:
        region = _region_to_tuple(cfg.get("vehicle_region"))
    region_used = region

    try:
        base_conf = float(_clamp_conf(cfg))
    except Exception:
        base_conf = 0.91
    try:
        veh_conf = float(cfg.get("vehicle_confidence", base_conf))
    except Exception:
        veh_conf = base_conf
    conf0 = max(base_conf, veh_conf)

    try:
        fallback_min = float(cfg.get("fast_scan_fallback_min", max(0.62, conf0 - 0.28)))
    except Exception:
        fallback_min = max(0.62, conf0 - 0.28)

    conf_steps = [conf0, conf0 - 0.05, conf0 - 0.10, conf0 - 0.15, conf0 - 0.20, conf0 - 0.25]
    conf_steps = [max(fallback_min, min(0.99, float(c))) for c in conf_steps]
    _seen = set()
    _uniq = []
    for _c in conf_steps:
        _rc = round(float(_c), 4)
        if _rc in _seen:
            continue
        _seen.add(_rc)
        _uniq.append(float(_c))
    conf_steps = _uniq

    topk = int(cfg.get("fast_scan_topk", 3))
    min_dist = int(cfg.get("fast_scan_min_dist", 32))

    bad_std = float(cfg.get("fast_scan_bad_std", 2.0))
    bad_mean = float(cfg.get("fast_scan_bad_mean", 8.0))

    def _is_bad_capture(gray: "np.ndarray") -> bool:
        try:
            m = float(gray.mean())
            s = float(gray.std())
            return (s < bad_std) and (m < bad_mean)
        except Exception:
            return False

    def _build_from_gray(screen_gray: "np.ndarray", origin_xy: Tuple[int, int], shot_scale: float) -> Dict[str, List[Tuple[int, int, float]]]:
        pos_map: Dict[str, List[Tuple[int, int, float]]] = {}
        if screen_gray is None:
            return pos_map
        if _is_bad_capture(screen_gray):
            try:
                if bool(cfg.get("fast_scan_log_bad_capture", True)):
                    _m = float(screen_gray.mean())
                    _s = float(screen_gray.std())
                    log(f"FASTSCAN: bad capture (mean={_m:.1f} std={_s:.1f}) -> skip scan")
            except Exception:
                pass
            return pos_map

        ox, oy = int(origin_xy[0]), int(origin_xy[1])
        s = float(shot_scale) if shot_scale else 1.0
        if s <= 0:
            s = 1.0

        for tmpl_path in templates:
            try:
                if (tmpl_path is None) or (not str(tmpl_path).lower().endswith(".png")):
                    continue
                stem = Path(tmpl_path).stem

                tmpl, tmpl_mask = _tmpl_gray_mask_cached(tmpl_path, cfg)
                if tmpl is None:
                    continue

                th, tw = tmpl.shape[:2]
                if th < 6 or tw < 6:
                    continue
                sh, sw = screen_gray.shape[:2]
                if sh < th or sw < tw:
                    continue

                try:
                    if tmpl_mask is not None:
                        res = cv2.matchTemplate(screen_gray, tmpl, cv2.TM_CCORR_NORMED, mask=tmpl_mask)
                    else:
                        res = cv2.matchTemplate(screen_gray, tmpl, cv2.TM_CCOEFF_NORMED)
                except Exception:
                    continue

                best_centers: List[Tuple[int, int, float]] = []
                best_score = 0.0
                try:
                    if len(templates) <= 1:
                        tmpl_scales = cfg.get("fast_scan_template_scales_single", [1.0])
                    else:
                        tmpl_scales = cfg.get("fast_scan_template_scales_multi", [1.0])
                    if not isinstance(tmpl_scales, (list, tuple)) or not tmpl_scales:
                        tmpl_scales = [1.0]
                except Exception:
                    tmpl_scales = [1.0]

                for thr in conf_steps:
                    ys, xs = (res >= thr).nonzero()
                    if len(xs) == 0:
                        continue

                    scores = res[ys, xs]
                    order = scores.argsort()[::-1]
                    kept: List[Tuple[int, int, float]] = []

                    for k in order[:1500]:
                        x = int(xs[k]); y = int(ys[k])
                        sc = float(scores[k])
                        if sc > best_score:
                            best_score = sc
                        ok = True
                        for (cx0, cy0, _sc0) in kept:
                            if (abs(cx0 - x) <= min_dist) and (abs(cy0 - y) <= min_dist):
                                ok = False
                                break
                        if not ok:
                            continue
                        kept.append((x, y, sc))
                        if len(kept) >= topk:
                            break

                    if kept:
                        for (x, y, sc) in kept:
                            cx = ox + int((x + tw / 2.0) / s)
                            cy = oy + int((y + th / 2.0) / s)
                            best_centers.append((cx, cy, sc))
                        break

                if best_centers:
                    pos_map[stem] = best_centers
            except Exception:
                continue

        return pos_map

    def _build_from_shot(shot_img, origin_xy: Tuple[int, int], shot_scale: float) -> Dict[str, List[Tuple[int, int, float]]]:
        try:
            screen = np.array(shot_img)
            if screen.ndim == 3:
                gray = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY)
            else:
                gray = screen
            s = float(shot_scale) if shot_scale else 1.0
            if s != 1.0:
                gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
            return _build_from_gray(gray, origin_xy, shot_scale=s)
        except Exception:
            return {}

    try:
        if region:
            shot = pyautogui.screenshot(region=region)
            origin = (int(region[0]), int(region[1]))
        else:
            shot = pyautogui.screenshot()
            origin = (0, 0)
    except Exception:
        return {}

    pos_map = _build_from_shot(shot, origin, 1.0)

    if (not pos_map) and region and bool(cfg.get("fast_scan_expand_region", True)):
        try:
            expand_down = int(cfg.get("vehicle_region_expand_down", 520))
            expand_up = int(cfg.get("vehicle_region_expand_up", 0))
            x, y, w, h = region
            y2 = max(0, int(y - expand_up))
            h2 = int(h + expand_down + expand_up)
            region2 = (int(x), int(y2), int(w), int(h2))
            if region2 != region:
                shot2 = pyautogui.screenshot(region=region2)
                pos_map2 = _build_from_shot(shot2, (region2[0], region2[1]), 1.0)
                if pos_map2:
                    pos_map = pos_map2
                    region_used = region2
        except Exception:
            pass

    if (not pos_map) and bool(cfg.get("fast_scan_multiscale", True)):
        try:
            scales = cfg.get("fast_scan_scales", None)
            if not isinstance(scales, (list, tuple)) or not scales:
                scales = [0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30]
            for s in scales:
                try:
                    s = float(s)
                except Exception:
                    continue
                if not (0.50 <= s <= 1.60):
                    continue
                pos_map2 = _build_from_shot(shot, origin, s)
                if pos_map2:
                    pos_map = pos_map2
                    break
        except Exception:
            pass

    if (not pos_map) and region and bool(cfg.get("fast_scan_fallback_fullscreen", True)):
        try:
            min_n = int(cfg.get("fast_scan_fullscreen_min_templates", 2) or 2)
        except Exception:
            min_n = 2
        if len(templates) >= max(2, min_n):
            try:
                shot_fs = pyautogui.screenshot()
                pos_map2 = _build_from_shot(shot_fs, (0, 0), 1.0)
                if pos_map2:
                    pos_map = pos_map2
                    region_used = None
            except Exception:
                pass

    if pos_map:
        _metrics_set("fastscan_last_ts", time.time())
        _metrics_set("fastscan_visible", sum(1 for hits in pos_map.values() if hits))
        _metrics_set("fastscan_free", sum(1 for hits in pos_map.values() if not hits))
        with FAST_SCAN_LOCK:
            FAST_SCAN_CACHE["ts"] = time.time()
            FAST_SCAN_CACHE["region"] = region_used
            FAST_SCAN_CACHE["pos"] = pos_map

    return pos_map



def fast_scan_build(cfg: Dict[str, Any],
                    templates: List[Path],
                    use_fullscreen: bool = False,
                    region: Optional[Tuple[int, int, int, int]] = None) -> Dict[str, List[Tuple[int, int, float]]]:
    """One-screenshot CV scan for ALL templates.

    Goal: build FAST_SCAN_CACHE['pos'] as {stem: [(abs_cx, abs_cy, score), ...]}.
    It tries:
      1) vehicle_region screenshot (if set)
      1b) optional region expansion (down/up) to catch cropped lists
      1c) optional multi-scale scan by resizing the screenshot (templates unchanged)
      2) optional fullscreen fallback if region is bad / empty
    Optional: pass region to override cfg['vehicle_region'].
    """
    if pyautogui is None or (not HAS_OPENCV) or (cv2 is None) or (np is None):
        return {}
    if not bool(cfg.get("fast_scan_enabled", True)):
        return {}
    if bool(cfg.get("fast_scan_use_legacy_engine", False)):
        return _fast_scan_build_legacy(cfg, templates, use_fullscreen=use_fullscreen, region=region)

    if use_fullscreen:
        region = None
    elif region is not None:
        region = _region_to_tuple(region)
    else:
        region = _region_to_tuple(cfg.get("vehicle_region"))
    region_used = region

    # Confidence ladder (vehicle list is usually noisier than form fields).
    # Start with max(global confidence, vehicle_confidence) but allow stepping down lower:
    # we validate candidates again (compete / plate) before committing.
    try:
        base_conf = float(_clamp_conf(cfg))
    except Exception:
        base_conf = 0.91
    try:
        veh_conf = float(cfg.get("vehicle_confidence", base_conf))
    except Exception:
        veh_conf = base_conf
    conf0 = max(base_conf, veh_conf)

    try:
        fallback_min = float(cfg.get("fast_scan_fallback_min", max(0.62, conf0 - 0.28)))
    except Exception:
        fallback_min = max(0.62, conf0 - 0.28)

    conf_steps = [conf0, conf0 - 0.05, conf0 - 0.10, conf0 - 0.15, conf0 - 0.20, conf0 - 0.25]
    conf_steps = [max(fallback_min, min(0.99, float(c))) for c in conf_steps]
    # keep order, drop duplicates
    _seen = set()
    _uniq = []
    for _c in conf_steps:
        _rc = round(float(_c), 4)
        if _rc in _seen:
            continue
        _seen.add(_rc)
        _uniq.append(float(_c))
    conf_steps = _uniq

    topk = int(cfg.get("fast_scan_topk", 3))
    min_dist = int(cfg.get("fast_scan_min_dist", 32))
    parallel_enabled = bool(cfg.get("fast_scan_parallel", True))
    try:
        max_workers = int(cfg.get("fast_scan_workers", 0) or 0)
    except Exception:
        max_workers = 0
    if max_workers <= 0:
        max_workers = max(2, min(8, os.cpu_count() or 2))

    # "Bad capture" detection (solid/empty region)
    bad_std = float(cfg.get("fast_scan_bad_std", 2.0))
    bad_mean = float(cfg.get("fast_scan_bad_mean", 8.0))

    def _is_bad_capture(gray: "np.ndarray") -> bool:
        try:
            m = float(gray.mean())
            s = float(gray.std())
            # very low contrast + very dark (typical empty list / masked area)
            return (s < bad_std) and (m < bad_mean)
        except Exception:
            return False

    meta_map: Dict[str, Any] = {}
    meta_lock = threading.Lock()

    def _build_from_gray(screen_gray: "np.ndarray", origin_xy: Tuple[int, int], shot_scale: float) -> Dict[str, List[Tuple[int, int, float]]]:
        pos_map: Dict[str, List[Tuple[int, int, float]]] = {}
        if screen_gray is None:
            return pos_map
        if _is_bad_capture(screen_gray):
            try:
                if bool(cfg.get("fast_scan_log_bad_capture", True)):
                    _m = float(screen_gray.mean())
                    _s = float(screen_gray.std())
                    log(f"FASTSCAN: bad capture (mean={_m:.1f} std={_s:.1f}) -> skip scan")
            except Exception:
                pass
            return pos_map

        ox, oy = int(origin_xy[0]), int(origin_xy[1])
        s = float(shot_scale) if shot_scale else 1.0
        if s <= 0:
            s = 1.0

        def _scan_template(tmpl_path: Path) -> Optional[Tuple[str, List[Tuple[int, int, float]]]]:
            try:
                if (tmpl_path is None) or (not str(tmpl_path).lower().endswith(".png")):
                    return None
                stem = Path(tmpl_path).stem

                tmpl, tmpl_mask = _tmpl_gray_mask_cached(tmpl_path, cfg)
                mask_used = tmpl_mask is not None
                if tmpl is None:
                    return None

                best_centers: List[Tuple[int, int, float]] = []
                best_score = 0.0
                try:
                    if len(templates) <= 1:
                        tmpl_scales = cfg.get("fast_scan_template_scales_single", [1.0])
                    else:
                        tmpl_scales = cfg.get("fast_scan_template_scales_multi", [1.0])
                    if not isinstance(tmpl_scales, (list, tuple)) or not tmpl_scales:
                        tmpl_scales = [1.0]
                except Exception:
                    tmpl_scales = [1.0]

                for sc in tmpl_scales:
                    try:
                        scale = float(sc)
                    except Exception:
                        scale = 1.0
                    if scale <= 0:
                        continue
                    try:
                        if abs(scale - 1.0) < 1e-3:
                            t = tmpl
                            m = tmpl_mask
                        else:
                            t = cv2.resize(tmpl, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            if tmpl_mask is not None:
                                m = cv2.resize(tmpl_mask, (t.shape[1], t.shape[0]), interpolation=cv2.INTER_NEAREST)
                            else:
                                m = None
                    except Exception:
                        continue
                    th, tw = t.shape[:2]
                    if th < 6 or tw < 6:
                        continue
                    # matchTemplate requires screen >= template
                    sh, sw = screen_gray.shape[:2]
                    if sh < th or sw < tw:
                        continue

                    try:
                        if m is not None:
                            res = cv2.matchTemplate(screen_gray, t, cv2.TM_CCORR_NORMED, mask=m)
                        else:
                            res = cv2.matchTemplate(screen_gray, t, cv2.TM_CCOEFF_NORMED)
                    except Exception:
                        continue

                    # progressively relax confidence to avoid NOT FOUND loops
                    for thr in conf_steps:
                        ys, xs = (res >= thr).nonzero()
                        if len(xs) == 0:
                            continue

                        # sort by score desc
                        scores = res[ys, xs]
                        order = scores.argsort()[::-1]
                        kept: List[Tuple[int, int, float]] = []

                        for k in order[:1500]:
                            x = int(xs[k]); y = int(ys[k])
                            scv = float(scores[k])
                            if scv > best_score:
                                best_score = scv
                            # NMS / spacing
                            ok = True
                            for (cx0, cy0, _sc0) in kept:
                                if (abs(cx0 - x) <= min_dist) and (abs(cy0 - y) <= min_dist):
                                    ok = False
                                    break
                            if not ok:
                                continue
                            kept.append((x, y, scv))
                            if len(kept) >= topk:
                                break

                        if kept:
                            # Convert to ABS screen centers; map back for scaled shots
                            for (x, y, scv) in kept:
                                cx = ox + int((x + tw / 2.0) / s)
                                cy = oy + int((y + th / 2.0) / s)
                                best_centers.append((cx, cy, scv))
                            break
                    if best_centers:
                        break

                if best_centers:
                    return stem, best_centers
                with meta_lock:
                    meta_map[stem] = {
                        "best_score": float(best_score),
                        "fallback_min": float(fallback_min),
                        "conf0": float(conf0),
                        "mask": bool(mask_used),
                    }
                try:
                    log(
                        f"FASTSCAN[{stem}] scan: best_score={best_score:.4f} "
                        f"fallback_min={fallback_min:.2f} conf0={conf0:.2f} "
                        f"mask={'on' if mask_used else 'off'}"
                    )
                except Exception:
                    pass
                return None
            except Exception:
                return None

        do_parallel = parallel_enabled and len(templates) > 1 and max_workers > 1
        if do_parallel:
            worker_count = max(1, min(max_workers, len(templates)))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                for fut in as_completed([pool.submit(_scan_template, tmpl_path) for tmpl_path in templates]):
                    try:
                        result = fut.result()
                    except Exception:
                        continue
                    if result:
                        stem, best_centers = result
                        pos_map[stem] = best_centers
        else:
            for tmpl_path in templates:
                result = _scan_template(tmpl_path)
                if result:
                    stem, best_centers = result
                    pos_map[stem] = best_centers

        return pos_map

    def _build_from_shot(shot_img, origin_xy: Tuple[int, int], shot_scale: float) -> Dict[str, List[Tuple[int, int, float]]]:
        try:
            screen = np.array(shot_img)
            if screen.ndim == 3:
                gray = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY)
            else:
                gray = screen
            s = float(shot_scale) if shot_scale else 1.0
            if s != 1.0:
                gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
            return _build_from_gray(gray, origin_xy, shot_scale=s)
        except Exception:
            return {}

    # 1) region shot (or fullscreen)
    try:
        if region:
            shot = pyautogui.screenshot(region=region)
            origin = (int(region[0]), int(region[1]))
        else:
            shot = pyautogui.screenshot()
            origin = (0, 0)
    except Exception:
        return {}

    pos_map = _build_from_shot(shot, origin, 1.0)

    # 1b) If region is cropped, try expanding it downward/upward once (keeps origin consistent).
    if (not pos_map) and region and bool(cfg.get("fast_scan_expand_region", True)):
        try:
            expand_down = int(cfg.get("vehicle_region_expand_down", 520))
            expand_up = int(cfg.get("vehicle_region_expand_up", 0))
            x, y, w, h = region
            y2 = max(0, int(y - expand_up))
            h2 = int(h + expand_down + expand_up)
            region2 = (int(x), int(y2), int(w), int(h2))
            if region2 != region:
                shot2 = pyautogui.screenshot(region=region2)
                pos_map2 = _build_from_shot(shot2, (region2[0], region2[1]), 1.0)
                if pos_map2:
                    pos_map = pos_map2
                    region_used = region2
        except Exception:
            pass

    # 1c) Multi-scale fallback: scale the screenshot (not templates) and map coords back.
    if (not pos_map) and bool(cfg.get("fast_scan_multiscale", True)):
        try:
            scales = cfg.get("fast_scan_scales", None)
            if not isinstance(scales, (list, tuple)) or not scales:
                scales = [0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30]
            for s in scales:
                try:
                    s = float(s)
                except Exception:
                    continue
                if not (0.50 <= s <= 1.60):
                    continue
                pos_map2 = _build_from_shot(shot, origin, s)
                if pos_map2:
                    pos_map = pos_map2
                    break
        except Exception:
            pass

    # 2) fallback to fullscreen if region is mis-set / empty
    # 2) fallback to fullscreen if region is mis-set / empty
    if (not pos_map) and region and bool(cfg.get("fast_scan_fallback_fullscreen", True)):
        # Important: when scanning a single template, "no hits" is normal (vehicle not on screen).
        # Fullscreen fallback is expensive; only do it when scanning multiple templates (suggests region is wrong).
        try:
            min_n = int(cfg.get("fast_scan_fullscreen_min_templates", 2) or 2)
        except Exception:
            min_n = 2
        if len(templates) >= max(2, min_n):
            try:
                log("FASTSCAN: 0 usable hits in vehicle_region -> fallback to fullscreen once")
                shot2 = pyautogui.screenshot()
                pos_map = _build_from_shot(shot2, (0, 0), 1.0)
                region_used = None
            except Exception:
                pass
    with FAST_SCAN_LOCK:
        FAST_SCAN_CACHE["ts"] = time.time()
        FAST_SCAN_CACHE["region"] = region_used
        FAST_SCAN_CACHE["pos"] = pos_map
        FAST_SCAN_CACHE["meta"] = meta_map
    try:
        visible = sum(len(v) for v in pos_map.values()) if isinstance(pos_map, dict) else 0
        free = len([k for k, v in (pos_map or {}).items() if not v])
        _metrics_set("fastscan_last_ts", time.time())
        _metrics_set("fastscan_visible", visible)
        _metrics_set("fastscan_free", free)
    except Exception:
        pass
    return pos_map
# ---------- Loop hooks: pre-sweep fast scan + watchdog stall refresh ----------
WATCHDOG_STATE: Dict[str, Any] = {
    "last_hash": None,
    "last_change_ts": 0.0,
    "last_refresh_ts": 0.0,
}
_PRE_SWEEP_STATE: Dict[str, Any] = {"running": False}

def _img_hash_small(pil_img) -> str:
    """Fast perceptual-ish hash for watchdog; stable enough for 'page changed' detection."""
    try:
        import numpy as _np
        arr = _np.array(pil_img.convert("L").resize((64, 64)))
        return hashlib.md5(arr.tobytes()).hexdigest()
    except Exception:
        try:
            b = pil_img.tobytes()
            return hashlib.md5(b).hexdigest()
        except Exception:
            return str(time.time())

def loop_pre_sweep_hook(*args):
    """Build FAST_SCAN_CACHE from a single screenshot for all templates.

    Compatible with BOTH call styles:
      - new: (cfg, work_list, run_event, stop_event)
      - legacy: (work_list, cfg, run_event, stop_event)
    """
    try:
        if _PRE_SWEEP_STATE.get("running"):
            return
        _PRE_SWEEP_STATE["running"] = True
        if len(args) != 4:
            return
        a0, a1, run_event, stop_event = args
        # Detect argument order
        if isinstance(a0, dict):
            cfg = a0
            work_list = a1
        else:
            work_list = a0
            cfg = a1

        if stop_event.is_set():
            return
        if not cfg.get("fast_scan_enabled", True):
            return

        _park_mouse_for_scan(cfg)

        # Scan only current work_list to avoid recursion and reduce wasted cycles.
        fast_scan_build(cfg, list(work_list))
    except Exception as e:
        try:
            log(f"pre_sweep_hook error: {e}")
        except Exception:
            pass
    finally:
        _PRE_SWEEP_STATE["running"] = False


def loop_watchdog_tick(cfg, run_event, stop_event):
    """If stall_region doesn't change for N seconds -> refresh via Create->Rent sequence."""
    try:
        if stop_event.is_set():
            return
        if not cfg.get("watchdog_enabled", True):
            return
        region = _region_to_tuple(cfg.get("stall_region"))
        timeout_s = float(cfg.get("stall_timeout_s", cfg.get("watchdog_timeout_s", 25.0)))
        cooldown_s = float(cfg.get("stall_refresh_cooldown_s", cfg.get("watchdog_refresh_cooldown_s", 20.0)))
        if not region or pyautogui is None:
            return
        img = pyautogui.screenshot(region=region)
        h = _img_hash_small(img)
        now = time.time()
        if WATCHDOG_STATE["last_hash"] != h:
            WATCHDOG_STATE["last_hash"] = h
            WATCHDOG_STATE["last_change_ts"] = now
            return
        # unchanged
        if WATCHDOG_STATE["last_change_ts"] <= 0:
            WATCHDOG_STATE["last_change_ts"] = now
            return
        if (now - WATCHDOG_STATE["last_change_ts"]) < timeout_s:
            return
        if (now - float(WATCHDOG_STATE.get("last_refresh_ts", 0.0))) < cooldown_s:
            return
        WATCHDOG_STATE["last_refresh_ts"] = now
        log(f"WATCHDOG: stall>{timeout_s:.0f}s -> refresh via Create->Rent")
        enter_create_rent(cfg, run_event, stop_event)
    except Exception as e:
        try:
            log(f"WATCHDOG error: {e}")
        except Exception:
            pass

def fast_scan_get(stem: str) -> List[Tuple[int, int, float]]:
    with FAST_SCAN_LOCK:
        pos = FAST_SCAN_CACHE.get("pos", {}).get(stem)
        if not pos:
            return []
        return list(pos)


def fast_scan_visible_templates(cfg: Dict[str, Any], work_list: List[Path]) -> Optional[List[Path]]:
    if not bool(cfg.get("fast_scan_enabled", True)):
        return None
    try:
        with FAST_SCAN_LOCK:
            pos_map = dict(FAST_SCAN_CACHE.get("pos") or {})
    except Exception:
        return None
    if not pos_map:
        try:
            fast_scan_build(cfg, list(work_list))
        except Exception:
            return []
        try:
            with FAST_SCAN_LOCK:
                pos_map = dict(FAST_SCAN_CACHE.get("pos") or {})
        except Exception:
            return []
        if not pos_map:
            return []
    visible: List[Path] = []
    for tmpl in work_list:
        try:
            hits = pos_map.get(tmpl.stem)
        except Exception:
            hits = None
        if hits:
            visible.append(tmpl)
    return visible


def fast_scan_prebuild_current(cfg: Dict[str, Any],
                               tmpl_path: Path,
                               region: Optional[Tuple[int, int, int, int]] = None) -> None:
    """Ensure FASTSCAN cache contains fresh results for the current template."""
    if not bool(cfg.get("fast_scan_enabled", True)):
        return
    if not bool(cfg.get("fast_scan_prebuild_current", True)):
        return
    try:
        cache_max_age = float(cfg.get("fast_scan_prebuild_cache_max_age", 1.0))
    except Exception:
        cache_max_age = 1.0
    try:
        cache_ts = float(FAST_SCAN_CACHE.get("ts", 0.0))
    except Exception:
        cache_ts = 0.0
    try:
        pos_map = FAST_SCAN_CACHE.get("pos", {}) or {}
        has_stem = bool(pos_map.get(tmpl_path.stem))
    except Exception:
        has_stem = False
    if cache_max_age <= 0:
        stale = True
    elif cache_ts <= 0:
        stale = True
    else:
        stale = (time.time() - cache_ts) > cache_max_age
    if has_stem and not stale:
        return
    try:
        log(f"FASTSCAN prebuild: stem={tmpl_path.stem} stale={stale} has_stem={has_stem}")
    except Exception:
        pass
    if bool(cfg.get("fast_scan_prebuild_fullscreen_first", False)):
        try:
            pos_map = fast_scan_build(cfg, [tmpl_path], use_fullscreen=True)
        except Exception:
            pos_map = {}
        try:
            has_hits = bool(pos_map.get(tmpl_path.stem))
        except Exception:
            has_hits = False
        if has_hits:
            return
    try:
        pos_map = fast_scan_build(cfg, [tmpl_path], region=region)
    except Exception:
        return
    try:
        has_hits = bool(pos_map.get(tmpl_path.stem))
    except Exception:
        has_hits = False
    if has_hits:
        return
    if region and bool(cfg.get("fast_scan_prebuild_fullscreen_on_miss", True)):
        try:
            fast_scan_build(cfg, [tmpl_path], use_fullscreen=True)
        except Exception:
            return


def _fast_scan_has_any_hits() -> bool:
    try:
        with FAST_SCAN_LOCK:
            pos = FAST_SCAN_CACHE.get("pos", {})
            for hits in pos.values():
                if hits:
                    return True
    except Exception:
        return False
    return False


def wait_for_vehicle_list_ready(cfg: Dict[str, Any],
                                templates: List[Path],
                                run_event: threading.Event,
                                stop_event: threading.Event) -> bool:
    """Wait until FASTSCAN sees at least one vehicle template on screen."""
    if pyautogui is None or not HAS_OPENCV:
        return True
    if not bool(cfg.get("fast_scan_enabled", True)):
        return True
    if not templates:
        return True
    try:
        cache_age = float(cfg.get("vehicle_list_ready_cache_max_age", 1.2))
    except Exception:
        cache_age = 1.2
    if cache_age > 0:
        try:
            ts = float(FAST_SCAN_CACHE.get("ts", 0.0))
        except Exception:
            ts = 0.0
        if ts > 0 and (time.time() - ts) <= cache_age and _fast_scan_has_any_hits():
            return True
    try:
        timeout = float(cfg.get("vehicle_list_ready_timeout", 8.0))
    except Exception:
        timeout = 8.0
    try:
        poll = float(cfg.get("vehicle_list_ready_poll", 0.6))
    except Exception:
        poll = 0.6

    region = _region_to_tuple(cfg.get("vehicle_region"))
    bad_std = float(cfg.get("fast_scan_bad_std", 2.0))
    bad_mean = float(cfg.get("fast_scan_bad_mean", 8.0))
    try:
        empty_std_min = float(cfg.get("vehicle_list_ready_region_std_min", 4.0))
    except Exception:
        empty_std_min = 4.0
    try:
        empty_mean_min = float(cfg.get("vehicle_list_ready_region_mean_min", 10.0))
    except Exception:
        empty_mean_min = 10.0

    def _region_has_content() -> Tuple[Optional[bool], Optional[str]]:
        if region is None or pyautogui is None or np is None:
            return None, None
        try:
            img = pyautogui.screenshot(region=region)
            arr = np.array(img.convert("L"))
            if arr.size == 0:
                return False, "bad capture"
            mean = float(arr.mean())
            std = float(arr.std())
            if std < bad_std and mean < bad_mean:
                return False, "bad capture"
            if std < empty_std_min and mean < empty_mean_min:
                return False, "empty region"
            return True, None
        except Exception:
            return False, "bad capture"

    try:
        sample_n = int(cfg.get("vehicle_list_ready_sample", 6))
    except Exception:
        sample_n = 6
    if sample_n > 0 and len(templates) > sample_n:
        templates = templates[:sample_n]

    if bool(NAV_STATE.get("in_create_rent")) and not bool(NAV_STATE.get("list_ready_initial_delay_done", False)):
        delay = d(cfg, "vehicle_list_ready_initial_delay", 1.6)
        if delay > 0:
            sleep_coop(delay, run_event, stop_event)
        NAV_STATE["list_ready_initial_delay_done"] = True

    end = time.time() + max(0.5, timeout)
    last_reason: Optional[str] = None
    while time.time() < end and not stop_event.is_set():
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return False
        fast_scan_build(cfg, templates)
        if _fast_scan_has_any_hits():
            return True
        region_ready, region_reason = _region_has_content()
        if region_ready is True:
            return True
        if region_reason:
            last_reason = region_reason
        else:
            last_reason = "no hits"
        sleep_coop(poll, run_event, stop_event)

    if not stop_event.is_set():
        fast_scan_build(cfg, templates)
        if _fast_scan_has_any_hits():
            return True
        region_ready, region_reason = _region_has_content()
        if region_ready is True:
            return True
        if region_reason:
            last_reason = region_reason
        else:
            last_reason = "no hits"

    if bool(cfg.get("vehicle_list_ready_force_refresh", True)):
        log("VEHICLE LIST not detected -> force refresh Create->Rent")
        enter_create_rent(cfg, run_event, stop_event)
        sleep_coop(d(cfg, "post_ok_after_create_rent_delay", 1.2), run_event, stop_event)
        fast_scan_build(cfg, templates)
        if _fast_scan_has_any_hits():
            return True

    if last_reason:
        log(f"VEHICLE LIST wait timed out -> continue (may be on wrong page). reason={last_reason}")
    else:
        log("VEHICLE LIST wait timed out -> continue (may be on wrong page)")
    return False



# ---------------- FASTSCAN safety: reject false-positive matches by cross-template competition ----------------
_TEMPLATE_LIST_CACHE: Dict[str, Any] = {"dir": None, "ts": 0.0, "paths": []}
_TEMPLATE_IMG_CACHE: Dict[str, Any] = {}  # path -> (mtime, gray_ndarray, mask_ndarray_or_None, mask_ready)
_TEMPLATE_IMG_LOCK = threading.Lock()



def vehicle_autoscroll_find(cfg: Dict[str, Any], stem: str, tmpl_path: str, region: Tuple[int, int, int, int], run_event=None, stop_event=None) -> List[Tuple[int, int, float]]:
    """Try to find a vehicle by scrolling the vehicle list inside region.
    Returns candidates like fast_scan_build/fast_scan_get.
    Safe: scrolls back to original position when possible.
    """
    if not region:
        return []
    if not bool(cfg.get("vehicle_autoscroll_on_miss", True)):
        return []
    try:
        steps = int(cfg.get("vehicle_autoscroll_steps", 6) or 0)
    except Exception:
        steps = 6
    try:
        pixels = int(cfg.get("vehicle_autoscroll_pixels", 420) or 0)
    except Exception:
        pixels = 420
    try:
        delay = float(cfg.get("vehicle_autoscroll_delay", 0.18) or 0.0)
    except Exception:
        delay = 0.18
    reset = bool(cfg.get("vehicle_autoscroll_reset", True))

    if steps <= 0 or pixels <= 0:
        return []

    rx, ry, rw, rh = region
    cx = int(rx + rw / 2)
    cy = int(ry + rh / 2)

    tried = 0
    best: List[Tuple[int, int, float]] = []
    try:
        try:
            pyautogui.moveTo(cx, cy, duration=0.0)
        except Exception:
            pass
        for _ in range(steps):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            if run_event is not None and getattr(run_event, "is_set", None) and not run_event.is_set():
                # paused
                sleep_coop(0.1, run_event, stop_event)
                continue
            try:
                pyautogui.scroll(-abs(pixels))
            except Exception:
                break
            tried += 1
            sleep_coop(max(0.05, delay), run_event, stop_event)
            try:
                pos_map = fast_scan_build(cfg, [tmpl_path], region=region)
                cand = pos_map.get(stem, [])
            except Exception:
                cand = []
            if cand:
                best = cand
                break
        return best
    finally:
        if reset and tried:
            try:
                pyautogui.moveTo(cx, cy, duration=0.0)
            except Exception:
                pass
            for _ in range(tried):
                try:
                    pyautogui.scroll(abs(pixels))
                except Exception:
                    break
                sleep_coop(0.05, run_event, stop_event)

def _list_all_vehicle_templates(cfg: Dict[str, Any]) -> List[Path]:
    """Return list of *.png templates under cfg['car_dir'] (root only). Cached."""
    try:
        car_dir = Path(str(cfg.get("car_dir", r"C:\\sale\\car")))
    except Exception:
        car_dir = Path(r"C:\\sale\\car")
    if not car_dir.exists():
        return []
    now = time.time()
    with FAST_SCAN_LOCK:
        try:
            cached_dir = _TEMPLATE_LIST_CACHE.get("dir")
            cached_ts = float(_TEMPLATE_LIST_CACHE.get("ts", 0.0))
            if (cached_dir == str(car_dir)) and ((now - cached_ts) < 3.0) and _TEMPLATE_LIST_CACHE.get("paths"):
                return list(_TEMPLATE_LIST_CACHE["paths"])
        except Exception:
            pass
        try:
            paths = [p for p in car_dir.glob("*.png") if p.is_file()]
            # keep stable order
            paths = sorted(paths, key=lambda p: p.name.lower())
        except Exception:
            paths = []
        _TEMPLATE_LIST_CACHE["dir"] = str(car_dir)
        _TEMPLATE_LIST_CACHE["ts"] = now
        _TEMPLATE_LIST_CACHE["paths"] = paths
        return list(paths)


def _read_image_gray(path: str, bg_gray: int = 30) -> Optional["np.ndarray"]:
    """Read image as grayscale, handling alpha PNGs by compositing on a dark background."""
    if (not HAS_OPENCV) or (cv2 is None) or (np is None):
        return None
    try:
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        # Already grayscale
        if len(getattr(img, "shape", ())) == 2:
            return img
        # Composite alpha if present
        if img.shape[2] == 4:
            bgr = img[:, :, :3].astype(np.float32)
            a = (img[:, :, 3:4].astype(np.float32) / 255.0)
            bg = np.full_like(bgr, float(bg_gray), dtype=np.float32)
            comp = (bgr * a) + (bg * (1.0 - a))
            comp8 = np.clip(comp, 0, 255).astype(np.uint8)
            return cv2.cvtColor(comp8, cv2.COLOR_BGR2GRAY)
        # Regular BGR/RGB
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except Exception:
        return None


def _read_image_gray_with_mask(path: str,
                               bg_gray: int = 30,
                               alpha_thr: int = 10) -> Tuple[Optional["np.ndarray"], Optional["np.ndarray"]]:
    """Read image as grayscale + alpha mask (if present)."""
    if (not HAS_OPENCV) or (cv2 is None) or (np is None):
        return None, None
    try:
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None, None
        if len(getattr(img, "shape", ())) == 2:
            return img, None
        if img.shape[2] == 4:
            bgr = img[:, :, :3].astype(np.float32)
            a = (img[:, :, 3:4].astype(np.float32) / 255.0)
            bg = np.full_like(bgr, float(bg_gray), dtype=np.float32)
            comp = (bgr * a) + (bg * (1.0 - a))
            comp8 = np.clip(comp, 0, 255).astype(np.uint8)
            gray = cv2.cvtColor(comp8, cv2.COLOR_BGR2GRAY)
            try:
                thr = int(alpha_thr)
            except Exception:
                thr = 10
            mask = (img[:, :, 3] > thr).astype(np.uint8) * 255
            return gray, mask
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), None
    except Exception:
        return None, None


def _tmpl_gray_mask_cached(path: Path, cfg: Dict[str, Any]) -> Tuple[Optional["np.ndarray"], Optional["np.ndarray"]]:
    try:
        p = str(path)
        mtime = float(path.stat().st_mtime)
    except Exception:
        return None, None
    try:
        with _TEMPLATE_IMG_LOCK:
            rec = _TEMPLATE_IMG_CACHE.get(p)
        if rec and (float(rec[0]) == mtime) and rec[1] is not None:
            if len(rec) >= 4 and bool(rec[3]):
                return rec[1], rec[2]
            if len(rec) >= 3 and rec[2] is not None:
                return rec[1], rec[2]
    except Exception:
        pass
    try:
        bg_gray = int(cfg.get("fast_scan_template_bg_gray", 30))
    except Exception:
        bg_gray = 30
    try:
        alpha_thr = int(cfg.get("fast_scan_alpha_mask_thr", 10))
    except Exception:
        alpha_thr = 10
    try:
        gray, mask = _read_image_gray_with_mask(p, bg_gray=bg_gray, alpha_thr=alpha_thr)
        if gray is None:
            return None, None
        with _TEMPLATE_IMG_LOCK:
            _TEMPLATE_IMG_CACHE[p] = (mtime, gray, mask, True)
        return gray, mask
    except Exception:
        return None, None

def fast_scan_compete_check(cfg: Dict[str, Any], expected_stem: str, cx: int, cy: int) -> Tuple[bool, str, float, float]:
    """Cross-template check: is another template closer to (cx,cy) than expected_stem?

    FAST PATH: uses FAST_SCAN_CACHE from pre-sweep instead of taking a new screenshot.
    Only falls back to screenshot if cache is empty/stale.

    Returns: (ok, best_stem, best_score, expected_score)
      ok=True  -> safe to click this candidate for expected_stem
      ok=False -> another template matches this area better
    """
    if not bool(cfg.get("fast_scan_compete_enabled", True)):
        return True, expected_stem, 0.0, 0.0
    if pyautogui is None or (not HAS_OPENCV) or (cv2 is None) or (np is None):
        return True, expected_stem, 0.0, 0.0

    # Screenshot-based compete: crop area around candidate, match all templates
    try:
        w = int(cfg.get("fast_scan_compete_w", 320))
        h = int(cfg.get("fast_scan_compete_h", 220))
    except Exception:
        w, h = 320, 220
    w = max(220, min(1200, w))
    h = max(160, min(900, h))
    x0 = int(max(0, cx - w // 2))
    y0 = int(max(0, cy - h // 2))

    best_stem = expected_stem
    best_score = -1.0
    expected_score = -1.0
    cache_hit = False  # keep variable for threshold logic below

    try:
        shot = pyautogui.screenshot(region=(x0, y0, w, h))
        screen = np.array(shot)
        gray = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY) if screen.ndim == 3 else screen
    except Exception:
        return True, expected_stem, 0.0, 0.0

    paths = _list_all_vehicle_templates(cfg)
    if not paths:
        return True, expected_stem, 0.0, 0.0

    for tmpl_path in paths:
        try:
            stem = tmpl_path.stem
            tmpl = cv2.imread(str(tmpl_path), 0)
            if tmpl is None:
                continue
            th, tw = tmpl.shape[:2]
            gh, gw = gray.shape[:2]
            if gh < th or gw < tw:
                continue
            res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _minv, maxv, _minl, _maxl = cv2.minMaxLoc(res)
            sc = float(maxv)
            if sc > best_score:
                best_score = sc
                best_stem = stem
            if stem == expected_stem:
                expected_score = sc
        except Exception:
            continue

    if expected_score < 0:
        expected_score = 0.0
    if best_score < 0:
        best_score = 0.0

    # КЛЮЧЕВОЕ: если лучший шаблон в этой области — именно наш (тот же stem),
    # разрешаем клик безусловно. Даже если score низкий — никто остальной не лучше,
    # значит в этой области реально наша машина (или пустая область — в обоих случаях
    # клик безопасен).
    if best_stem == expected_stem:
        return True, best_stem, best_score, expected_score

    # Minimum expected match in the crop. If it's too low, reject to avoid "random" clicks.
    # NOTE: when using cache (no crop screenshot), scores are from full-screen matching
    # and are typically lower. Use a relaxed threshold for cache-based checks.
    try:
        expected_min = float(cfg.get("fast_scan_compete_expected_min", max(0.55, float(_clamp_conf(cfg)) - 0.25)))
    except Exception:
        expected_min = 0.55

    if expected_score < expected_min:
        return False, best_stem, best_score, expected_score

    # Decision: if something else is clearly better -> reject
    try:
        margin = float(cfg.get("fast_scan_compete_margin", 0.035))
    except Exception:
        margin = 0.035
    try:
        min_best = float(cfg.get("fast_scan_compete_min_best", 0.75))
    except Exception:
        min_best = 0.75

    if best_stem != expected_stem and (best_score >= min_best) and ((best_score - expected_score) >= margin):
        return False, best_stem, best_score, expected_score
    return True, best_stem, best_score, expected_score


def wait_for_form_ready(cfg: Dict[str, Any],
                        run_event: threading.Event, stop_event: threading.Event) -> bool:
    sleep_coop(d(cfg, "after_vehicle_click_delay", 2.8), run_event, stop_event)
    if stop_event.is_set():
        return False

    if not cfg.get("form_anchor_enabled", True):
        return True

    if not ANCHOR_FORM_PATH.exists():
        return True

    timeout = float(cfg.get("form_anchor_timeout", 8.0))
    poll = float(cfg.get("form_anchor_poll", 0.35)) / _speed(cfg)
    t0 = time.time()

    while time.time() - t0 <= timeout and not stop_event.is_set():
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return False



        # Try multiple confidences + grayscale modes (anchor template can be scale/AA sensitive)
        found = None
        try:
            base_conf = float(cfg.get('form_anchor_conf', min(float(cfg.get('confidence', 0.94)), 0.90)))
        except Exception:
            base_conf = 0.94
        confs = [base_conf, max(0.70, base_conf - 0.04), max(0.70, base_conf - 0.08)]
        grays = [bool(cfg.get('grayscale', True)), False]

        for conf_try in confs:
            for gray_try in grays:
                try:
                    if bool(cfg.get('use_confidence', True)) and HAS_OPENCV:
                        box = pyautogui.locateOnScreen(str(ANCHOR_FORM_PATH), confidence=conf_try, grayscale=gray_try)
                    else:
                        box = pyautogui.locateOnScreen(str(ANCHOR_FORM_PATH), grayscale=gray_try)
                    if box:
                        c = pyautogui.center(box)
                        found = (int(c.x), int(c.y))
                        break
                except Exception:
                    found = None
            if found:
                break

        if found:
            return True

    # Fallback heuristic: if plate_region is visible and non-empty, consider form ready.
    # This is intentionally lenient: a tiny label on dark background can have low std.
    if bool(cfg.get('form_anchor_fallback_plate', True)):
        pr = _region_to_tuple(cfg.get('plate_region'))
        if pr:
            try:
                imgp = pyautogui.screenshot(region=pr)
                arr = np.array(imgp.convert('L'))
                if arr.size > 0:
                    std = float(arr.std())
                    mx = float(arr.max())
                    mean = float(arr.mean())
                    if (std >= float(cfg.get('plate_ready_std_min', 4.0)) and mx >= float(cfg.get('plate_ready_max_min', 25.0))) or                            (mean >= float(cfg.get('plate_ready_mean_min', 10.0)) and mx >= float(cfg.get('plate_ready_max_min', 25.0))):
                        return True
            except Exception:
                pass

    sleep_coop(poll, run_event, stop_event)
    log("FORM READY wait timed out (anchor not found) -> abort this item to avoid random clicks")
    return False



# ---------------- vehicle blacklist capture helper ----------------
def match_capture_to_templates(cfg: Dict[str, Any], cap_img: "Image.Image") -> Tuple[Optional[str], float]:
    """Try to identify which template stem matches a user-captured region (card/name)."""
    if not HAS_OPENCV or np is None or cap_img is None:
        return (None, 0.0)

    try:
        cap = cv2.cvtColor(np.array(cap_img), cv2.COLOR_RGB2GRAY)
    except Exception:
        return (None, 0.0)

    car_dir = Path(cfg.get("car_dir") or DEFAULT_CONFIG.get("car_dir") or ".")
    try:
        tmpl_paths = [p for p in car_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    except Exception:
        tmpl_paths = []

    if not tmpl_paths:
        return (None, 0.0)

    # scales: allow mild multiscale because players sometimes capture with slightly different UI scale
    multiscale = bool(cfg.get("multiscale", True))
    scales = [1.0]
    if multiscale:
        scales = [0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15]

    best_stem = None
    best_score = 0.0

    ch, cw = cap.shape[:2]
    for p in tmpl_paths:
        try:
            tmpl = _read_image_gray(str(p))
        except Exception:
            tmpl = None
        if tmpl is None:
            continue

        for s in scales:
            try:
                if s == 1.0:
                    t = tmpl
                else:
                    tw = max(8, int(tmpl.shape[1] * s))
                    th = max(8, int(tmpl.shape[0] * s))
                    t = cv2.resize(tmpl, (tw, th), interpolation=cv2.INTER_AREA)
                th, tw = t.shape[:2]
                if ch < th or cw < tw:
                    continue
                res = cv2.matchTemplate(cap, t, cv2.TM_CCOEFF_NORMED)
                _minv, maxv, _minl, _maxl = cv2.minMaxLoc(res)
                if maxv > best_score:
                    best_score = float(maxv)
                    best_stem = p.stem
            except Exception:
                continue

    return (best_stem, best_score)

# ---------------- file structure helpers ----------------
def list_templates(cfg: Dict[str, Any]) -> List[Path]:
    car_dir = Path(cfg["car_dir"])
    bl = set([str(x).strip() for x in (cfg.get("blacklist_vehicles") or []) if str(x).strip()])
    try:
        files = [p for p in car_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
        if bl:
            files = [p for p in files if p.stem not in bl]
        return sorted(files)
    except Exception as e:
        log(f"list_templates error: {e}")
        return []



def list_vehicle_stems_from_car(cfg: Dict[str, Any]) -> List[str]:
    """Return vehicle stems based on /car: one PNG template AND matching folder.
    This is the single source of truth for 'available vehicles' in UI/Stats.
    """
    try:
        car_dir = Path(cfg["car_dir"])
    except Exception:
        return []
    stems: List[str] = []
    try:
        for p in car_dir.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() != ".png":
                continue
            stem = p.stem
            if not stem or stem.startswith("_"):
                continue
            if (car_dir / stem).is_dir():
                stems.append(stem)
    except Exception:
        return []
    return sorted(set(stems))



def item_folder(cfg: Dict[str, Any], stem: str) -> Path:
    return Path(cfg["car_dir"]) / stem


def _sanitize_text(text: str) -> str:
    if text is None:
        return ""
    # Remove NULs and non-printable control chars except \n and \t.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _read_text_with_fallback(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return _sanitize_text(path.read_text(encoding=enc))
        except UnicodeDecodeError:
            continue
        except Exception as e:
            log(f"read_text error {path}: {e}")
            return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        log(f"read_text warning: fallback decode(ignore) used for {path}")
        return _sanitize_text(text)
    except Exception as e:
        log(f"read_text error {path}: {e}")
        return ""


def _find_case_insensitive_file(folder: Path, filename: str) -> Optional[Path]:
    try:
        target = filename.lower()
        for p in folder.iterdir():
            if not p.is_file():
                continue
            if p.name.lower() == target:
                return p
    except Exception:
        return None
    return None


def read_text(folder: Path, base: str) -> str:
    p1 = folder / f"{base}.txt"
    p2 = folder / base
    chosen: Optional[Path] = None
    if p1.exists():
        chosen = p1
    elif p2.exists():
        chosen = p2
    else:
        p1_ci = _find_case_insensitive_file(folder, f"{base}.txt")
        if p1_ci is not None:
            chosen = p1_ci
        else:
            p2_ci = _find_case_insensitive_file(folder, base)
            if p2_ci is not None:
                chosen = p2_ci

    if chosen is None:
        log(f"read_text miss: {folder} base={base}")
        return ""
    text = _read_text_with_fallback(chosen)
    log(f"read_text ok: {chosen} base={base} len={len(text)}")
    return text


def find_photo(folder: Path, idx: int) -> Optional[Path]:
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = folder / f"{idx}{ext}"
        if p.exists():
            return p
    return None


def is_valid_item(cfg: Dict[str, Any], stem: str) -> bool:
    folder = item_folder(cfg, stem)
    if not folder.exists():
        return False
    has_desc = (folder / "description.txt").exists() or (folder / "description").exists()
    has_price = (folder / "price.txt").exists() or (folder / "price").exists()
    has_photos = all(find_photo(folder, i) is not None for i in (1, 2, 3))
    return has_desc and has_price and has_photos


# ---------------- duplicate guard by photo hash ----------------
def compute_photo1_hash(cfg: Dict[str, Any], stem: str) -> Optional[str]:
    folder = item_folder(cfg, stem)
    p1 = find_photo(folder, 1)
    if not p1:
        return None
    try:
        return md5_file(p1)
    except Exception as e:
        log(f"hash error {stem}: {e}")
        return None


def is_duplicate_hash(photo_hashes: Dict[str, str], h: str) -> bool:
    return h in set(photo_hashes.values())


# ---------------- dialog ops ----------------

def _fg_window_info() -> Tuple[str, str]:
    """Return (class_name, title) for the current foreground window (Windows)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        # class
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        # title
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        return cls_buf.value or "", title_buf.value or ""
    except Exception:
        return "", ""


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]


def _is_probably_file_dialog(cls: str, title: str) -> bool:
    """Heuristic: detect Windows file open dialog / explorer picker."""
    cls = (cls or "").strip()
    title = (title or "").strip()
    if cls in ("#32770", "CabinetWClass", "ExploreWClass"):
        return True
    t = title.lower()
    if any(k in t for k in ("open", "откры", "выберите", "choose", "select")):
        return True
    return False


def _normalize_dialog_path_text(text: str) -> str:
    text = (text or "").strip().strip('"')
    text = text.replace("/", "\\")
    return text.lower()


def _focus_dialog_path_bar(cfg: Dict[str, Any],
                           run_event: threading.Event, stop_event: threading.Event) -> None:
    try:
        click_xy(cfg["coords"]["select_path"], run_event, stop_event)
        sleep_coop(d(cfg, "file_dialog_after_click_path", 0.25), run_event, stop_event)
    except Exception:
        pass


def _clear_path_field_backspace(cfg: Dict[str, Any],
                                run_event: threading.Event, stop_event: threading.Event) -> None:
    if pyautogui is None:
        return
    try:
        presses = int(cfg.get("file_dialog_clear_backspace_count", 120))
    except Exception:
        presses = 120
    try:
        interval = float(cfg.get("file_dialog_clear_backspace_delay", 0.005))
    except Exception:
        interval = 0.005
    pyautogui.press("backspace", presses=max(1, presses), interval=max(0.0, interval))


def _force_dialog_latin_layout(cfg: Dict[str, Any]) -> None:
    if not bool(cfg.get("file_dialog_force_latin_layout", True)):
        return
    if not sys.platform.startswith("win"):
        return
    target_hex = str(cfg.get("layout_switch_latin_target_hkl", "00000409"))
    try:
        if _activate_keyboard_layout(target_hex):
            time.sleep(0.03)
            return
    except Exception:
        pass
    _force_layout_switch(cfg)


def _focus_dialog_filename_field(cfg: Dict[str, Any],
                                 run_event: Optional[threading.Event] = None,
                                 stop_event: Optional[threading.Event] = None) -> bool:
    coords = cfg.get("file_dialog_filename_coords")
    if isinstance(coords, (list, tuple)) and len(coords) == 2:
        try:
            clicks = int(cfg.get("file_dialog_filename_clicks", 2))
        except Exception:
            clicks = 2
        try:
            delay = float(cfg.get("file_dialog_filename_focus_delay", 0.06)) / _speed(cfg)
        except Exception:
            delay = 0.06
        for _ in range(max(1, clicks)):
            click_xy([int(coords[0]), int(coords[1])], run_event, stop_event)
            time.sleep(max(0.01, delay))
        return True
    if bool(cfg.get("file_dialog_filename_use_hotkey", False)):
        try:
            keys = cfg.get("file_dialog_filename_hotkey", ["alt", "n"])
            if pyautogui is not None and isinstance(keys, (list, tuple)) and keys:
                pyautogui.hotkey(*[str(k).lower() for k in keys])
                time.sleep(0.03 / _speed(cfg))
                return True
        except Exception:
            pass
    if bool(cfg.get("file_dialog_filename_tab_focus_enabled", True)) and pyautogui is not None:
        try:
            pre_click = cfg.get("file_dialog_filename_tab_focus_click_coords")
            if isinstance(pre_click, (list, tuple)) and len(pre_click) == 2:
                click_xy([int(pre_click[0]), int(pre_click[1])], run_event, stop_event)
                sleep_coop(d(cfg, "file_dialog_after_click_path", 0.25), run_event, stop_event)
            tabs = int(cfg.get("file_dialog_filename_tab_count", 6))
            reverse = bool(cfg.get("file_dialog_filename_tab_reverse", False))
            if reverse:
                pyautogui.hotkey("shift", "tab")
                for _ in range(max(0, tabs - 1)):
                    pyautogui.hotkey("shift", "tab")
            else:
                pyautogui.press("tab", presses=max(1, tabs), interval=0.02 / _speed(cfg))
            time.sleep(0.03 / _speed(cfg))
            return True
        except Exception:
            pass
    log("DIALOG: filename coords missing and no hotkey/tab focus -> cannot focus filename field")
    return False


def _verify_dialog_path(folder_path: str, cfg: Dict[str, Any]) -> Optional[bool]:
    if pyautogui is None or pyperclip is None:
        return None
    old_clip = _safe_clip_get()
    try:
        if not _safe_clip_set(""):
            return None
        pyautogui.hotkey("alt", "d")
        time.sleep(0.05 / _speed(cfg))
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.05 / _speed(cfg))
        got = _safe_clip_get()
    finally:
        _safe_clip_set(old_clip)
    if not got:
        return None
    return _normalize_dialog_path_text(got) == _normalize_dialog_path_text(folder_path)


def _read_dialog_filename_field_text(cfg: Dict[str, Any]) -> Optional[str]:
    if pyautogui is None or pyperclip is None:
        return _winapi_read_dialog_filename_text()
    old_clip = _safe_clip_get()
    try:
        if not _safe_clip_set("__WV_FILENAME_VERIFY__"):
            return _winapi_read_dialog_filename_text()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(max(0.01, 0.02 / _speed(cfg)))
        pyautogui.hotkey("ctrl", "c")
        time.sleep(max(0.01, 0.02 / _speed(cfg)))
        got = _safe_clip_get()
    finally:
        _safe_clip_set(old_clip)
    if got is None:
        return _winapi_read_dialog_filename_text()
    got = (got or "").strip()
    if got == "__WV_FILENAME_VERIFY__":
        win_text = _winapi_read_dialog_filename_text()
        return "" if win_text is None else win_text
    return got


def _winapi_read_dialog_filename_text() -> Optional[str]:
    if not sys.platform.startswith("win"):
        return None
    hwnd = _winapi_find_dialog_filename_edit()
    if not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return (buf.value or "").strip()
    except Exception:
        return None


def _winapi_set_focused_text(text: str, cfg: Dict[str, Any]) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return False
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", ctypes.c_long * 4),
        ]
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
        return False
    hwnd = gti.hwndFocus
    if not hwnd:
        return False
    WM_SETTEXT = 0x000C
    result = user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text))
    time.sleep(max(0.01, 0.02 / _speed(cfg)))
    return bool(result)


def _winapi_find_dialog_filename_edit() -> Optional[int]:
    if not sys.platform.startswith("win"):
        return None
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None
    hwnd_fg = user32.GetForegroundWindow()
    if not hwnd_fg:
        return None

    handles: List[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_child(hwnd, _lparam) -> bool:
        handles.append(hwnd)
        return True

    try:
        user32.EnumChildWindows(hwnd_fg, _enum_child, 0)
    except Exception:
        return None

    def _class_name(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        try:
            user32.GetClassNameW(hwnd, buf, 256)
        except Exception:
            return ""
        return buf.value or ""

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def _is_visible(hwnd: int) -> bool:
        try:
            return bool(user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    def _rect(hwnd: int) -> Optional[RECT]:
        r = RECT()
        try:
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return r
        except Exception:
            return None
        return None

    edits = [h for h in handles if _class_name(h) == "Edit" and _is_visible(h)]
    combo_edits: List[int] = []
    label_texts = cfg.get("file_dialog_filename_label_texts") or ["File name", "Имя файла"]
    label_texts = [str(t).strip().lower() for t in (label_texts or []) if str(t).strip()]
    for h in handles:
        if _class_name(h) != "ComboBoxEx32":
            continue
        sub_handles: List[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_sub(hwnd, _lparam) -> bool:
            sub_handles.append(hwnd)
            return True

        try:
            user32.EnumChildWindows(h, _enum_sub, 0)
        except Exception:
            continue
        for sub in sub_handles:
            if _class_name(sub) == "Edit" and _is_visible(sub):
                combo_edits.append(sub)
    def _window_text(hwnd: int) -> str:
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return (buf.value or "").strip()
        except Exception:
            return ""

    def _center_y(r: RECT) -> int:
        return int((r.top + r.bottom) / 2)

    label_rects: List[RECT] = []
    if label_texts:
        for h in handles:
            if _class_name(h) != "Static":
                continue
            if not _is_visible(h):
                continue
            text = _window_text(h).lower()
            if not text:
                continue
            if any(lbl in text for lbl in label_texts):
                r = _rect(h)
                if r is not None:
                    label_rects.append(r)

    if label_rects:
        best: Optional[Tuple[int, int]] = None
        for h in edits + combo_edits:
            r = _rect(h)
            if r is None:
                continue
            for lr in label_rects:
                same_row = abs(_center_y(r) - _center_y(lr)) <= 6
                right_side = r.left >= lr.right - 6
                if not (same_row and right_side):
                    continue
                dist = abs(r.left - lr.right) + abs(_center_y(r) - _center_y(lr))
                if best is None or dist < best[1]:
                    best = (h, dist)
        if best is not None:
            return best[0]

    candidates: List[Tuple[int, int]] = []
    for h in edits + combo_edits:
        r = _rect(h)
        if r is None:
            continue
        candidates.append((h, r.top))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def _winapi_set_dialog_filename_text(text: str, cfg: Dict[str, Any]) -> bool:
    if not sys.platform.startswith("win"):
        return False
    hwnd = _winapi_find_dialog_filename_edit()
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return False
    WM_SETTEXT = 0x000C
    result = user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text))
    time.sleep(max(0.01, 0.02 / _speed(cfg)))
    return bool(result)


def ensure_dialog_folder(cfg: Dict[str, Any], folder_path: str,
                         run_event: threading.Event, stop_event: threading.Event,
                         reason: str = "reuse_check") -> bool:
    """Ensure the dialog is in the expected folder before picking a file."""
    if not wait_for_file_dialog(cfg, run_event, stop_event):
        log("DIALOG: ensure folder failed (picker not foreground)")
        return False
    verify = _verify_dialog_path(folder_path, cfg)
    if verify is True:
        return True
    if verify is None:
        if bool(cfg.get("file_dialog_force_navigate_on_unknown", True)):
            log(f"DIALOG: path verify unavailable -> force navigate ({reason})")
            return navigate_dialog_to_folder(cfg, folder_path, run_event, stop_event)
        return True
    log(f"DIALOG: path mismatch -> force navigate ({reason})")
    return navigate_dialog_to_folder(cfg, folder_path, run_event, stop_event)


def wait_for_file_dialog(cfg: Dict[str, Any],
                         run_event: threading.Event, stop_event: threading.Event) -> bool:
    """Wait until the file dialog becomes the foreground window."""
    if not bool(cfg.get("file_dialog_wait_foreground", True)):
        return True
    try:
        timeout = float(cfg.get("file_dialog_wait_timeout", 10.0))
    except Exception:
        timeout = 10.0
    end = time.time() + max(0.5, timeout)
    while (time.time() < end) and (not stop_event.is_set()):
        if run_event is not None and (not run_event.is_set()):
            sleep_coop(0.12, run_event, stop_event)
            continue
        cls, title = _fg_window_info()
        if _is_probably_file_dialog(cls, title):
            return True
        sleep_coop(0.06, run_event, stop_event)
    return False


def wait_for_file_dialog_close(cfg: Dict[str, Any],
                               run_event: threading.Event, stop_event: threading.Event) -> bool:
    """Wait until file dialog is no longer foreground (after selecting a file)."""
    if not bool(cfg.get("file_dialog_wait_close", True)):
        return True
    try:
        timeout = float(cfg.get("file_dialog_wait_close_timeout", 6.0))
    except Exception:
        timeout = 6.0
    end = time.time() + max(0.5, timeout)
    while (time.time() < end) and (not stop_event.is_set()):
        if run_event is not None and (not run_event.is_set()):
            sleep_coop(0.12, run_event, stop_event)
            continue
        cls, title = _fg_window_info()
        if not _is_probably_file_dialog(cls, title):
            return True
        sleep_coop(0.06, run_event, stop_event)
    return False

def open_file_dialog(cfg: Dict[str, Any], first: bool,
                     run_event: threading.Event, stop_event: threading.Event) -> bool:
    key = "files_prev" if first else "files"
    click_xy(cfg["coords"][key], run_event, stop_event)

    # Old behavior was pure sleep; that can race when Explorer takes longer to appear.
    sleep_coop(d(cfg, "file_dialog_open_delay", 2.2), run_event, stop_event)

    if wait_for_file_dialog(cfg, run_event, stop_event):
        return not stop_event.is_set()

    # Retry once (sometimes the first click is swallowed / webview lag).
    log("DIALOG: file picker not foreground yet -> retry open")
    click_xy(cfg["coords"][key], run_event, stop_event)
    sleep_coop(d(cfg, "file_dialog_open_retry_delay", 2.4), run_event, stop_event)

    ok = wait_for_file_dialog(cfg, run_event, stop_event)
    if not ok:
        log("DIALOG: FAILED to open file picker (timeout) -> abort to avoid wrong photos")
    return ok and (not stop_event.is_set())


def navigate_dialog_to_folder(cfg: Dict[str, Any], folder_path: str,
                              run_event: threading.Event, stop_event: threading.Event) -> bool:
    if not wait_for_file_dialog(cfg, run_event, stop_event):
        log("DIALOG: not ready (picker not foreground) -> abort")
        return False
    entered_by_type = False

    def _focus_and_clear() -> None:
        _focus_dialog_path_bar(cfg, run_event, stop_event)
        _clear_path_field_backspace(cfg, run_event, stop_event)
        sleep_coop(d(cfg, "file_dialog_after_clear_path", 0.12), run_event, stop_event)

    def _type_path(use_clipboard: bool, allow_forced_paste: bool) -> Tuple[bool, str]:
        typed = False
        method = "none"
        ascii_only = False
        try:
            ascii_only = all(ord(ch) < 128 for ch in folder_path)
        except Exception:
            ascii_only = False
        prefer_clipboard = bool(cfg.get("file_dialog_prefer_clipboard", True))
        clipboard_retries = int(cfg.get("file_dialog_clipboard_retries", 2))
        force_click_type = bool(cfg.get("file_dialog_force_click_type_path", False))
        if force_click_type:
            nonlocal entered_by_type
            if bool(cfg.get("file_dialog_force_address_hotkeys_before_type", True)) and pyautogui is not None:
                try:
                    pyautogui.hotkey("alt", "d")
                except Exception:
                    pass
                try:
                    pyautogui.hotkey("ctrl", "l")
                except Exception:
                    pass
                sleep_coop(0.04 / _speed(cfg), run_event, stop_event)
            try:
                clicks = int(cfg.get("file_dialog_clicks_before_type", 2))
            except Exception:
                clicks = 2
            for _ in range(max(1, clicks)):
                click_xy(cfg["coords"]["select_path"], run_event, stop_event)
                sleep_coop(d(cfg, "file_dialog_after_click_path", 0.25), run_event, stop_event)
            if bool(cfg.get("file_dialog_use_winapi_settext_path", True)) and _winapi_set_focused_text(folder_path, cfg):
                if pyautogui is None:
                    return False, "winapi_settext_no_enter"
                if bool(cfg.get("file_dialog_winapi_clear_selection", True)):
                    pyautogui.press("right")
                if bool(cfg.get("file_dialog_winapi_focus_before_enter", True)):
                    try:
                        pyautogui.hotkey("ctrl", "l")
                    except Exception:
                        pass
                sleep_coop(d(cfg, "file_dialog_winapi_enter_delay", 0.12), run_event, stop_event)
                pyautogui.press("enter")
                sleep_coop(d(cfg, "file_dialog_winapi_after_enter_delay", 0.25), run_event, stop_event)
                if bool(cfg.get("file_dialog_winapi_enter_twice", True)):
                    pyautogui.press("enter")
                sleep_coop(d(cfg, "file_dialog_winapi_folder_wait", 1.0), run_event, stop_event)
                entered_by_type = True
                return True, "winapi_settext_path"
            _force_dialog_latin_layout(cfg)
            char_delay = max(0.005, float(cfg.get("type_char_delay", 0.02)) / _speed(cfg))
            if bool(cfg.get("file_dialog_use_unicode_input", False)) and _type_text_unicode(folder_path, char_delay):
                pass
            elif pyautogui is not None:
                pyautogui.write(folder_path, interval=char_delay)
            else:
                type_text_fallback(folder_path, cfg, run_event, stop_event, layout="latin")
            sleep_coop(d(cfg, "file_dialog_after_type_path", 0.20), run_event, stop_event)
            if bool(cfg.get("file_dialog_force_click_type_verify", True)):
                verify_after_type = _verify_dialog_path(folder_path, cfg)
                if verify_after_type is not True and bool(cfg.get("file_dialog_force_click_type_fallback_paste", True)):
                    log("DIALOG: click-type verify failed -> paste fallback")
                    _focus_dialog_path_bar(cfg, run_event, stop_event)
                    if pyautogui is not None:
                        try:
                            pyautogui.hotkey("alt", "d")
                        except Exception:
                            pass
                        try:
                            pyautogui.hotkey("ctrl", "l")
                        except Exception:
                            pass
                        sleep_coop(0.04 / _speed(cfg), run_event, stop_event)
                    _clear_path_field_backspace(cfg, run_event, stop_event)
                    sleep_coop(d(cfg, "file_dialog_after_clear_path", 0.12), run_event, stop_event)
                    pasted = paste_text_verified(folder_path, cfg)
                    if not pasted and bool(cfg.get("paste_allow_unverified", True)):
                        pasted = paste_text_unverified(folder_path, cfg)
                    sleep_coop(d(cfg, "file_dialog_path_post_paste_delay", 0.18), run_event, stop_event)
            if pyautogui is not None:
                pyautogui.press("enter")
            entered_by_type = True
            return True, "click_type_enter"
        if use_clipboard and bool(cfg.get("ignore_keyboard_layout", True)) and pyautogui is not None and pyperclip is not None:
            if paste_text_verified(folder_path, cfg):
                typed = True
                method = "paste_verified"
            elif bool(cfg.get("paste_allow_unverified", True)) and paste_text_unverified(folder_path, cfg):
                typed = True
                method = "paste_unverified"
        if (not typed and allow_forced_paste
                and bool(cfg.get("file_dialog_force_paste_no_verify", True))
                and pyautogui is not None and pyperclip is not None):
            try:
                pyautogui.hotkey("alt", "d")
            except Exception:
                pass
            try:
                pyautogui.hotkey("ctrl", "l")
            except Exception:
                pass
            sleep_coop(0.04 / _speed(cfg), run_event, stop_event)
            try:
                clicks = int(cfg.get("file_dialog_select_path_clicks", 2))
            except Exception:
                clicks = 2
            for _ in range(max(1, clicks)):
                click_xy(cfg["coords"]["select_path"], run_event, stop_event)
                sleep_coop(d(cfg, "file_dialog_after_click_path", 0.25), run_event, stop_event)
            old_clip = _safe_clip_get()
            try:
                if _safe_clip_set(folder_path):
                    try:
                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.press("backspace")
                    except Exception:
                        pass
                    if bool(cfg.get("file_dialog_use_shift_insert", True)):
                        pyautogui.hotkey("shift", "insert")
                    else:
                        pyautogui.hotkey("ctrl", "v")
                    sleep_coop(0.04 / _speed(cfg), run_event, stop_event)
                    typed = True
                    method = "forced_paste"
                    log("DIALOG: path forced paste (no verify)")
            finally:
                _safe_clip_set(old_clip)
        allow_layout_write = bool(cfg.get("file_dialog_allow_layout_sensitive_write", False))
        allow_ascii_write = bool(cfg.get("file_dialog_allow_layout_insensitive_write_ascii", True)) and ascii_only
        allow_fallback_typing = bool(cfg.get("file_dialog_allow_fallback_typing", False))
        if not typed and (not prefer_clipboard or allow_fallback_typing) and bool(cfg.get("file_dialog_force_direct_write", True)) and pyautogui is not None:
            if bool(cfg.get("ignore_keyboard_layout", True)) and not (allow_layout_write or allow_ascii_write):
                log("DIALOG: direct write blocked by layout -> fallback typing")
                typed = False
            else:
                try:
                    pyautogui.hotkey("alt", "d")
                except Exception:
                    pass
                try:
                    pyautogui.hotkey("ctrl", "l")
                except Exception:
                    pass
                sleep_coop(0.04 / _speed(cfg), run_event, stop_event)
                try:
                    pyautogui.hotkey("ctrl", "a")
                    pyautogui.press("backspace")
                except Exception:
                    pass
                try:
                    pyautogui.write(folder_path, interval=max(0.005, float(cfg.get("type_char_delay", 0.02)) / _speed(cfg)))
                    typed = True
                    method = "direct_write_ascii" if allow_ascii_write else "direct_write"
                    log("DIALOG: path direct write used")
                except Exception:
                    typed = False
        if not typed:
            _force_layout_switch(cfg)
            type_text_fallback(folder_path, cfg, run_event, stop_event)
            method = "fallback_type"
        return typed, method

    def _save_dialog_debug(tag: str) -> None:
        if not bool(cfg.get("file_dialog_save_debug", True)):
            return
        if pyautogui is None:
            return
        try:
            dbg_dir = cfg.get("file_dialog_debug_dir") or str(USER_DIR)
            dbg_path = Path(str(dbg_dir))
            dbg_path.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out = dbg_path / f"file_dialog_debug_{tag}_{stamp}.png"
            pyautogui.screenshot(str(out))
            log(f"DIALOG: saved debug screenshot -> {out}")
        except Exception:
            pass

    verify_enabled = bool(cfg.get("file_dialog_verify_path", False))
    strict_verify = bool(cfg.get("file_dialog_verify_strict", False))
    sanity_check = bool(cfg.get("file_dialog_sanity_check_path", True))
    if bool(cfg.get("file_dialog_force_click_type_path", False)):
        verify_enabled = False
        sanity_check = False
        strict_verify = False
    max_attempts = 3 if verify_enabled else (2 if sanity_check else 1)
    prefer_clipboard = bool(cfg.get("file_dialog_prefer_clipboard", True))
    retry_clipboard = bool(cfg.get("file_dialog_retry_clipboard_on_verify_fail", True))

    verify = None
    last_typed_method = "none"
    for attempt in range(max_attempts):
        if stop_event.is_set():
            return False
        _focus_and_clear()
        pre_type_delay = d(cfg, "file_dialog_path_pre_type_delay", 0.18)
        if pre_type_delay > 0:
            sleep_coop(pre_type_delay, run_event, stop_event)
        use_clipboard = (attempt == 0)
        if prefer_clipboard and retry_clipboard:
            use_clipboard = True
        if attempt >= 1:
            if verify_enabled:
                log("DIALOG: verify failed -> retry with clipboard")
            else:
                log("DIALOG: sanity check -> retry typing without clipboard")
        allow_forced_paste = (attempt == 0)
        _typed, last_typed_method = _type_path(use_clipboard, allow_forced_paste)
        if _typed and last_typed_method == "winapi_settext_path":
            sleep_coop(d(cfg, "file_dialog_winapi_before_pick_delay", 1.0), run_event, stop_event)
        if attempt == 0:
            log(f"DIALOG: navigate to folder -> {folder_path}")

        sleep_coop(d(cfg, "file_dialog_path_post_type_delay", 0.12), run_event, stop_event)

        if not (verify_enabled or sanity_check):
            break

        verify = _verify_dialog_path(folder_path, cfg)
        if verify is True:
            break
        if verify is None and (verify_enabled or sanity_check):
            log("DIALOG: path verification unavailable -> retry focus/insert")
            _save_dialog_debug("verify_none")
            if attempt + 1 < max_attempts:
                continue
            break
        if attempt + 1 < max_attempts:
            log(f"DIALOG: path not set (attempt {attempt + 1}/{max_attempts}) -> retry focus/insert")

    if verify is False:
        log("DIALOG: path verification failed after retries")
        _save_dialog_debug("verify_false")
        if strict_verify:
            return False

    # Commit folder navigation.
    if not entered_by_type:
        try:
            if pyautogui is not None:
                pyautogui.press("enter")
        except Exception:
            pass

    sleep_coop(d(cfg, "file_dialog_after_enter_path", 1.80), run_event, stop_event)

    # Some systems/dialogs need a second enter (or a bit more time) for the list to refresh.
    if bool(cfg.get("file_dialog_enter_twice", True)):
        try:
            if pyautogui is not None:
                pyautogui.press("enter")
        except Exception:
            pass
        sleep_coop(d(cfg, "file_dialog_after_enter_path_second", 0.35), run_event, stop_event)

    # Give the dialog a moment to repaint the file list before we click a file slot.
    sleep_coop(d(cfg, "file_dialog_after_navigate_before_pick", 0.95), run_event, stop_event)
    return not stop_event.is_set()

def pick_file(cfg: Dict[str, Any], idx: int,
              run_event: threading.Event, stop_event: threading.Event,
              file_path: Optional[Path] = None,
              use_filename_field: bool = False,
              filename_only: bool = False) -> bool:
    key = "file_one" if idx == 1 else ("file_two" if idx == 2 else "file_three")
    log(f"DIALOG: pick file idx={idx} via {key}")

    if not wait_for_file_dialog(cfg, run_event, stop_event):
        log("DIALOG: pick requested but picker not foreground -> abort")
        return False

    if use_filename_field and file_path:
        try:
            if not _focus_dialog_filename_field(cfg, run_event, stop_event):
                raise RuntimeError("filename_focus_failed")
            if bool(cfg.get("file_dialog_filename_clear_before_paste", True)):
                if pyautogui is not None:
                    pyautogui.hotkey("ctrl", "a")
                    pyautogui.press("backspace")
            path_text = str(file_path)
            char_delay = max(0.005, float(cfg.get("type_char_delay", 0.02)) / _speed(cfg))
            typed_path = False
            if (bool(cfg.get("file_dialog_filename_use_winapi_settext", True))
                    and (_winapi_set_dialog_filename_text(path_text, cfg)
                         or _winapi_set_focused_text(path_text, cfg))):
                typed_path = True
            elif bool(cfg.get("file_dialog_filename_use_unicode_input", True)) and _type_text_unicode(path_text, char_delay):
                typed_path = True
            elif pyautogui is not None:
                pyautogui.write(path_text, interval=char_delay)
                typed_path = True
            else:
                type_text_fallback(path_text, cfg, run_event, stop_event, layout="latin")
                typed_path = True
            if not typed_path:
                old_clip = _safe_clip_get()
                try:
                    if _safe_clip_set(path_text):
                        if pyautogui is not None:
                            pyautogui.hotkey("ctrl", "v")
                        typed_path = True
                        sleep_coop(d(cfg, "file_dialog_filename_post_paste_delay", 0.12), run_event, stop_event)
                finally:
                    _safe_clip_set(old_clip)
            if bool(cfg.get("file_dialog_filename_verify", True)):
                got = _read_dialog_filename_field_text(cfg)
                if (got is None) or (not got) or (_normalize_dialog_path_text(got) != _normalize_dialog_path_text(path_text)):
                    log("DIALOG: filename field verify failed -> force paste")
                    if pyautogui is not None:
                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.press("backspace")
                    old_clip = _safe_clip_get()
                    try:
                        if _safe_clip_set(path_text):
                            if pyautogui is not None:
                                pyautogui.hotkey("ctrl", "v")
                            sleep_coop(d(cfg, "file_dialog_filename_post_paste_delay", 0.12), run_event, stop_event)
                    finally:
                        _safe_clip_set(old_clip)
                    got = _read_dialog_filename_field_text(cfg)
                    if (got is None) or (not got) or (_normalize_dialog_path_text(got) != _normalize_dialog_path_text(path_text)):
                        log("DIALOG: filename field still empty/mismatch after paste")
                        if filename_only:
                            return False
            sleep_coop(d(cfg, "file_dialog_filename_post_paste_delay", 0.12), run_event, stop_event)
            if bool(cfg.get("file_dialog_filename_confirm_enter", True)):
                pyautogui.press("enter")
            sleep_coop(d(cfg, "file_dialog_after_open", 1.35), run_event, stop_event)
            if wait_for_file_dialog_close(cfg, run_event, stop_event):
                return True
            log("DIALOG: filename field did not close picker -> fallback to click")
        except Exception:
            log("DIALOG: filename field selection failed -> fallback to click")
        if filename_only:
            log("DIALOG: filename-only requested -> abort before click fallback")
            return False

    # Click the file row/slot and let the selection highlight apply.
    click_xy(cfg["coords"][key], run_event, stop_event)
    sleep_coop(d(cfg, "file_dialog_after_pick_click", 0.35), run_event, stop_event)

    # Open/Select (click button + enter as extra confirm)
    click_xy(cfg["coords"]["open_file"], run_event, stop_event)
    sleep_coop(d(cfg, "file_dialog_after_click_open", 0.10), run_event, stop_event)
    try:
        if pyautogui is not None:
            pyautogui.press("enter")
    except Exception:
        pass

    sleep_coop(d(cfg, "file_dialog_after_open", 1.35), run_event, stop_event)

    # Ensure the picker actually closed; otherwise next keystrokes can land in the game
    # or the next upload will reuse the previous folder/file.
    if not wait_for_file_dialog_close(cfg, run_event, stop_event):
        log("DIALOG: picker did not close after Open -> extra confirm (Enter)")
        try:
            if pyautogui is not None:
                pyautogui.press("enter")
        except Exception:
            pass
        sleep_coop(d(cfg, "file_dialog_after_open_confirm", 0.55), run_event, stop_event)
        wait_for_file_dialog_close(cfg, run_event, stop_event)

    return not stop_event.is_set()

# ---------------- core posting ----------------

# ---------- Plate validation (image-based, no OCR) ----------
PLATE_LAST: Dict[str, Any] = {
    "stem": None,
    "score": None,
    "ref_path": None,
    "live_pil": None,
}

def _grab_plate_live(cfg: Dict[str, Any]):
    """Grab a WIDE plate line region (should include 'Гос.Номер:' + the value).
    User sets cfg['plate_region'] approximately. We then find the label anchor and crop the value.
    """
    if pyautogui is None:
        return None
    region = _region_to_tuple(cfg.get("plate_region"))
    if region is None:
        return None
    try:
        return pyautogui.screenshot(region=region)
    except Exception:
        return None


# Cache for the label anchor image ("Гос.Номер:" only) used to stabilize plate cropping
_PLATE_LABEL_ANCHOR_CACHE: Dict[str, Any] = {"mtime": 0.0, "gray": None, "w": 0, "h": 0}
_PLATE_OCR_DEDUPE: Dict[str, Any] = {}


def _load_plate_label_anchor_gray():
    """Load plate label anchor (gray) from PLATE_LABEL_ANCHOR_PATH with caching."""
    if not HAS_OPENCV:
        return None
    try:
        p = PLATE_LABEL_ANCHOR_PATH
        if not p.exists():
            return None
        mtime = p.stat().st_mtime
        if _PLATE_LABEL_ANCHOR_CACHE.get("gray") is not None and float(_PLATE_LABEL_ANCHOR_CACHE.get("mtime", 0.0)) == float(mtime):
            g = _PLATE_LABEL_ANCHOR_CACHE["gray"]
            w = int(_PLATE_LABEL_ANCHOR_CACHE.get("w", 0))
            h = int(_PLATE_LABEL_ANCHOR_CACHE.get("h", 0))
            if g is not None and w > 0 and h > 0:
                return g, w, h
        img = _read_image_gray(str(p))
        if img is None:
            return None
        h, w = img.shape[:2]
        _PLATE_LABEL_ANCHOR_CACHE.update({"mtime": mtime, "gray": img, "w": w, "h": h})
        return img, w, h
    except Exception:
        return None


def _estimate_bg_level(gray):
    """Median intensity of border pixels."""
    try:
        h, w = gray.shape[:2]
        if h < 4 or w < 4:
            return float(np.median(gray))
        border = np.concatenate([gray[0, :], gray[h-1, :], gray[:, 0], gray[:, w-1]], axis=0)
        return float(np.median(border))
    except Exception:
        return 0.0


def _trim_bbox(gray, thr: int = 18):
    """Tight bbox for pixels that differ from background."""
    try:
        bg = _estimate_bg_level(gray)
        diff = np.abs(gray.astype(np.int16) - int(bg)).astype(np.int16)
        mask = diff > int(thr)
        ys, xs = np.where(mask)
        if xs.size < 10 or ys.size < 10:
            return None
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        pad = 2
        h, w = gray.shape[:2]
        x0 = max(0, x0 - pad)
        y0 = max(0, y0 - pad)
        x1 = min(w - 1, x1 + pad)
        y1 = min(h - 1, y1 + pad)
        if x1 - x0 < 6 or y1 - y0 < 6:
            return None
        return x0, y0, x1 + 1, y1 + 1
    except Exception:
        return None


def _extract_plate_value_from_line(cfg: Dict[str, Any], line_pil, allow_fallback: bool = True):
    """Given screenshot of plate_region (label+value), crop VALUE using label anchor.
    Returns (pil_or_None, debug_dict). If allow_fallback and label not found -> returns (line_pil, dbg).
    """
    dbg: Dict[str, Any] = {"label_found": False, "label_score": 0.0, "value_box": None, "reason": ""}
    if line_pil is None:
        dbg["reason"] = "no_line"
        return (line_pil if allow_fallback else None), dbg
    if not HAS_OPENCV:
        dbg["reason"] = "no_opencv"
        return (line_pil if allow_fallback else None), dbg

    anchor = _load_plate_label_anchor_gray()
    if not anchor:
        dbg["reason"] = "no_label_anchor"
        if allow_fallback:
            try:
                ratio = float(cfg.get("plate_value_fallback_right_ratio", 0.62))
            except Exception:
                ratio = 0.62
            try:
                pad_y = int(cfg.get("plate_value_fallback_pad_y", 3) or 0)
            except Exception:
                pad_y = 3
            ratio = max(0.2, min(0.9, ratio))
            try:
                w, h = line_pil.size
                x0 = max(0, int(w * (1.0 - ratio)))
                y0 = max(0, pad_y)
                x1 = w
                y1 = max(y0 + 6, h - pad_y)
                if x1 - x0 >= 10 and y1 - y0 >= 10:
                    val_pil = line_pil.crop((x0, y0, x1, y1))
                    dbg["value_box"] = (x0, y0, x1, y1)
                    dbg["reason"] = "fallback_right_crop(no_label_anchor)"
                    if bool(cfg.get("plate_value_trim_fallback", False)):
                        thr2 = int(cfg.get("plate_value_trim_thr", 18) or 18)
                        val_gray = cv2.cvtColor(np.array(val_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
                        bb = _trim_bbox(val_gray, thr=thr2)
                        if bb:
                            val_pil = val_pil.crop(bb)
                    return val_pil, dbg
            except Exception:
                pass
        return (line_pil if allow_fallback else None), dbg

    try:
        a_gray, aw, ah = anchor
        line_rgb = np.array(line_pil.convert("RGB"))
        line_gray = cv2.cvtColor(line_rgb, cv2.COLOR_RGB2GRAY)
        if line_gray.shape[0] < ah or line_gray.shape[1] < aw:
            dbg["reason"] = "line_smaller_than_anchor"
            return (line_pil if allow_fallback else None), dbg

        scales = cfg.get("plate_label_match_scales", [1.0])
        if not isinstance(scales, (list, tuple)):
            scales = [1.0]
        best = (-1.0, (0, 0), a_gray, aw, ah, 1.0)
        for scale in scales:
            try:
                scale = float(scale)
            except Exception:
                continue
            if scale <= 0:
                continue
            if abs(scale - 1.0) < 1e-3:
                scaled = a_gray
                saw, sah = aw, ah
            else:
                saw = max(1, int(round(aw * scale)))
                sah = max(1, int(round(ah * scale)))
                if saw < 4 or sah < 4:
                    continue
                if line_gray.shape[0] < sah or line_gray.shape[1] < saw:
                    continue
                scaled = cv2.resize(a_gray, (saw, sah), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(line_gray, scaled, cv2.TM_CCOEFF_NORMED)
            _minv, maxv, _minloc, maxloc = cv2.minMaxLoc(res)
            if float(maxv) > float(best[0]):
                best = (float(maxv), maxloc, scaled, saw, sah, scale)
        maxv, maxloc, _best_anchor, aw, ah, best_scale = best
        thr = float(cfg.get("plate_label_confidence", 0.45))
        dbg["label_score"] = float(maxv)
        dbg["label_scale"] = float(best_scale)
        if maxv < thr:
            dbg["reason"] = f"label_score_below_thr({maxv:.3f}<{thr:.3f})"
            if allow_fallback:
                try:
                    ratio = float(cfg.get("plate_value_fallback_right_ratio", 0.62))
                except Exception:
                    ratio = 0.62
                try:
                    pad_y = int(cfg.get("plate_value_fallback_pad_y", 3) or 0)
                except Exception:
                    pad_y = 3
                ratio = max(0.2, min(0.9, ratio))
                try:
                    w, h = line_pil.size
                    x0 = max(0, int(w * (1.0 - ratio)))
                    y0 = max(0, pad_y)
                    x1 = w
                    y1 = max(y0 + 6, h - pad_y)
                    if x1 - x0 >= 10 and y1 - y0 >= 10:
                        val_pil = line_pil.crop((x0, y0, x1, y1))
                        dbg["value_box"] = (x0, y0, x1, y1)
                        dbg["reason"] = "fallback_right_crop(label_score_low)"
                        if bool(cfg.get("plate_value_trim_fallback", False)):
                            thr2 = int(cfg.get("plate_value_trim_thr", 18) or 18)
                            val_gray = cv2.cvtColor(np.array(val_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
                            bb = _trim_bbox(val_gray, thr=thr2)
                            if bb:
                                val_pil = val_pil.crop(bb)
                        return val_pil, dbg
                except Exception:
                    pass
            return (line_pil if allow_fallback else None), dbg

        dbg["label_found"] = True
        lx, ly = int(maxloc[0]), int(maxloc[1])

        gap = int(cfg.get("plate_value_gap", 6) or 0)
        pad_y = int(cfg.get("plate_value_pad_y", 3) or 0)
        max_w = cfg.get("plate_value_max_w", None)
        try:
            max_w = int(max_w) if (max_w is not None and int(max_w) > 0) else None
        except Exception:
            max_w = None

        x0 = max(0, lx + aw + gap)
        y0 = max(0, ly - pad_y)
        y1 = min(line_gray.shape[0], ly + ah + pad_y)
        x1 = line_gray.shape[1] if max_w is None else min(line_gray.shape[1], x0 + max_w)

        if x1 - x0 < 10 or y1 - y0 < 10:
            dbg["reason"] = "value_box_too_small"
            return (line_pil if allow_fallback else None), dbg

        val_pil = line_pil.crop((x0, y0, x1, y1))
        dbg["value_box"] = (x0, y0, x1, y1)

        if bool(cfg.get("plate_value_trim", True)):
            thr2 = int(cfg.get("plate_value_trim_thr", 18) or 18)
            val_gray = cv2.cvtColor(np.array(val_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
            bb = _trim_bbox(val_gray, thr=thr2)
            if bb:
                val_pil = val_pil.crop(bb)

        return val_pil, dbg
    except Exception as e:
        dbg["reason"] = f"exception:{e}"
        return (line_pil if allow_fallback else None), dbg


def _grab_plate_value_live(cfg: Dict[str, Any]):
    line = _grab_plate_live(cfg)
    if line is None:
        return None, {"reason": "no_plate_region"}
    val, dbg = _extract_plate_value_from_line(cfg, line, allow_fallback=True)
    return val, dbg


def _grab_plate_value_and_line(cfg: Dict[str, Any]):
    line = _grab_plate_live(cfg)
    if line is None:
        return None, {"reason": "no_plate_region"}, None
    val, dbg = _extract_plate_value_from_line(cfg, line, allow_fallback=True)
    return val, dbg, line


def _plate_similarity(a_pil, b_pil) -> float:
    if not HAS_OPENCV:
        return 0.0
    try:
        a = cv2.cvtColor(np.array(a_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
        b = cv2.cvtColor(np.array(b_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
        target_w, target_h = 240, 48
        a = cv2.resize(a, (target_w, target_h), interpolation=cv2.INTER_AREA)
        b = cv2.resize(b, (target_w, target_h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        _minv, maxv, _minloc, _maxloc = cv2.minMaxLoc(res)
        if maxv != maxv:
            return 0.0
        return float(maxv)
    except Exception:
        return 0.0


def plate_match_score(cfg: Dict[str, Any], ref_path: Path, live_img=None) -> float:
    """Compare current plate VALUE to reference image.
    If PLATE_LABEL_ANCHOR_PATH exists, we anchor on 'Гос.Номер:' and crop VALUE on both sides (best-effort).
    live_img may be provided (already-captured live value crop).
    """
    if not HAS_OPENCV:
        return 0.0
    try:
        ref_img = Image.open(ref_path)
    except Exception:
        return 0.0

    # live
    if live_img is None:
        live_img, _dbg = _grab_plate_value_live(cfg)
    if live_img is None:
        return 0.0

    # ref (try extract value if ref still includes label)
    ref_val, _dbg2 = _extract_plate_value_from_line(cfg, ref_img, allow_fallback=True)
    ref_use = ref_val if ref_val is not None else ref_img

    return _plate_similarity(live_img, ref_use)



def plate_validate(cfg: dict, folder: Path, stem: Optional[str] = None, sched: Optional[dict] = None) -> bool:
    """Validate that the current form's plate line matches this vehicle's reference plate_ref.png.
    Returns True if validation passes (or not required), False if it fails."""
    try:
        if sched is None:
            try:
                sched_p = Path(folder) / "schedule.json"
                sched = json.loads(sched_p.read_text(encoding="utf-8")) if sched_p.exists() else {}
            except Exception:
                sched = {}

        plate_text = ""
        try:
            plate_text = str(sched.get("plate_text", "")).strip()
        except Exception:
            plate_text = ""

        if plate_text:
            try:
                vehicle_key = plate_registry.get_vehicle_by_plate(plate_text)
                if vehicle_key:
                    try:
                        log(f"[{stem or folder.name}] plate registry: {plate_text} -> {vehicle_key}")
                    except Exception:
                        pass
            except Exception as e:
                try:
                    log(f"[{stem or folder.name}] plate registry error: {e}")
                except Exception:
                    pass

        required = bool(sched.get("plate_require", False))
        if not required:
            return True

        ref_path = Path(folder) / "plate_ref.png"
        if not ref_path.exists():
            log(f"[{stem or folder.name}] plate validation required but plate_ref.png is missing -> SKIP to be safe")
            return False

        thr = float(cfg.get("plate_confidence", 0.82))
        score, val_ref, val_live = plate_match_score(cfg, ref_path)

        def _norm(v: Optional[str]) -> str:
            if not v:
                return ""
            v = v.upper()
            v = re.sub(r"\s+", "", v)
            v = re.sub(r"[^0-9A-ZА-Я]", "", v)
            return v

        nref = _norm(val_ref)
        nlive = _norm(val_live)

        ok = False
        if nref and nlive and (nref == nlive):
            ok = True
        elif score >= thr and nlive:
            # Fallback: score-based (useful if extraction is imperfect)
            ok = True

        if ok:
            log(f"[{stem or folder.name}] plate OK (score={score:.2f}, ref='{val_ref}', live='{val_live}')")
            return True

        log(f"[{stem or folder.name}] plate MISMATCH (score={score:.2f} < {thr:.2f}, ref='{val_ref}', live='{val_live}') -> SKIP")
        return False
    except Exception as e:
        log(f"[{stem or folder.name}] plate validate error -> SKIP to be safe: {e}")
        return False

def _configure_plate_reader_env(cfg: Dict[str, Any], prompt_override: Optional[bool] = None) -> None:
    try:
        if prompt_override is None:
            prompt = bool(cfg.get("plate_read_prompt_on_fail", True))
        else:
            prompt = bool(prompt_override)
        os.environ["PLATE_READ_PROMPT_ON_FAIL"] = "1" if prompt else "0"
    except Exception:
        pass
    try:
        scale = cfg.get("plate_read_ocr_scale")
        if scale is not None:
            os.environ["PLATE_READ_OCR_SCALE"] = str(int(scale))
    except Exception:
        pass
    try:
        pad = cfg.get("plate_read_ocr_pad")
        if pad is not None:
            os.environ["PLATE_READ_OCR_PAD"] = str(int(pad))
    except Exception:
        pass
    try:
        lang = cfg.get("plate_read_ocr_lang")
        if lang:
            os.environ["PLATE_READ_OCR_LANG"] = str(lang)
    except Exception:
        pass
    try:
        psm_list = cfg.get("plate_read_psm_list")
        if psm_list:
            if isinstance(psm_list, (list, tuple)):
                os.environ["PLATE_READ_OCR_PSM_LIST"] = ",".join(str(v) for v in psm_list)
            else:
                os.environ["PLATE_READ_OCR_PSM_LIST"] = str(psm_list)
    except Exception:
        pass
    try:
        thresholds = cfg.get("plate_read_thresholds")
        if thresholds:
            if isinstance(thresholds, (list, tuple)):
                os.environ["PLATE_READ_OCR_THRESHOLDS"] = ",".join(str(v) for v in thresholds)
            else:
                os.environ["PLATE_READ_OCR_THRESHOLDS"] = str(thresholds)
    except Exception:
        pass
    try:
        loose_enabled = cfg.get("plate_read_loose_enabled")
        if loose_enabled is not None:
            os.environ["PLATE_READ_OCR_LOOSE_ENABLED"] = "1" if bool(loose_enabled) else "0"
    except Exception:
        pass
    try:
        loose_psm_list = cfg.get("plate_read_loose_psm_list")
        if loose_psm_list:
            if isinstance(loose_psm_list, (list, tuple)):
                os.environ["PLATE_READ_OCR_LOOSE_PSM_LIST"] = ",".join(str(v) for v in loose_psm_list)
            else:
                os.environ["PLATE_READ_OCR_LOOSE_PSM_LIST"] = str(loose_psm_list)
    except Exception:
        pass
    try:
        loose_lang = cfg.get("plate_read_loose_lang")
        if loose_lang:
            os.environ["PLATE_READ_OCR_LOOSE_LANG"] = str(loose_lang)
    except Exception:
        pass
    try:
        loose_config = cfg.get("plate_read_loose_config")
        if loose_config is not None:
            os.environ["PLATE_READ_OCR_LOOSE_CONFIG"] = str(loose_config)
    except Exception:
        pass
    try:
        data_enabled = cfg.get("plate_read_ocr_data_enabled")
        if data_enabled is not None:
            os.environ["PLATE_READ_OCR_DATA_ENABLED"] = "1" if bool(data_enabled) else "0"
    except Exception:
        pass
    try:
        data_psm_list = cfg.get("plate_read_ocr_data_psm_list")
        if data_psm_list:
            if isinstance(data_psm_list, (list, tuple)):
                os.environ["PLATE_READ_OCR_DATA_PSM_LIST"] = ",".join(str(v) for v in data_psm_list)
            else:
                os.environ["PLATE_READ_OCR_DATA_PSM_LIST"] = str(data_psm_list)
    except Exception:
        pass
    try:
        data_config = cfg.get("plate_read_ocr_data_config")
        if data_config is not None:
            os.environ["PLATE_READ_OCR_DATA_CONFIG"] = str(data_config)
    except Exception:
        pass
    try:
        ttl = cfg.get("plate_read_manual_cache_ttl")
        if ttl is not None:
            os.environ["PLATE_READ_MANUAL_CACHE_TTL"] = str(float(ttl))
    except Exception:
        pass


def _configure_runtime_env(cfg: Dict[str, Any]) -> None:
    try:
        tg_enabled = bool(cfg.get("tg_tracker_cfg", {}).get("enabled", False))
        os.environ["TG_TRACKER_ENABLED"] = "1" if tg_enabled else "0"
    except Exception:
        pass
    try:
        plate_enabled = bool(cfg.get("plate_registry_cfg", {}).get("enabled", False))
        os.environ["PLATE_REGISTRY_ENABLED"] = "1" if plate_enabled else "0"
    except Exception:
        pass


def _log_plate_ocr_params(cfg: Dict[str, Any],
                          plate_region: Optional[Tuple[int, int, int, int]],
                          image,
                          source: str) -> None:
    try:
        if image is not None:
            try:
                w, h = image.size
                crop_size = f"{w}x{h}"
            except Exception:
                crop_size = "unknown"
        else:
            crop_size = "none"
        modes = {
            "plate_read_ocr_scale": cfg.get("plate_read_ocr_scale"),
            "plate_read_ocr_pad": cfg.get("plate_read_ocr_pad"),
            "plate_read_psm_list": cfg.get("plate_read_psm_list"),
            "plate_read_thresholds": cfg.get("plate_read_thresholds"),
            "plate_read_prompt_on_fail": cfg.get("plate_read_prompt_on_fail"),
            "plate_read_use_value_crop_first": cfg.get("plate_read_use_value_crop_first"),
            "plate_read_side_crop_enabled": cfg.get("plate_read_side_crop_enabled"),
            "plate_read_side_crop_ratio": cfg.get("plate_read_side_crop_ratio"),
            "plate_read_autocrop_enabled": cfg.get("plate_read_autocrop_enabled"),
            "plate_read_autocrop_min_area_ratio": cfg.get("plate_read_autocrop_min_area_ratio"),
            "plate_read_autocrop_min_height_ratio": cfg.get("plate_read_autocrop_min_height_ratio"),
            "plate_read_autocrop_pad": cfg.get("plate_read_autocrop_pad"),
            "plate_read_full_line_fallback": cfg.get("plate_read_full_line_fallback"),
            "plate_read_save_line_debug": cfg.get("plate_read_save_line_debug"),
            "plate_read_loose_enabled": cfg.get("plate_read_loose_enabled"),
            "plate_read_loose_psm_list": cfg.get("plate_read_loose_psm_list"),
            "plate_read_loose_lang": cfg.get("plate_read_loose_lang"),
            "plate_read_loose_config": cfg.get("plate_read_loose_config"),
            "plate_read_ocr_data_enabled": cfg.get("plate_read_ocr_data_enabled"),
            "plate_read_ocr_data_psm_list": cfg.get("plate_read_ocr_data_psm_list"),
            "plate_read_ocr_data_config": cfg.get("plate_read_ocr_data_config"),
        }
        log(f"PLATE: OCR params src={source} plate_region={plate_region} crop={crop_size} modes={modes}")
    except Exception:
        pass


def _plate_autocrop_text_region(image, cfg: Dict[str, Any]):
    if image is None or not HAS_OPENCV or np is None or cv2 is None:
        return None
    try:
        min_area_ratio = float(cfg.get("plate_read_autocrop_min_area_ratio", 0.012))
    except Exception:
        min_area_ratio = 0.012
    try:
        min_height_ratio = float(cfg.get("plate_read_autocrop_min_height_ratio", 0.45))
    except Exception:
        min_height_ratio = 0.45
    try:
        pad = int(cfg.get("plate_read_autocrop_pad", 2))
    except Exception:
        pad = 2

    try:
        arr = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _thr, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if float(np.mean(bw)) > 127.0:
            bw = cv2.bitwise_not(bw)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
        contours, _hier = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    except Exception:
        return None

    if not contours:
        return None

    h, w = gray.shape[:2]
    area_min = max(12.0, float(h * w) * min_area_ratio)
    height_min = max(6.0, float(h) * min_height_ratio)
    best = None
    best_right = -1
    for c in contours:
        try:
            x, y, cw, ch = cv2.boundingRect(c)
        except Exception:
            continue
        if cw <= 2 or ch <= 2:
            continue
        area = float(cw * ch)
        if area < area_min or ch < height_min:
            continue
        right = x + cw
        if right > best_right:
            best_right = right
            best = (x, y, cw, ch)

    if not best:
        return None

    x, y, cw, ch = best
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(w, x + cw + pad)
    y1 = min(h, y + ch + pad)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    try:
        return image.crop((x0, y0, x1, y1))
    except Exception:
        return None


def read_plate_with_retry(cfg: Dict[str, Any],
                          plate_roi: Optional[Tuple[int, int, int, int]],
                          stem: Optional[str],
                          run_event: threading.Event,
                          stop_event: threading.Event) -> "plate_reader.PlateReadResult":
    def _has_digit(text: str) -> bool:
        return any(ch.isdigit() for ch in text or "")

    def _try_side_crop(image):
        if image is None:
            return None
        try:
            ratio = float(cfg.get("plate_read_side_crop_ratio", 0.55))
        except Exception:
            ratio = 0.55
        ratio = max(0.2, min(0.9, ratio))
        try:
            w, h = image.size
        except Exception:
            return None
        if w <= 0 or h <= 0:
            return None
        crop_w = int(w * ratio)
        if crop_w < 10:
            return None
        x0 = max(0, w - crop_w)
        if x0 >= w - 2:
            return None
        try:
            cropped = image.crop((x0, 0, w, h))
            cw, ch = cropped.size
            _log_plate_ocr_params(cfg, (0, 0, cw, ch), cropped, "side-crop")
            return plate_reader.read_plate_from_ui(cropped, (0, 0, cw, ch))
        except Exception:
            return None

    try:
        attempts = int(cfg.get("plate_read_attempts", 2))
    except Exception:
        attempts = 2
    try:
        delay = float(cfg.get("plate_read_retry_delay", 0.35))
    except Exception:
        delay = 0.35

    last = plate_reader.PlateReadResult(None, None, "", "none", None)
    prompt_used = False
    if stem:
        state = _PLATE_OCR_DEDUPE.setdefault(stem, {"sources": set(), "saved": {}, "value_dbg": False})
        logged_sources = state["sources"]
        saved_debug = state["saved"]
        logged_value_dbg = state["value_dbg"]
    else:
        logged_sources = set()
        saved_debug = {}
        logged_value_dbg = False
    for i in range(max(1, attempts)):
        def _log_ocr_once(region, image, source):
            if source in logged_sources:
                return
            logged_sources.add(source)
            _log_plate_ocr_params(cfg, region, image, source)
        prompt_allowed = bool(cfg.get("plate_read_prompt_on_fail", True)) and (i >= (attempts - 1)) and (not prompt_used)
        try:
            os.environ["PLATE_READ_PROMPT_ON_FAIL"] = "0"
        except Exception:
            pass
        _configure_plate_reader_env(cfg, False)
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return last
        live = None
        line = None
        if bool(cfg.get("plate_read_use_value_crop_first", True)):
            try:
                live, dbg, line = _grab_plate_value_and_line(cfg)
            except Exception:
                live, dbg, line = None, None, None
            if dbg:
                try:
                    if not logged_value_dbg:
                        log(f"PLATE: value-crop dbg={dbg}")
                        logged_value_dbg = True
                        if stem:
                            _PLATE_OCR_DEDUPE[stem]["value_dbg"] = True
                except Exception:
                    pass
            if line is not None and bool(cfg.get("plate_read_save_line_debug", False)) and not saved_debug.get("line", False):
                try:
                    dbg_dir = Path(os.getenv("PLATE_READ_DEBUG_DIR", str(USER_DIR)))
                    dbg_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    out = dbg_dir / f"plate_line_{stamp}.png"
                    line.save(out)
                    log(f"PLATE: saved line debug -> {out}")
                    saved_debug["line"] = True
                except Exception:
                    pass
            if live is not None:
                try:
                    w, h = live.size
                    _log_ocr_once((0, 0, w, h), live, "value-crop")
                    last = plate_reader.read_plate_from_ui(live, (0, 0, w, h))
                except Exception:
                    _log_ocr_once(plate_roi, None, "roi-fallback")
                    last = plate_reader.read_plate_from_ui(None, plate_roi)
            else:
                _log_ocr_once(plate_roi, None, "roi")
                last = plate_reader.read_plate_from_ui(None, plate_roi)
        else:
            _log_ocr_once(plate_roi, None, "roi")
            last = plate_reader.read_plate_from_ui(None, plate_roi)
        if last.plate:
            return last
        if line is not None and bool(cfg.get("plate_read_full_line_fallback", True)) and (not last.plate):
            try:
                lw, lh = line.size
                _log_ocr_once((0, 0, lw, lh), line, "full-line")
                line_res = plate_reader.read_plate_from_ui(line, (0, 0, lw, lh))
            except Exception:
                line_res = None
            if line_res:
                if line_res.plate:
                    log("PLATE: OCR full-line -> success")
                    return line_res
                if line_res.raw_text:
                    log(f"PLATE: OCR full-line raw='{line_res.raw_text}' method={line_res.method}")
                last = line_res
        if live is not None and bool(cfg.get("plate_read_side_crop_enabled", True)) and (not _has_digit(last.raw_text)):
            side = _try_side_crop(live)
            if side:
                if side.plate:
                    log("PLATE: OCR right-crop -> success")
                    return side
                if side.raw_text:
                    log(f"PLATE: OCR right-crop raw='{side.raw_text}' method={side.method}")
                last = side
        if live is not None and bool(cfg.get("plate_read_autocrop_enabled", True)) and (not last.plate):
            cropped = _plate_autocrop_text_region(live, cfg)
            if cropped is not None:
                if bool(cfg.get("plate_read_save_autocrop_debug", False)) and not saved_debug.get("autocrop", False):
                    try:
                        dbg_dir = Path(os.getenv("PLATE_READ_DEBUG_DIR", str(USER_DIR)))
                        dbg_dir.mkdir(parents=True, exist_ok=True)
                        stamp = time.strftime("%Y%m%d_%H%M%S")
                        out = dbg_dir / f"plate_autocrop_{stamp}.png"
                        cropped.save(out)
                        log(f"PLATE: saved autocrop debug -> {out}")
                        saved_debug["autocrop"] = True
                    except Exception:
                        pass
                try:
                    cw, ch = cropped.size
                    _log_ocr_once((0, 0, cw, ch), cropped, "autocrop")
                    auto_res = plate_reader.read_plate_from_ui(cropped, (0, 0, cw, ch))
                except Exception:
                    auto_res = None
                if auto_res:
                    if auto_res.plate:
                        log("PLATE: OCR autobox -> success")
                        return auto_res
                    if auto_res.raw_text:
                        log(f"PLATE: OCR autobox raw='{auto_res.raw_text}' method={auto_res.method}")
                    last = auto_res
        if last.raw_text:
            log(f"PLATE: OCR raw='{last.raw_text}' method={last.method}")
        if bool(cfg.get("plate_read_value_crop_fallback", True)):
            try:
                live, dbg = _grab_plate_value_live(cfg)
            except Exception:
                live, dbg = None, None
            if live is not None:
                if bool(cfg.get("plate_read_save_value_crop_debug", True)) and not saved_debug.get("value", False):
                    try:
                        dbg_dir = Path(os.getenv("PLATE_READ_DEBUG_DIR", str(USER_DIR)))
                        dbg_dir.mkdir(parents=True, exist_ok=True)
                        stamp = time.strftime("%Y%m%d_%H%M%S")
                        out = dbg_dir / f"plate_value_crop_{stamp}.png"
                        live.save(out)
                        log(f"PLATE: saved value crop debug -> {out}")
                        saved_debug["value"] = True
                    except Exception:
                        pass
                try:
                    w, h = live.size
                    prev_prompt = os.getenv("PLATE_READ_PROMPT_ON_FAIL", "1")
                    os.environ["PLATE_READ_PROMPT_ON_FAIL"] = "0"
                    try:
                        _log_ocr_once((0, 0, w, h), live, "value-crop-fallback")
                        fallback = plate_reader.read_plate_from_ui(live, (0, 0, w, h))
                    finally:
                        os.environ["PLATE_READ_PROMPT_ON_FAIL"] = prev_prompt
                except Exception:
                    fallback = None
                if fallback and fallback.plate:
                    log("PLATE: OCR fallback from value crop -> success")
                    return fallback
                if fallback:
                    if fallback.raw_text:
                        log(f"PLATE: OCR fallback raw='{fallback.raw_text}' method={fallback.method}")
                    last = fallback
        if (not last.plate) and prompt_allowed:
            try:
                os.environ["PLATE_READ_PROMPT_ON_FAIL"] = "1"
            except Exception:
                pass
            _configure_plate_reader_env(cfg, True)
            if live is not None:
                try:
                    w, h = live.size
                    _log_ocr_once((0, 0, w, h), live, "manual-prompt")
                    manual_res = plate_reader.read_plate_from_ui(live, (0, 0, w, h))
                except Exception:
                    manual_res = plate_reader.read_plate_from_ui(None, plate_roi)
            else:
                _log_ocr_once(plate_roi, None, "manual-prompt")
                manual_res = plate_reader.read_plate_from_ui(None, plate_roi)
            prompt_used = True
            last = manual_res or last
            if last.plate:
                return last
            try:
                os.environ["PLATE_READ_PROMPT_ON_FAIL"] = "0"
            except Exception:
                pass
            _configure_plate_reader_env(cfg, False)
        if i < attempts - 1:
            sleep_coop(delay, run_event, stop_event)
    return last

def iter_plate_blacklist_refs(cfg: Dict[str, Any]):
    """Yield (label, path) for all plate blacklist entries."""
    for item in (cfg.get("plate_blacklist") or []):
        label = str(item.get("label") or "plate")
        p = _resolve_user_path(item.get("path"))
        if not p:
            continue
        try:
            pp = Path(p)
        except Exception:
            continue
        if pp.exists():
            yield label, pp


def plate_blacklist_best(cfg: Dict[str, Any]) -> Tuple[Optional[str], float, Optional[Path]]:
    """Return best (label, score, path) for current live plate snapshot."""
    if not HAS_OPENCV:
        return None, 0.0, None

    live, dbg = _grab_plate_value_live(cfg)
    if live is None:
        return None, 0.0, None

    best_label: Optional[str] = None
    best_score: float = 0.0
    best_path: Optional[Path] = None
    for label, p in iter_plate_blacklist_refs(cfg):
        sc = plate_match_score(cfg, p, live_img=live)
        if sc > best_score:
            best_label, best_score, best_path = label, float(sc), p
    return best_label, float(best_score), best_path


def is_plate_blacklisted(cfg: Dict[str, Any]) -> Tuple[bool, Optional[str], float]:
    """True if current plate VALUE matches any saved blacklist ref above threshold."""
    if not HAS_OPENCV:
        return False, None, 0.0
    thr = float(cfg.get("plate_blacklist_confidence", 0.94))
    label, score, _p = plate_blacklist_best(cfg)
    if label and score >= thr:
        return True, label, float(score)
    return False, label, float(score)


def enter_create_rent(cfg: Dict[str, Any],
                      run_event: threading.Event, stop_event: threading.Event) -> bool:
    
    # Throttle repeated Create->Rent navigation (prevents thrash when scans fail).
    now = time.time()
    cooldown = float(cfg.get("enter_create_cooldown_s", 8.0))
    try:
        last = float(NAV_STATE.get("last_enter", 0.0))
    except Exception:
        last = 0.0
    if cooldown > 0 and (now - last) < cooldown:
        # Assume we're already on Create->Rent flow; skip extra clicks.
        return True
    # Координата промежуточного попапа "Выберите категорию" (новый интерфейс).
    # Если create_category задана в coords — кликаем её между create и create_rent.
    # Если не задана — старое поведение (два клика).
    # [0,0] значит отключено — проверяем что хотя бы одна координата не 0
    _cc = (cfg.get("coords") or {}).get("create_category") or [0, 0]
    popup_xy = _cc if (_cc[0] != 0 or _cc[1] != 0) else None

    wait_if_paused(run_event, stop_event)
    if stop_event.is_set():
        return False
    # Кнопка "Создать" — одиночный клик (двойной вызывал баги с UI)
    click_xy(cfg["coords"]["create"], run_event, stop_event)
    sleep_coop(d(cfg, "ui_delay_long", 1.40), run_event, stop_event)
    # Если есть промежуточный попап категорий — кликаем его
    if popup_xy:
        click_xy(popup_xy, run_event, stop_event)
        sleep_coop(d(cfg, "ui_delay_medium", 0.70), run_event, stop_event)
    click_xy(cfg["coords"]["create_rent"], run_event, stop_event)
    sleep_coop(d(cfg, "ui_delay_long", 1.40), run_event, stop_event)
    log("enter_create_rent done")
    NAV_STATE["last_enter"] = time.time()
    NAV_STATE["in_create_rent"] = True
    NAV_STATE["list_ready_initial_delay_done"] = False
    return True



def enter_create_rent_force(cfg: Dict[str, Any],
                            run_event: threading.Event, stop_event: threading.Event) -> bool:
    """Force Create->Rent navigation bypassing cooldown."""
    try:
        NAV_STATE["last_enter"] = 0.0
    except Exception:
        pass
    return enter_create_rent(cfg, run_event, stop_event)
# ---------------- idle tracker + bump ----------------
class IdleTracker:
    _lock = threading.Lock()
    last_activity_at = time.time()
    last_bump_at = 0.0

    @staticmethod
    def mark_activity() -> None:
        with IdleTracker._lock:
            IdleTracker.last_activity_at = time.time()

    @staticmethod
    def maybe_bump(cfg: Dict[str, Any], run_event: threading.Event, stop_event: threading.Event) -> bool:
        if not bool(cfg.get("bump_enabled", True)):
            return False
        idle_after = float(cfg.get("bump_idle_after", 90.0))
        cooldown = float(cfg.get("bump_cooldown", idle_after))
        now = time.time()

        with IdleTracker._lock:
            idle_for = now - float(IdleTracker.last_activity_at)
            since_bump = now - float(IdleTracker.last_bump_at)

        if idle_after <= 0:
            return False
        if idle_for < idle_after:
            return False
        if since_bump < cooldown:
            return False

        ok = bump_my_ads(cfg, run_event, stop_event)
        with IdleTracker._lock:
            IdleTracker.last_bump_at = time.time()
            IdleTracker.last_activity_at = time.time()
        return ok


def bump_my_ads(cfg: Dict[str, Any], run_event: threading.Event, stop_event: threading.Event) -> bool:
    """Click through 'My Ads' grid to bump/refresh listings.
    Supports optional scrolling to capture an extra row that may be off-screen.
    """
    if pyautogui is None:
        return False

    wait_if_paused(run_event, stop_event)
    if stop_event.is_set():
        return False

    coords = cfg.get("coords") or {}
    if "my_ads" not in coords:
        log("BUMP: coords.my_ads missing -> skip")
        return False

    log("BUMP: idle threshold reached -> going to 'My Ads' and clicking grid…")

    click_xy(coords["my_ads"], run_event, stop_event)
    sleep_coop(d(cfg, "bump_enter_delay", 1.20), run_event, stop_event)
    if stop_event.is_set():
        return False

    # Prefer point-based bump (more robust across resolutions / partial rows)
    use_points = bool(cfg.get("bump_use_points", False))
    points = cfg.get("bump_points") or []
    pages = max(1, int(cfg.get("bump_pages", 1)))
    scroll_px = int(cfg.get("bump_scroll_pixels", 260))
    scroll_delay = float(cfg.get("bump_scroll_delay", 0.25)) / _speed(cfg)

    click_delay = d(cfg, "bump_click_delay", 0.60)
    back_delay = d(cfg, "bump_back_delay", 0.55)

    if use_points and isinstance(points, list) and len(points) > 0:
        for pg in range(pages):
            for pt in points:
                wait_if_paused(run_event, stop_event)
                if stop_event.is_set():
                    return False
                try:
                    x, y = int(pt[0]), int(pt[1])
                except Exception:
                    continue

                click_xy([x, y], run_event, stop_event)
                sleep_coop(click_delay, run_event, stop_event)

                # return to list
                click_xy(coords["my_ads"], run_event, stop_event)
                sleep_coop(back_delay, run_event, stop_event)

            if pg < pages - 1 and pyautogui is not None and scroll_px != 0:
                try:
                    pyautogui.scroll(-abs(scroll_px))
                    time.sleep(scroll_delay)
                except Exception:
                    pass
    else:
        # Fallback: grid-based bump (legacy)
        cols = int(cfg.get("bump_grid_cols", 5))
        rows = int(cfg.get("bump_grid_rows", 3))
        x0 = int(cfg.get("bump_grid_x0", 464))
        y0 = int(cfg.get("bump_grid_y0", 211))
        dx = int(cfg.get("bump_grid_dx", 314))
        dy = int(cfg.get("bump_grid_dy", 290))

        passes = max(1, int(cfg.get("bump_pages", 1)))
        for pg in range(passes):
            for r in range(rows):
                for c in range(cols):
                    wait_if_paused(run_event, stop_event)
                    if stop_event.is_set():
                        return False

                    click_xy([x0 + c * dx, y0 + r * dy], run_event, stop_event)
                    sleep_coop(click_delay, run_event, stop_event)

                    # return to list
                    click_xy(coords["my_ads"], run_event, stop_event)
                    sleep_coop(back_delay, run_event, stop_event)

            if pg < passes - 1 and pyautogui is not None and scroll_px != 0:
                try:
                    pyautogui.scroll(-abs(scroll_px))
                    time.sleep(scroll_delay)
                except Exception:
                    pass

    enter_create_rent(cfg, run_event, stop_event)
    log("BUMP: done -> returned to Create->Rent")
    return True


def post_one_item(cfg: Dict[str, Any], tmpl_png: Path,
                  run_event: threading.Event, stop_event: threading.Event) -> PostResult:
    stem = tmpl_png.stem
    folder = item_folder(cfg, stem)

    if not is_valid_item(cfg, stem):
        return PostResult(status="INVALID_ITEM")

    dedupe_policy = str(cfg.get("dedupe_policy", "off")).strip().lower()
    loop_mode = bool(cfg.get("loop_mode", True))
    if loop_mode and dedupe_policy == "on_success" and bool(cfg.get("dedupe_force_in_loop", False)) is False:
        dedupe_policy = "off"

    photo_hashes = load_photo_hashes()
    h = compute_photo1_hash(cfg, stem)

    if dedupe_policy == "on_success" and h and is_duplicate_hash(photo_hashes, h):
        log(f"[{stem}] skipped (duplicate photo hash)")
        return PostResult(status="DUPLICATE_HASH")

    # --- Vehicle selection (fast scan + fallback locate) ---
    region = _region_to_tuple(cfg.get("vehicle_region"))
    region_used = region

    if bool(cfg.get("ensure_vehicle_list_ready", True)):
        try:
            wait_for_vehicle_list_ready(cfg, list_templates(cfg), run_event, stop_event)
        except Exception:
            pass

    # If pre_sweep ran at the start of this cycle, the cache already has
    # positions for ALL templates from a single screenshot.  We only need
    # to re-scan if this specific stem has no cached hits (e.g. it was
    # off-screen, or appeared after scrolling).  This avoids taking a
    # separate screenshot per vehicle — the biggest speed win.
    _cached_hits = fast_scan_get(stem)
    if not _cached_hits:
        # Not in cache (or cache empty) — do a single-template rescan
        try:
            fast_scan_prebuild_current(cfg, tmpl_png, region=region)
        except Exception:
            pass

    candidates = fast_scan_get(stem)
    rebuild_on_miss = bool(cfg.get("fast_scan_rebuild_on_miss", False))
    rebuild_single = bool(cfg.get("fast_scan_rebuild_on_miss_single", False))

    def _templates_for_rebuild() -> List[Path]:
        return [tmpl_png] if rebuild_single else list_templates(cfg)

    try:
        if candidates:
            best = max([c[2] for c in candidates])
            log(f"FASTSCAN[{stem}]: hits={len(candidates)} best={best:.2f}")
        else:
            try:
                base_conf = float(_clamp_conf(cfg))
            except Exception:
                base_conf = 0.91
            try:
                veh_conf = float(cfg.get("vehicle_confidence", base_conf))
            except Exception:
                veh_conf = base_conf
            conf0 = max(base_conf, veh_conf)
            try:
                fallback_min = float(cfg.get("fast_scan_fallback_min", max(0.62, conf0 - 0.28)))
            except Exception:
                fallback_min = max(0.62, conf0 - 0.28)
            meta = (FAST_SCAN_CACHE.get("meta") or {}).get(stem, {})
            best_score = meta.get("best_score", None)
            mask_used = meta.get("mask", None)
            if isinstance(best_score, (int, float)):
                best_str = f"{best_score:.4f}"
            else:
                best_str = "n/a"
            if mask_used is True:
                mask_str = "on"
            elif mask_used is False:
                mask_str = "off"
            else:
                mask_str = "n/a"
            log(
                f"FASTSCAN[{stem}]: hits=0 best_score={best_str} "
                f"fallback_min={fallback_min:.2f} conf0={conf0:.2f} mask={mask_str}"
            )
    except Exception:
        pass
    if not candidates:
        # Rescan once on the live selection list WITHOUT re-entering Create->Rent (prevents thrash).
        try:
            FORCE_REFRESH_STATE["fail_streak"] = int(FORCE_REFRESH_STATE.get("fail_streak", 0)) + 1
        except Exception:
            pass

        # Try to find via autoscroll first (if enabled).
        if not candidates and region:
            try:
                candidates = vehicle_autoscroll_find(cfg, stem, tmpl_png, region, run_event, stop_event)
            except Exception:
                pass

        if rebuild_on_miss and not candidates:
            try:
                fast_scan_build(cfg, _templates_for_rebuild())
                candidates = fast_scan_get(stem)
            except Exception:
                pass

        # Optional: full-screen rescan on repeated misses (helps when vehicle_region is off).
        if rebuild_on_miss and (not candidates) and region and bool(cfg.get("fast_scan_fullscreen_on_miss", True)):
            try:
                streak = int(FORCE_REFRESH_STATE.get("fail_streak", 0))
                after = int(cfg.get("fast_scan_fullscreen_on_miss_after", 2))
                if streak >= max(1, after):
                    fast_scan_build(cfg, _templates_for_rebuild(), use_fullscreen=True)
                    candidates = fast_scan_get(stem)
            except Exception:
                pass

        # If fast-scan keeps failing for multiple items, force a single refresh with cooldown.
        if not candidates:
            try:
                now = time.time()
                last_force = float(FORCE_REFRESH_STATE.get("last_force", 0.0))
                cooldown = float(cfg.get("force_refresh_cooldown_s", 18.0))
                streak = int(FORCE_REFRESH_STATE.get("fail_streak", 0))
                if streak >= int(cfg.get("fast_scan_fail_streak_for_refresh", 2)) and (now - last_force) >= cooldown:
                    FORCE_REFRESH_STATE["last_force"] = now
                    log(f"FASTSCAN: fail_streak={streak} -> force refresh Create->Rent")
                    enter_create_rent(cfg, run_event, stop_event)
                    if rebuild_on_miss:
                        fast_scan_build(cfg, _templates_for_rebuild())
                        candidates = fast_scan_get(stem)
            except Exception:
                pass

    if candidates:
        try:
            FORCE_REFRESH_STATE["fail_streak"] = 0
        except Exception:
            pass

    if not candidates:

        pos = None
        locate_tries = int(cfg.get("vehicle_locate_tries", 4))
        base_retry = float(cfg.get("vehicle_locate_retry_delay", 0.45))
        for t in range(max(1, locate_tries)):
            wait_if_paused(run_event, stop_event)
            if stop_event.is_set():
                return PostResult(status="STOPPED")
            pos = locate_center_vehicle(cfg, tmpl_png, region=region)
            if pos:
                candidates = [(pos[0], pos[1], 1.0)]
                break
            # Give the UI a moment to render / repaint; avoid re-enter loops when cars are already on-screen
            if t < locate_tries - 1:
                try:
                    if pyautogui is not None:
                        pyautogui.moveRel(1, 0)
                        pyautogui.moveRel(-1, 0)
                except Exception:
                    pass
                sleep_coop(max(0.05, (base_retry + t * 0.25) / _speed(cfg)), run_event, stop_event)

        if (not candidates) and region and bool(cfg.get("vehicle_locate_fullscreen_on_miss", True)):
            try:
                fullscreen_tries = int(cfg.get("vehicle_locate_fullscreen_tries", 2))
            except Exception:
                fullscreen_tries = 2
            try:
                log(f"[{stem}] locate fallback: fullscreen tries={fullscreen_tries}")
            except Exception:
                pass
            for t in range(max(1, fullscreen_tries)):
                wait_if_paused(run_event, stop_event)
                if stop_event.is_set():
                    return PostResult(status="STOPPED")
                pos = locate_center_vehicle(cfg, tmpl_png, region=None)
                if pos:
                    candidates = [(pos[0], pos[1], 1.0)]
                    break
                if t < fullscreen_tries - 1:
                    sleep_coop(max(0.05, (base_retry + t * 0.25) / _speed(cfg)), run_event, stop_event)

    if not candidates:
        try:
            IdleTracker.maybe_bump(cfg, run_event, stop_event)
        except Exception as e:
            log(f"BUMP error: {e}")
        return PostResult(status="TEMPLATE_NOT_FOUND")

    IdleTracker.mark_activity()

    # Try candidates; validate plate if available
    picked = False


    _candidate_count = 0
    _plate_mismatch_count = 0
    def _back_to_vehicle_list():
        """Try to return from the form back to the vehicle selection list (without full refresh)."""
        try:
            coords = cfg.get("coords") or {}
            pt = coords.get("form_back") or coords.get("back_form") or coords.get("form_back_xy")
            if pt and isinstance(pt, (list, tuple)) and len(pt) >= 2 and int(pt[0]) > 5 and int(pt[1]) > 5:
                click_xy([int(pt[0]), int(pt[1])], run_event, stop_event)
                sleep_coop(d(cfg, "ui_delay_medium", 0.70), run_event, stop_event)
                return True
        except Exception:
            pass
        return False

    pick_attempts = 0
    for ci, (cx, cy, score) in enumerate(candidates):
        _candidate_count += 1
        pick_attempts = ci + 1
        wait_if_paused(run_event, stop_event)
        if stop_event.is_set():
            return PostResult(status="STOPPED")
        # Avoid clicking very weak matches (false positives). Fast-scan can step down
        # its thresholds to "find something", so we hard-gate clicks here.
        # Uses vehicle_click_min from config (default 0.78).
        try:
            _raw_min = cfg.get("vehicle_click_min")
            if _raw_min is not None:
                click_min = float(_raw_min)
            else:
                click_min = max(0.70, float(_clamp_conf(cfg)) - 0.14)
        except Exception:
            click_min = 0.78
        try:
            if score is not None and float(score) < click_min:
                log(f"[{stem}] candidate score {float(score):.2f} < click_min {click_min:.2f} -> skip")
                continue
        except Exception:
            pass


        # Extra safety: reject false-positive template hits (e.g. Brottora matching Shiron card)
        try:
            ok_comp, best_stem, best_sc, exp_sc = fast_scan_compete_check(cfg, stem, int(cx), int(cy))
            if not ok_comp:
                log(f"[{stem}] FASTSCAN candidate rejected: best={best_stem} {best_sc:.2f} vs me {exp_sc:.2f}")
                continue
        except Exception:
            pass


        click_xy([int(cx), int(cy)], run_event, stop_event)
        # NOTE: confirm re-click DISABLED. The second click at the same
        # vehicle-list coordinates can land on the form's "upload photo"
        # area when the form opens fast, opening a file dialog and
        # stealing focus from the text fields.  One click is enough.

        if not wait_for_form_ready(cfg, run_event, stop_event):
            log(f"[{stem}] FORM not ready (anchor missing) -> try next candidate / refresh")
            if ci < (len(candidates) - 1):
                if _back_to_vehicle_list():
                    continue
            enter_create_rent(cfg, run_event, stop_event)
            continue

        try:
            _metrics_inc("candidates_found", 1)
        except Exception:
            pass

        plate_roi = _region_to_tuple(cfg.get("plate_region"))
        plate_result = read_plate_with_retry(cfg, plate_roi, stem, run_event, stop_event)
        plate_text = plate_result.plate
        vehicle_key = ""
        if plate_result.debug_path:
            log(f"[{stem}] plate read failed, debug={plate_result.debug_path}")

        if plate_text:
            if plate_registry.is_enabled():
                vehicle_key = plate_registry.resolve_or_prompt(plate_text) or ""
                if vehicle_key:
                    log(f"[{stem}] plate registry: {plate_text} -> {vehicle_key}")
                if bool(cfg.get("plate_registry_auto_register", True)) and stem:
                    plate_registry.register_plate(plate_text, stem)
            else:
                vehicle_key = stem
        else:
            limits = _limits_cfg(cfg)
            hard_block_no_plate = bool(limits.get("hard_block_no_plate")) or (os.getenv("HARD_BLOCK_NO_PLATE", "0") == "1")
            if hard_block_no_plate:
                log(f"[{stem}] NO_PLATE -> block (hard_block_no_plate)")
                _metrics_inc("blocked_no_plate", 1)
                _metrics_set("last_decision_reason", "no_plate")
                _metrics_set("last_decision_at", time.time())
                return PostResult(status="BLOCKED", reason="no_plate")

        if bool(cfg.get("plate_registry_enforce_match", True)) and vehicle_key and vehicle_key != stem:
            log(f"[{stem}] plate registry mismatch: plate {plate_text} -> {vehicle_key} (skip)")
            _metrics_inc("blocked_unknown_plate", 1)
            # Try next candidate without full reset if possible.
            if ci < (len(candidates) - 1):
                if _back_to_vehicle_list():
                    continue
            enter_create_rent(cfg, run_event, stop_event)
            continue

        limits = _limits_cfg(cfg)
        tg_now = time.time()
        tg_state = tg_rent_tracker.vehicle_stats(plate_text, tg_now)
        tg_active = tg_rent_tracker.active_rentals(tg_now)
        tg_state["active_total"] = tg_active.get("count", 0)
        tg_state["next_end_ts"] = tg_active.get("next_end_ts")
        tg_state["limits"] = limits
        scan_state = {
            "plate_confidence": plate_result.confidence,
            "fastscan_free": METRICS_STATE.get("fastscan_free", 0),
        }
        decision = rental_limiter.allow(vehicle_key or "", plate_text, tg_state, scan_state, tg_now)
        if not decision.allowed:
            log(f"[{stem}] decision gate blocked: {decision.reason}")
            _metrics_set("last_decision_reason", decision.reason)
            _metrics_set("last_decision_at", time.time())
            if decision.reason == "never_rent":
                _metrics_inc("blocked_never_rent", 1)
            elif decision.reason in ("over_active_limit", "over_daily_hours", "over_weekly_hours", "cooldown"):
                _metrics_inc("blocked_over_limit", 1)
            elif decision.reason == "no_plate":
                _metrics_inc("blocked_no_plate", 1)
            elif decision.reason == "low_confidence":
                _metrics_inc("blocked_low_confidence", 1)
            elif decision.reason == "unknown_plate_blocked":
                _metrics_inc("blocked_unknown_plate", 1)
            return PostResult(status="BLOCKED", reason=decision.reason)

        # Plate blacklist (do-not-rent): if hits, back out and restart selection
        bl_hit, bl_label, bl_score = is_plate_blacklisted(cfg)
        if bl_hit:
            log(f"[{stem}] PLATE_BLACKLIST hit: {bl_label} score={bl_score:.3f} -> skip candidate")
            # Prefer trying the next candidate (same list) without full reset.
            if ci < (len(candidates) - 1):
                if _back_to_vehicle_list():
                    continue
            enter_create_rent(cfg, run_event, stop_event)
            continue


        if not plate_validate(cfg, folder, stem=stem):
            _plate_mismatch_count += 1
            log(f"[{stem}] PLATE mismatch -> try next candidate (stay in list if possible)")
            # Prefer trying the next candidate (duplicate thumbnails) without full reset.
            if ci < (len(candidates) - 1):
                if _back_to_vehicle_list():
                    continue
                # fallback: refresh selection list
                enter_create_rent(cfg, run_event, stop_event)
                continue
            # last candidate -> full refresh (next sweep may succeed)
            enter_create_rent(cfg, run_event, stop_event)
            continue

        picked = True
        break

    # record pick attempts
    if picked:
        try:
            append_pick_attempt_row(stem, pick_attempts, True, reason="picked")
        except Exception:
            pass
    else:
        try:
            append_pick_attempt_row(stem, pick_attempts, False, reason="no_candidate_valid")
        except Exception:
            pass

    if not picked:
        if _candidate_count > 0 and _plate_mismatch_count >= _candidate_count and bool(cfg.get("plate_mismatch_is_distinct_status", True)):
            return PostResult(status="PLATE_MISMATCH")
        return PostResult(status="TEMPLATE_NOT_FOUND")

    desc = read_text(folder, "description")
    base_price_raw = (read_text(folder, "price") or "").strip() or "0"
    price_raw, price_value, price_info = compute_effective_price(cfg, folder, base_price_raw)
    if price_info.get("mode") in ("schedule", "auto_suggest"):
        log(f"[{stem}] dynamic price: base={price_info.get('base_val')} {price_info.get('daytype')}:{price_info.get('slot')} x{price_info.get('mult'):.2f} -> {price_raw}")
    else:
        log(f"[{stem}] price manual: {price_raw}")

    log(f"[{stem}] writing fields… (desc={len(desc)} chars, price='{price_raw}')")

    if not write_field(cfg["coords"]["comment"], desc, cfg, run_event, stop_event, field_name="comment"):
        return PostResult(status="ERROR", price_raw=price_raw, price_value=price_value, reason="write_comment")
    sleep_coop(d(cfg, "ui_delay_short", 0.25), run_event, stop_event)

    if not write_field(cfg["coords"]["price"], price_raw, cfg, run_event, stop_event, field_name="price"):
        return PostResult(status="ERROR", price_raw=price_raw, price_value=price_value, reason="write_price")
    sleep_coop(d(cfg, "ui_delay_short", 0.25), run_event, stop_event)
    # Some UI fields only commit on focus change; tab out after price to ensure it applies.
    if bool(cfg.get("apply_tab_after_price", True)):
        try:
            pyautogui.press("tab")
        except Exception:
            pass
        sleep_coop(d(cfg, "after_price_apply_delay", 0.12), run_event, stop_event)
    log(f"[{stem}] fields written -> start photos")
    sleep_coop(d(cfg, "fields_before_photos_delay", 0.35), run_event, stop_event)
    if stop_event.is_set():
        return PostResult(status="STOPPED", price_raw=price_raw, price_value=price_value)


    if not all(find_photo(folder, i) for i in (1, 2, 3)):
        log(f"[{stem}] missing photos 1/2/3 in folder {folder}")
        return PostResult(status="INVALID_ITEM", price_raw=price_raw, price_value=price_value)

    folder_path = str(folder)

    for i in (1, 2, 3):
        first = (i == 1)
        if not open_file_dialog(cfg, first, run_event, stop_event):
            return PostResult(status="DIALOG_FAIL", price_raw=price_raw, price_value=price_value, reason=f"open_dialog_{i}")

        # Only type the folder path on the FIRST dialog for this listing.
        # The next dialog usually opens in the same folder already.
        reuse_folder = bool(cfg.get("file_dialog_reuse_folder", True))
        filename_coords = cfg.get("file_dialog_filename_coords")
        select_path_coords = None
        try:
            select_path_coords = cfg.get("coords", {}).get("select_path")
        except Exception:
            select_path_coords = None
        has_filename_coords = isinstance(filename_coords, (list, tuple)) and len(filename_coords) == 2
        same_as_select_path = bool(has_filename_coords and select_path_coords and list(filename_coords) == list(select_path_coords))
        allow_filename_hotkey = bool(cfg.get("file_dialog_filename_allow_hotkey_fallback", True))
        use_filename_field = (
            bool(cfg.get("file_dialog_use_filename_field", False))
            and ((has_filename_coords and not same_as_select_path) or allow_filename_hotkey)
            and (not bool(cfg.get("file_dialog_filename_first_only", True)) or first)
        )
        photo_path = find_photo(folder, i)
        if use_filename_field and bool(cfg.get("file_dialog_filename_prefer_only", True)):
            if pick_file(cfg, i, run_event, stop_event, file_path=photo_path, use_filename_field=True, filename_only=True):
                continue
            log("DIALOG: filename-only failed -> fallback to navigate+click")
            use_filename_field = False
        if first or (not reuse_folder) or bool(cfg.get("file_dialog_set_path_each_file", False)):
            # Extra pause before typing the folder path for the FIRST dialog (focus/lag safety)
            if first:
                sleep_coop(d(cfg, "file_dialog_first_path_extra_pause", 2.0), run_event, stop_event)
            if not (use_filename_field and bool(cfg.get("file_dialog_filename_skip_navigate", True))):
                if not navigate_dialog_to_folder(cfg, folder_path, run_event, stop_event):
                    return PostResult(status="DIALOG_FAIL", price_raw=price_raw, price_value=price_value, reason="navigate_dialog")
        else:
            sleep_coop(d(cfg, "file_dialog_reuse_delay", 0.18), run_event, stop_event)
            if (not use_filename_field) and bool(cfg.get("file_dialog_verify_on_reuse", True)):
                if not ensure_dialog_folder(cfg, folder_path, run_event, stop_event, reason="reuse_before_pick"):
                    return PostResult(status="DIALOG_FAIL", price_raw=price_raw, price_value=price_value, reason="reuse_verify")

        if first:
            sleep_coop(1.0, run_event, stop_event)
            if stop_event.is_set():
                return PostResult(status="STOPPED", price_raw=price_raw, price_value=price_value)
        if not pick_file(cfg, i, run_event, stop_event, file_path=photo_path, use_filename_field=use_filename_field):
            return PostResult(status="DIALOG_FAIL", price_raw=price_raw, price_value=price_value, reason=f"pick_{i}")

    click_xy(cfg["coords"]["create_ad"], run_event, stop_event)
    sleep_coop(d(cfg, "ui_delay_medium", 0.70), run_event, stop_event)

    # Set rental duration in the payment popup.
    # Field already has a default value (e.g. "5"). Click it, move to end, append "0" → "50".
    select_time_xy = cfg["coords"].get("select_time") or [741, 545]
    rental_hours_append = str(cfg.get("rental_hours_append", "0"))
    log(f"[{stem}] setting rental hours (append '{rental_hours_append}')")

    # Click the hours input field twice (ensure focus)
    click_xy(select_time_xy, run_event, stop_event)
    sleep_coop(0.2, run_event, stop_event)
    click_xy(select_time_xy, run_event, stop_event)
    sleep_coop(0.2, run_event, stop_event)

    if pyautogui is not None:
        # Move cursor to end of field
        pyautogui.press("end")
        time.sleep(0.1)
        # Use clipboard paste (more reliable in CEF/web UI than pyautogui.write)
        try:
            import pyperclip
            pyperclip.copy(rental_hours_append)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            # Fallback: type character by character via press()
            for ch in rental_hours_append:
                pyautogui.press(ch)
                time.sleep(0.05)
        sleep_coop(0.2, run_event, stop_event)

    click_xy(cfg["coords"]["pay_card"], run_event, stop_event)
    sleep_coop(d(cfg, "ui_delay_long", 1.40), run_event, stop_event)

    # NOTE: Do NOT click "Создать" -> "Аренда" here after paying.
    # LoopManager handles navigation back to the vehicle list via
    # enter_create_rent (with proper cooldown).  Clicking here caused
    # a double/triple click on "Создать" — the second click would land
    # outside the intended popup and close it.
    sleep_coop(d(cfg, "post_ok_after_create_rent_delay", 1.2), run_event, stop_event)
    # Navigation back to Create->Rent is handled by LoopManager (enter_create_after_ok).
    # wait_for_vehicle_list_ready is called there after proper navigation.

    log(f"[{stem}] POST OK (+{price_value})")
    IdleTracker.mark_activity()

    hash_to_commit = h if (dedupe_policy == "on_success" and h) else None
    if hash_to_commit:
        ph = load_photo_hashes()
        ph[stem] = hash_to_commit
        save_photo_hashes(ph)

    return PostResult(status="OK", price_raw=price_raw, price_value=price_value, photo_hash_to_commit=hash_to_commit)



def format_pop_delta(cur_val: float, prev_val: float):
    """Return (display_str, numeric_for_sort). Safe standalone helper."""
    try:
        cur_val = float(cur_val)
        prev_val = float(prev_val)
        if prev_val <= 0.0:
            if cur_val <= 0.0:
                return "0%", 0.0
            return "NEW", 9_999_999.0
        delta = (cur_val - prev_val) / prev_val * 100.0
        return f"{delta:+.0f}%", float(delta)
    except Exception:
        return "—", -9_999_999.0

# ---------------- GUI ----------------

# ---------------- Telegram Rent Tracker (read-only) ----------------
# Reads your own Telegram account notifications (MTProto) and writes rentals.csv + rentals_summary.json into USER_DIR.
# Requires: pip install telethon
# IMPORTANT: keep your .session file private. Anyone with it can access your Telegram account.

_RENT_OUT_RE = re.compile(
    r"Транспорт\s+сдан\s+в\s+аренду!\s*.*?"
    r"Транспорт:\s*(?P<car>.+?)\s*"
    r"Номер\s+транспорта:\s*(?P<plate>\S+)\s*"
    r"Цена:\s*\$(?P<price>[\d\s]+)\s*"
    r"Длительность:\s*(?P<hours>\d+)\s*час",
    re.S | re.I
)
_RENT_RETURN_RE = re.compile(
    r"Транспорт\s+вернулся\s+с\s+аренды!\s*.*?"
    r"Транспорт:\s*(?P<car>.+?)\s*"
    r"Номер\s+транспорта:\s*(?P<plate>\S+)",
    re.S | re.I
)
_RENT_RENTER_RE = re.compile(r"Арендатор:\s*(?P<renter>.+)$", re.M)
_RENT_OUT_MARKER_RE = re.compile(r"(сдан\s+в\s+аренду|rent\s*out)", re.I)
_RENT_RETURN_MARKER_RE = re.compile(r"(вернулся\s+с\s+аренды|rent\s*return|return)", re.I)
_FIELD_CAR_RE = re.compile(r"^\s*[^\w\n]*\s*(?:Транспорт|Vehicle|Car)\s*:\s*(?P<car>.+)$", re.M | re.I)
_FIELD_PLATE_RE = re.compile(r"^\s*[^\w\n]*\s*(?:Номер\s+транспорта|Номер|Plate)\s*:\s*(?P<plate>\S+)", re.M | re.I)
_FIELD_PRICE_RE = re.compile(r"^\s*[^\w\n]*\s*(?:Цена|Стоимость|Price)\s*:\s*\$?(?P<price>[\d\s]+)", re.M | re.I)
_FIELD_HOURS_RE = re.compile(r"^\s*[^\w\n]*\s*(?:Длительность|Duration|Hours)\s*:\s*(?P<hours>\d+)", re.M | re.I)
_FIELD_RENTER_RE = re.compile(r"^\s*[^\w\n]*\s*(?:Арендатор|Renter)\s*:\s*(?P<renter>.+)$", re.M | re.I)

def _tg_match_field(regex: re.Pattern, text: str, field: str) -> str:
    try:
        m = regex.search(text)
        if not m:
            return ""
        return str(m.group(field) or "").strip()
    except Exception:
        return ""

def _parse_tg_price(value: str) -> Optional[int]:
    try:
        return int(value.replace(" ", "").replace(",", ""))
    except Exception:
        return None

def _parse_rent_out_fields(text: str) -> Optional[Dict[str, Any]]:
    m = _RENT_OUT_RE.search(text)
    if m:
        car = m.group("car").strip()
        plate = m.group("plate").strip()
        price = _parse_tg_price(m.group("price") or "")
        try:
            hours = int(m.group("hours"))
        except Exception:
            hours = None
        renter = ""
        mr = _RENT_RENTER_RE.search(text)
        if mr:
            renter = mr.group("renter").strip()
        if car and plate and price is not None and hours is not None:
            return {"car": car, "plate": plate, "price": price, "hours": hours, "renter": renter}
        return None

    if not _RENT_OUT_MARKER_RE.search(text):
        return None

    car = _tg_match_field(_FIELD_CAR_RE, text, "car")
    plate = _tg_match_field(_FIELD_PLATE_RE, text, "plate")
    price_raw = _tg_match_field(_FIELD_PRICE_RE, text, "price")
    hours_raw = _tg_match_field(_FIELD_HOURS_RE, text, "hours")
    renter = _tg_match_field(_FIELD_RENTER_RE, text, "renter")

    price = _parse_tg_price(price_raw)
    try:
        hours = int(hours_raw) if hours_raw else None
    except Exception:
        hours = None

    if car and plate and price is not None and hours is not None:
        return {"car": car, "plate": plate, "price": price, "hours": hours, "renter": renter}
    return None

def _parse_rent_return_fields(text: str) -> Optional[Dict[str, str]]:
    m = _RENT_RETURN_RE.search(text)
    if m:
        car = m.group("car").strip()
        plate = m.group("plate").strip()
        if plate:
            return {"car": car, "plate": plate}
        return None

    if not _RENT_RETURN_MARKER_RE.search(text):
        return None

    car = _tg_match_field(_FIELD_CAR_RE, text, "car")
    plate = _tg_match_field(_FIELD_PLATE_RE, text, "plate")
    if plate:
        return {"car": car, "plate": plate}
    return None

class TelegramRentTrackerManager:
    def __init__(self, user_dir: Path, log_fn):
        self.user_dir = user_dir
        self.log = log_fn
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._state = "stopped"   # stopped|starting|running|error
        self._detail = ""
        self._last_event = ""
        self._loop = None
        self._client = None

        self._name_map = {}
        self._unknown_queue = deque()
        self._unknown_seen = set()
    def status(self) -> Dict[str, str]:
        with self._lock:
            return {
                "state": self._state,
                "detail": self._detail,
                "last_event": self._last_event,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._state in ("starting", "running")

    @staticmethod
    def _norm_name(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^\[[^\]]+\]\s*", "", s)  # drop tags like [RL]
        s = re.sub(r"\s*\[[^\]]+\]\s*$", "", s)
        s = re.sub(r"\s*\([^)]+\)\s*$", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.casefold()

    def update_name_map(self, name_map: Dict[str, str]) -> None:
        """Update Majestic->local mapping without restart (thread-safe)."""
        nm: Dict[str, str] = {}
        try:
            if isinstance(name_map, dict):
                for k, v in name_map.items():
                    kk = str(k or "").strip()
                    vv = str(v or "").strip()
                    if kk and vv:
                        nm[self._norm_name(kk)] = vv
        except Exception:
            nm = {}
        with self._lock:
            self._name_map = nm

    def _resolve_car_key(self, car_raw: str) -> str:
        k = self._norm_name(car_raw)
        with self._lock:
            m = dict(self._name_map) if isinstance(self._name_map, dict) else {}
        return str(m.get(k, "") or "").strip()

    def _queue_unknown(self, car_raw: str) -> None:
        """Queue unknown Majestic car name for UI mapping dialog (deduped)."""
        raw = (car_raw or "").strip()
        if not raw:
            return
        k = self._norm_name(raw)
        with self._lock:
            if k in self._unknown_seen:
                return
            self._unknown_seen.add(k)
            self._unknown_queue.append(raw)

    def pop_unknown(self) -> Optional[str]:
        with self._lock:
            try:
                return self._unknown_queue.popleft()
            except Exception:
                return None



    def start(self, tg_cfg: Dict[str, Any]) -> None:
        if self.is_running():
            return
        if TelegramClient is None or events is None:
            with self._lock:
                self._state = "error"
                self._detail = "Telethon not installed (pip install telethon)"
            return

        api_id = int(tg_cfg.get("api_id") or 0)
        api_hash = str(tg_cfg.get("api_hash") or "").strip()
        if api_id <= 0 or not api_hash:
            with self._lock:
                self._state = "error"
                self._detail = "Missing api_id/api_hash"
            return

        self._stop.clear()
        with self._lock:
            self._state = "starting"
            self._detail = "Connecting..."
            self._last_event = ""

        self._thread = threading.Thread(
            target=self._run_thread,
            args=(dict(tg_cfg),),
            name="TelegramRentTracker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # best-effort async disconnect
        try:
            loop = None
            client = None
            with self._lock:
                loop = self._loop
                client = self._client
            if loop is not None and client is not None:
                def _kick():
                    try:
                        fut = client.disconnect()
                        if asyncio.iscoroutine(fut):
                            asyncio.create_task(fut)
                    except Exception:
                        pass
                loop.call_soon_threadsafe(_kick)
        except Exception:
            pass
        with self._lock:
            self._state = "stopped"
            self._detail = "Stopped"

    def _append_csv(self, csv_path: Path, row: Dict[str, Any]) -> None:
        """Append event row to CSV with stable header; auto-backs up legacy format once."""
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)

            fields = ["ts","type","car","car_key","plate","price","hours","revenue","renter"]
            header_fields = None
            if csv_path.exists():
                try:
                    with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
                        first = f.readline().strip()
                    if first:
                        header_fields = [h.strip() for h in first.split(",") if h.strip()]
                except Exception:
                    header_fields = None

            # Auto-upgrade legacy header -> backup and start fresh
            if header_fields and header_fields != fields:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = csv_path.with_name(csv_path.stem + f"_legacy_{stamp}" + csv_path.suffix)
                try:
                    shutil.move(str(csv_path), str(bak))
                    self.log(f"TG: CSV header upgraded (backup: {bak.name})")
                except Exception as e:
                    self.log(f"TG CSV backup failed: {e}")
                header_fields = None

            use_fields = header_fields if header_fields else fields
            out = {k: row.get(k, "") for k in use_fields}

            need_header = not csv_path.exists()
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=use_fields, extrasaction="ignore")
                if need_header:
                    w.writeheader()
                w.writerow(out)
        except Exception as e:
            self.log(f"TG CSV write error: {e}")

    def _load_summary(self, path: Path) -> Dict[str, Any]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
        except Exception:
            pass
        return {"by_plate": {}, "by_renter": {}, "totals": {"revenue": 0, "hours": 0, "events": 0}}

    def _save_summary(self, path: Path, data: Dict[str, Any]) -> None:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"TG summary write error: {e}")

    def _update_summary_rent_out(self, summary: Dict[str, Any], car_raw: str, car_key: str, plate: str, renter: str, price: int, hours: int) -> None:
        # IMPORTANT: Majestic bot "Цена" is already the total for the whole duration (NOT per-hour).
        revenue = int(price)

        by_plate = summary.setdefault("by_plate", {})
        by_renter = summary.setdefault("by_renter", {})
        by_car_key = summary.setdefault("by_car_key", {})
        unknown_cars = summary.setdefault("unknown_cars", {})
        totals = summary.setdefault("totals", {"revenue": 0, "hours": 0, "events": 0})

        # plate aggregate
        p = by_plate.get(plate) or {"revenue": 0, "hours": 0, "events": 0, "last_renter": "", "last_price": 0, "last_hours": 0, "last_car": ""}
        p["revenue"] += revenue
        p["hours"] += hours
        p["events"] += 1
        p["last_renter"] = renter
        p["last_price"] = price
        p["last_hours"] = hours
        p["last_car"] = car_raw
        by_plate[plate] = p

        # renter aggregate
        if renter:
            r = by_renter.get(renter) or {"revenue": 0, "hours": 0, "events": 0, "last_plate": "", "last_car": ""}
            r["revenue"] += revenue
            r["hours"] += hours
            r["events"] += 1
            r["last_plate"] = plate
            r["last_car"] = car_raw
            by_renter[renter] = r

        # car-key aggregate (mapped) or unknown bucket
        if car_key:
            c = by_car_key.get(car_key) or {"revenue": 0, "hours": 0, "events": 0, "last_car_raw": "", "last_plate": "", "last_renter": ""}
            c["revenue"] += revenue
            c["hours"] += hours
            c["events"] += 1
            c["last_car_raw"] = car_raw
            c["last_plate"] = plate
            c["last_renter"] = renter
            by_car_key[car_key] = c
        else:
            u = unknown_cars.get(car_raw) or {"events": 0, "last_plate": "", "last_price": 0, "last_hours": 0, "last_renter": ""}
            u["events"] += 1
            u["last_plate"] = plate
            u["last_price"] = price
            u["last_hours"] = hours
            u["last_renter"] = renter
            unknown_cars[car_raw] = u

        totals["revenue"] += revenue
        totals["hours"] += hours
        totals["events"] += 1

    def _run_thread(self, tg_cfg: Dict[str, Any]) -> None:
        try:
            asyncio.run(self._main_async(tg_cfg))
        except Exception as e:
            with self._lock:
                self._state = "error"
                self._detail = f"Crash: {e}"
            self.log(f"TG tracker crash: {e}")

    async def _main_async(self, tg_cfg: Dict[str, Any]) -> None:
        api_id = int(tg_cfg.get("api_id") or 0)
        api_hash = str(tg_cfg.get("api_hash") or "").strip()
        self.update_name_map(tg_cfg.get("name_map", {}))

        chat_contains = str(tg_cfg.get("chat_title_contains") or "Majestic").strip()
        session_name = str(tg_cfg.get("session_name") or "majestic_session").strip()
        out_csv_name = str(tg_cfg.get("output_csv") or "rentals.csv").strip()
        out_json_name = str(tg_cfg.get("output_json") or "rentals_summary.json").strip()

        session_path = str(self.user_dir / session_name)
        csv_path = self.user_dir / out_csv_name
        json_path = self.user_dir / out_json_name

        client = TelegramClient(session_path, api_id, api_hash)
        with self._lock:
            self._client = client
            self._loop = asyncio.get_running_loop()

        attempt = 0
        while True:
            try:
                await client.start()  # will ask for code/2FA on first run in console
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                attempt += 1
                wait_s = min(6.0, 1.0 * attempt)
                with self._lock:
                    self._state = "retrying"
                    self._detail = f"Session locked, retrying in {wait_s:.1f}s"
                self.log(f"TG session locked; retry in {wait_s:.1f}s")
                if self._stop.is_set():
                    return
                await asyncio.sleep(wait_s)
                continue
        with self._lock:
            self._state = "running"
            self._detail = "Connected"

        target = None
        if chat_contains:
            low = chat_contains.lower()
            async for d in client.iter_dialogs():
                title = (d.name or "").strip()
                if low in title.lower():
                    target = d.entity
                    break
        if target is None:
            with self._lock:
                self._state = "error"
                self._detail = f"Chat not found: '{chat_contains}'"
            self.log(f"TG: chat not found contains='{chat_contains}'. Open Telegram and check the dialog title.")
            await client.disconnect()
            return

        summary = self._load_summary(json_path)

        @client.on(events.NewMessage(chats=target))
        async def _handler(event):
            try:
                text = event.raw_text or ""
                ts = datetime.now().isoformat(timespec="seconds")
                rent_out = _parse_rent_out_fields(text)
                if rent_out:
                    car = rent_out["car"]
                    car_key = self._resolve_car_key(car)
                    if not car_key:
                        self._queue_unknown(car)
                    plate = rent_out["plate"]
                    price = int(rent_out["price"])
                    hours = int(rent_out["hours"])
                    renter = rent_out.get("renter", "")

                    row = {
                        "ts": ts,
                        "type": "RENT_OUT",
                        "car": car,
                        "car_key": car_key,
                        "plate": plate,
                        "price": price,
                        "hours": hours,
                        "revenue": price,
                        "renter": renter,
                    }
                    self._append_csv(csv_path, row)
                    self._update_summary_rent_out(summary, car, car_key, plate, renter, price, hours)
                    if car_key:
                        Stats.record_tg_rent(car_key, price, hours, renter, plate, car_raw=car)
                    self._save_summary(json_path, summary)

                    with self._lock:
                        self._last_event = f"OUT {plate} ${price} ({hours}h) ({renter})"
                    self.log(f"TG RENT_OUT: {plate} ${price} ({hours}h) {renter}")
                    return

                rent_ret = _parse_rent_return_fields(text)
                if rent_ret:
                    car = rent_ret.get("car", "")
                    plate = rent_ret.get("plate", "")
                    row = {
                        "ts": ts,
                        "type": "RENT_RETURN",
                        "car": car,
                        "plate": plate,
                    }
                    self._append_csv(csv_path, row)
                    with self._lock:
                        self._last_event = f"RETURN {plate}"
                    self.log(f"TG RENT_RETURN: {plate}")
            except Exception as e:
                self.log(f"TG parse error: {e}")

        # Keep loop alive until stop requested
        while not self._stop.is_set():
            await asyncio.sleep(0.5)

        await client.disconnect()
        with self._lock:
            self._state = "stopped"
            self._detail = "Stopped"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Stats: score weighting slider (created after Tk root exists)
        self.score_weight_var = tk.IntVar(master=self, value=50)

        self.cfg_lock = threading.Lock()
        self.cfg = ConfigManager.load()
        _DEBUG_SLOW["enabled"] = bool(self.cfg.get("debug_slow_mode", False))
        _DEBUG_SLOW["delay"] = float(self.cfg.get("debug_slow_delay", 1.5))

        # Telegram tracker (optional)
        self.tg_tracker = TelegramRentTrackerManager(USER_DIR, log)
        # auto-create telegram section in config.json (non-destructive)
        try:
            if CONFIG_PATH.exists():
                raw = CONFIG_PATH.read_text(encoding="utf-8", errors="ignore")
                if '"telegram"' not in raw:
                    ConfigManager.save(self.cfg)
        except Exception:
            pass
        try:
            tg = self.cfg.get("telegram", {})
            if isinstance(tg, dict) and tg.get("enabled"):
                self.tg_tracker.start(tg)
        except Exception:
            pass

        self.run_event = threading.Event()
        self.stop_event = threading.Event()
        # Событие простоя основного цикла аренды машин
        # Устанавливается когда бот ушёл в цикл ожидания (все машины выставлены/сданы)
        # Сбрасывается когда бот начинает реальную работу (итерация по шаблонам)
        self.loop_idle_event = threading.Event()
        # Флаг занятости монитора предметов — взводится ItemSaleMonitor перед full_monitor_cycle,
        # сбрасывается после. LoopManager ждёт его сброса перед началом sweep.
        self.items_busy_event = threading.Event()
        # UI/state helpers
        self._suppress_select_event = False
        self._selected_vehicle_cache = ""
        self._last_manual_select_ts = 0.0

        # Editor typing/dirty guards (prevents fields being overwritten while you type)
        self._editor_is_typing = False
        self._editor_dirty = False
        self._last_editor_loaded_vehicle = ""

        # Robust typing detection:
        # - Focus events are not always reliable; we also bump on KeyPress.
        self._editor_typing_deadline = 0.0
        self._editor_typing_job = None

        def _set_editor_typing(on: bool):
            try:
                self._editor_is_typing = bool(on)
            except Exception:
                pass

        def _bump_editor_typing():
            """Mark editor as typing (stays until FocusOut)."""
            try:
                self._editor_is_typing = True
            except Exception:
                pass

        def _mark_editor_dirty():
            try:
                self._editor_dirty = True
            except Exception:
                pass

        self._set_editor_typing = _set_editor_typing
        self._bump_editor_typing = _bump_editor_typing
        self._mark_editor_dirty = _mark_editor_dirty

        self.loop_thread: Optional[LoopManager] = None
        self._save_debounce_job = None

        self.colors = {
            "bg":      "#1a1a2e",
            "panel":   "#16213e",
            "panel2":  "#0f3460",
            "fg":      "#d4d4dc",
            "muted":   "#7a7a8e",
            "accent":  "#4a6fa5",
            "accent2": "#5b8a72",
            "success": "#4caf7c",
            "danger":  "#c0544e",
            "warning": "#c4903e",
            "border":  "#2a2a4a",
            "btn":     "#253554",
        }

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._bind_mousewheel_global()
        self._setup_hotkeys()

        self.after(150, self._pump_logs)
        self.after(900, self._refresh_stats)

        log(f"Starting app… v{SCRIPT_VERSION} user={USER_NAME}")
        log(f"User dir: {USER_DIR}")
        log(f"Config: {CONFIG_PATH}")
        log(f"Admin: {'YES' if is_admin() else 'NO'} | OpenCV(conf): {'YES' if HAS_OPENCV else 'NO'}")
        if missing:
            log("Missing modules: " + ", ".join(missing))

    # ---------- config live ----------

    def __getattr__(self, attr):
        """Safety-net for missing callbacks.

        Tkinter's tk.Tk defines __getattr__ that proxies unknown attributes into the internal tkapp.
        When a callback method is missing (e.g. self.clear_logs), Tkinter ends up raising
        AttributeError: '_tkinter.tkapp' object has no attribute '<name>' and crashes on startup.

        We intercept *likely callback names* and return a safe no-op callable instead, so the UI
        can still boot. Real implementations can be added later without breaking startup.
        """
        # Common patterns for our UI callbacks / actions
        if (
            attr in {
                "clear_logs",
                "open_charts",
                "open_log",
                "_on_reset_arm_changed",
                "_on_stats_heading_click",
                "_on_sort_changed",
                "_on_window_changed",
                "_on_search_changed",
            }
            or attr.startswith("_on_")
            or attr.endswith("_action")
            or attr.endswith("_handler")
        ):
            def _missing(*args, **kwargs):
                try:
                    log(f"[WARN] Missing handler: {attr} (called)")
                    if hasattr(self, "reset_status_var"):
                        # show small status when reset panel exists
                        try:
                            self.reset_status_var.set(f"Missing handler: {attr}")
                        except Exception:
                            pass
                except Exception:
                    pass
                return None
            return _missing

        # fallback to Tk's proxy behavior for genuine tk attributes
        return super().__getattr__(attr)

    def cfg_provider(self) -> Dict[str, Any]:
        with self.cfg_lock:
            return dict(self.cfg)

    def _apply_cfg_patch(self, patch: Dict[str, Any]) -> None:
        with self.cfg_lock:
            self.cfg.update(patch)
            try:
                _configure_runtime_env(self.cfg)
            except Exception:
                pass
        # Аккумулируем изменённые ключи между debounce'ами — не терять предыдущие.
        # Это ключевое для двухпроцессной архитектуры: при save мы пошлём
        # только эти ключи, всё остальное возьмётся из свежего файла (чтобы не затереть
        # изменения второго процесса, напр. телеграм-креды или bump_points).
        try:
            prev = getattr(self, "_last_cfg_patch_keys", None) or set()
            self._last_cfg_patch_keys = set(prev) | set(patch.keys())
        except Exception:
            self._last_cfg_patch_keys = set(patch.keys())
        self._debounced_save()

    def _apply_cfg_patch_with_keys(self, patch: Dict[str, Any], changed_keys: Optional[Set[str]]) -> None:
        with self.cfg_lock:
            self.cfg.update(patch)
            try:
                _configure_runtime_env(self.cfg)
            except Exception:
                pass
            _DEBUG_SLOW["enabled"] = bool(self.cfg.get("debug_slow_mode", False))
            _DEBUG_SLOW["delay"] = float(self.cfg.get("debug_slow_delay", 1.5))
        try:
            prev = getattr(self, "_last_cfg_patch_keys", None) or set()
            new_keys = set(changed_keys) if changed_keys else set(patch.keys())
            self._last_cfg_patch_keys = set(prev) | new_keys
        except Exception:
            self._last_cfg_patch_keys = set(changed_keys or patch.keys())
        self._debounced_save()

    def _debounced_save(self, ms: int = 260) -> None:
        if self._save_debounce_job is not None:
            try:
                self.after_cancel(self._save_debounce_job)
            except Exception:
                pass
        self._save_debounce_job = self.after(ms, self._save_now)

    def _save_now(self) -> None:
        self._save_debounce_job = None
        cfg = self.cfg_provider()
        try:
            changed_keys = getattr(self, "_last_cfg_patch_keys", None)
            self._last_cfg_patch_keys = None
            ConfigManager.save(cfg, changed_keys=changed_keys)
        except Exception as e:
            log(f"CONFIG save error: {e}")

    # ---------- UI setup ----------
    def _setup_window(self):
        self.title(f"Wiwang Poster — Авторазмещение — {USER_NAME}")
        self.geometry("1220x930")
        self.minsize(1080, 780)
        self.configure(bg=self.colors["bg"])

    def _setup_style(self):
        style = ttk.Style()
        # Use clam as base — most styleable theme
        try:
            style.theme_use("clam")
        except Exception:
            try:
                style.theme_use("default")
            except Exception:
                pass

        _bg      = self.colors["bg"]
        _panel   = self.colors["panel"]
        _panel2  = self.colors.get("panel2", "#0f3460")
        _fg      = self.colors["fg"]
        _muted   = self.colors["muted"]
        _accent  = self.colors["accent"]
        _accent2 = self.colors.get("accent2", "#5b8a72")
        _danger  = self.colors["danger"]
        _border  = self.colors["border"]
        _btn     = self.colors.get("btn", _panel)
        _success = self.colors.get("success", "#4caf7c")
        _warning = self.colors.get("warning", "#c4903e")

        # Slightly brighter accent for hover (lighten by blending with white)
        _accent_hover = "#5c82b8"
        _btn_hover    = "#2e4268"
        _danger_hover = "#a84540"

        # ── Root / Frame ──────────────────────────────────────────────────────
        style.configure("Root.TFrame",
                        background=_bg)
        style.configure("TFrame",
                        background=_bg)
        style.configure("Panel.TFrame",
                        background=_panel,
                        relief="flat")
        style.configure("Card.TFrame",
                        background=_panel,
                        relief="flat")

        # ── Notebook ──────────────────────────────────────────────────────────
        # Grafana-style: tabs are slightly raised with a clean bottom indicator
        style.configure("TNotebook",
                        background=_bg,
                        borderwidth=0,
                        tabmargins=[0, 4, 0, 0])
        style.configure("TNotebook.Tab",
                        background=_panel,
                        foreground=_muted,
                        padding=(16, 8),
                        font=("Segoe UI", 9),
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", _bg), ("active", _btn)],
                  foreground=[("selected", _fg), ("active", _fg)],
                  expand=[("selected", [1, 1, 1, 0])])

        # ── Labels ────────────────────────────────────────────────────────────
        style.configure("TLabel",
                        background=_bg,
                        foreground=_fg,
                        font=("Segoe UI", 9))
        style.configure("Muted.TLabel",
                        background=_bg,
                        foreground=_muted,
                        font=("Segoe UI", 9))
        style.configure("SectionTitle.TLabel",
                        background=_bg,
                        foreground=_fg,
                        font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TLabel",
                        background=_bg,
                        foreground=_accent,
                        font=("Segoe UI", 9, "bold"))
        style.configure("Success.TLabel",
                        background=_bg,
                        foreground=_success,
                        font=("Segoe UI", 9))
        style.configure("Danger.TLabel",
                        background=_bg,
                        foreground=_danger,
                        font=("Segoe UI", 9))
        style.configure("Warning.TLabel",
                        background=_bg,
                        foreground=_warning,
                        font=("Segoe UI", 9))
        style.configure("Panel.TLabel",
                        background=_panel,
                        foreground=_fg,
                        font=("Segoe UI", 9))
        style.configure("Panel.Muted.TLabel",
                        background=_panel,
                        foreground=_muted,
                        font=("Segoe UI", 9))

        # ── Buttons ───────────────────────────────────────────────────────────
        # Grafana-style: flat, calm — no harsh glows
        style.configure("TButton",
                        background=_btn,
                        foreground=_fg,
                        padding=(12, 6),
                        font=("Segoe UI", 9),
                        borderwidth=1,
                        relief="flat",
                        focusthickness=0,
                        focuscolor="none")
        style.map("TButton",
                  background=[("active", _btn_hover), ("pressed", _accent)],
                  foreground=[("active", _fg), ("pressed", _fg)],
                  relief=[("pressed", "flat")])

        style.configure("Accent.TButton",
                        background=_accent,
                        foreground="#ffffff",
                        padding=(12, 6),
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        focuscolor="none")
        style.map("Accent.TButton",
                  background=[("active", _accent_hover), ("pressed", _accent_hover)],
                  foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])

        style.configure("Success.TButton",
                        background=_success,
                        foreground="#ffffff",
                        padding=(12, 6),
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        focuscolor="none")
        style.map("Success.TButton",
                  background=[("active", "#3d9e6c"), ("pressed", "#2e7d55")],
                  foreground=[("active", "#ffffff")])

        style.configure("Danger.TButton",
                        background=_danger,
                        foreground="#ffffff",
                        padding=(12, 6),
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        focuscolor="none")
        style.map("Danger.TButton",
                  background=[("active", _danger_hover), ("pressed", "#8a3830")],
                  foreground=[("active", "#ffffff")])

        style.configure("Warning.TButton",
                        background=_warning,
                        foreground="#ffffff",
                        padding=(12, 6),
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        focuscolor="none")
        style.map("Warning.TButton",
                  background=[("active", "#b07c2e"), ("pressed", "#8c6022")],
                  foreground=[("active", "#ffffff")])

        # ── Entry ─────────────────────────────────────────────────────────────
        # Grafana-style: clean inset, subtle border
        style.configure("TEntry",
                        fieldbackground=_panel,
                        foreground=_fg,
                        insertcolor=_accent,
                        borderwidth=1,
                        relief="flat",
                        padding=(6, 5),
                        font=("Segoe UI", 9))
        style.map("TEntry",
                  fieldbackground=[("focus", _panel)],
                  bordercolor=[("focus", _accent), ("!focus", _border)])
        style.configure("Search.TEntry",
                        fieldbackground=_panel,
                        foreground=_fg,
                        insertcolor=_accent,
                        borderwidth=1,
                        relief="flat",
                        padding=(6, 5),
                        font=("Segoe UI", 9))
        style.map("Search.TEntry",
                  fieldbackground=[("focus", _panel)],
                  bordercolor=[("focus", _accent), ("!focus", _border)])

        # ── Scrollbar ─────────────────────────────────────────────────────────
        style.configure("TScrollbar",
                        background=_border,
                        troughcolor=_bg,
                        arrowcolor=_muted,
                        borderwidth=0,
                        relief="flat")
        style.map("TScrollbar",
                  background=[("active", _accent), ("pressed", _accent)])

        # ── Treeview ──────────────────────────────────────────────────────────
        # Grafana-style: alternating row shading, calm accent selection
        style.configure("Treeview",
                        background=_panel,
                        fieldbackground=_panel,
                        foreground=_fg,
                        rowheight=26,
                        borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=_panel2,
                        foreground=_muted,
                        font=("Segoe UI", 9, "bold"),
                        relief="flat",
                        padding=(8, 5))
        style.map("Treeview",
                  background=[("selected", _accent)],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading",
                  background=[("active", _btn)],
                  foreground=[("active", _fg)])

        # ── Checkbutton ───────────────────────────────────────────────────────
        style.configure("TCheckbutton",
                        background=_bg,
                        foreground=_fg,
                        font=("Segoe UI", 9),
                        focuscolor="none")
        style.map("TCheckbutton",
                  background=[("active", _bg)],
                  foreground=[("active", _accent)])
        style.configure("Panel.TCheckbutton",
                        background=_panel,
                        foreground=_fg,
                        font=("Segoe UI", 9),
                        focuscolor="none")
        style.map("Panel.TCheckbutton",
                  background=[("active", _panel)],
                  foreground=[("active", _accent)])

        # ── Radiobutton ───────────────────────────────────────────────────────
        style.configure("TRadiobutton",
                        background=_bg,
                        foreground=_fg,
                        font=("Segoe UI", 9),
                        focuscolor="none")
        style.map("TRadiobutton",
                  background=[("active", _bg)],
                  foreground=[("active", _accent)])

        # ── Combobox ──────────────────────────────────────────────────────────
        style.configure("TCombobox",
                        fieldbackground=_panel,
                        background=_btn,
                        foreground=_fg,
                        arrowcolor=_muted,
                        borderwidth=1,
                        relief="flat",
                        padding=(6, 5),
                        font=("Segoe UI", 9))
        style.map("TCombobox",
                  fieldbackground=[("readonly", _panel), ("focus", _panel)],
                  foreground=[("readonly", _fg)],
                  selectbackground=[("readonly", _btn)],
                  selectforeground=[("readonly", _fg)])

        # ── Spinbox ───────────────────────────────────────────────────────────
        style.configure("TSpinbox",
                        fieldbackground=_panel,
                        background=_btn,
                        foreground=_fg,
                        arrowcolor=_muted,
                        borderwidth=1,
                        relief="flat",
                        padding=(6, 5),
                        font=("Segoe UI", 9))

        # ── Progressbar ───────────────────────────────────────────────────────
        style.configure("TProgressbar",
                        background=_accent,
                        troughcolor=_border,
                        borderwidth=0,
                        thickness=5)
        style.configure("Success.TProgressbar",
                        background=_success,
                        troughcolor=_border,
                        borderwidth=0,
                        thickness=5)
        style.configure("Danger.TProgressbar",
                        background=_danger,
                        troughcolor=_border,
                        borderwidth=0,
                        thickness=5)

        # ── Scale ─────────────────────────────────────────────────────────────
        style.configure("TScale",
                        background=_bg,
                        troughcolor=_border,
                        sliderlength=14)

        # ── Separator ─────────────────────────────────────────────────────────
        style.configure("TSeparator",
                        background=_border)

        # ── LabelFrame ────────────────────────────────────────────────────────
        style.configure("TLabelframe",
                        background=_panel,
                        bordercolor=_border,
                        relief="flat")
        style.configure("TLabelframe.Label",
                        background=_panel,
                        foreground=_muted,
                        font=("Segoe UI", 9, "bold"))

    def _make_scrollable_tab(self, parent: tk.Widget, *, bg: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=bg)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        inner = tk.Frame(canvas, bg=bg)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(_e=None):
            try:
                canvas.itemconfigure(win, width=canvas.winfo_width())
            except Exception:
                pass

        def _sync_scroll(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        return inner

    def _setup_hotkeys(self):
        f12_mode = str(self.cfg.get("hotkey_f12_action", "stop")).strip().lower()
        f12_label = "pause/resume" if f12_mode == "pause" else "stop"

        def _f12_action():
            if f12_mode == "pause":
                self.toggle_pause_resume()
            else:
                self.stop_hard()

        try:
            self.bind_all("<F12>", lambda _e=None: _f12_action(), add="+")
            self.bind_all("<F9>", lambda _e=None: self.stop_hard(), add="+")
        except Exception:
            pass
        hotkey_ready = False
        if Listener is not None and Key is not None:
            try:
                def _on_press(key):
                    if key == Key.f12:
                        self.after(0, _f12_action)
                    elif key == Key.f9:
                        self.after(0, self.stop_hard)

                self._pynput_listener = Listener(on_press=_on_press)
                self._pynput_listener.daemon = True
                self._pynput_listener.start()
                log(f"Hotkeys: F12 {f12_label}, F9 stop (pynput)")
                hotkey_ready = True
            except Exception as e:
                log(f"Hotkey setup failed (pynput): {e}")
        if (not hotkey_ready) and keyboard is not None:
            try:
                keyboard.add_hotkey("f12", _f12_action)
                keyboard.add_hotkey("f9", self.stop_hard)
                log(f"Hotkeys: F12 {f12_label}, F9 stop")
                hotkey_ready = True
            except Exception as e:
                log(f"Hotkey setup failed (keyboard): {e}")
        self._bind_mousewheel_global()

    def _bind_mousewheel_global(self):
        def _has_class_binding(widget, sequence: str) -> bool:
            try:
                return bool(widget.bind_class(widget.winfo_class(), sequence))
            except Exception:
                return False

        def _widget_handles_scroll(widget) -> bool:
            if not hasattr(widget, "yview_scroll"):
                return False
            return any(_has_class_binding(widget, seq) for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"))

        def _find_scroll_target(widget):
            current = widget
            while current is not None:
                if hasattr(current, "yview_scroll"):
                    return current
                try:
                    parent_name = current.winfo_parent()
                except Exception:
                    break
                if not parent_name:
                    break
                try:
                    current = current.nametowidget(parent_name)
                except Exception:
                    break
            return None

        def _scroll(event):
            try:
                widget = self.winfo_containing(event.x_root, event.y_root)
            except Exception:
                widget = None
            if widget is None:
                return
            if _widget_handles_scroll(widget):
                return
            target = _find_scroll_target(widget)
            if target is None:
                return
            try:
                if event.delta:
                    target.yview_scroll(int(-1 * (event.delta / 120)), "units")
                elif event.num == 4:
                    target.yview_scroll(-3, "units")
                elif event.num == 5:
                    target.yview_scroll(3, "units")
            except Exception:
                return
            return "break"

        self.bind_all("<MouseWheel>", _scroll, add="+")
        self.bind_all("<Button-4>", _scroll, add="+")
        self.bind_all("<Button-5>", _scroll, add="+")

    def _panel(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=self.colors["panel"], highlightbackground=self.colors["border"], highlightthickness=1)

    def _label(self, parent, text: str, muted: bool = False, bold: bool = False) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            fg=(self.colors["muted"] if muted else self.colors["fg"]),
            bg=self.colors["panel"],
            font=("Segoe UI", 10, ("bold" if bold else "normal")),
        )

    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_run = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_tune = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_coords = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_stats = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_logs = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_tg = tk.Frame(self.nb, bg=self.colors["bg"])
        self.tab_config = tk.Frame(self.nb, bg=self.colors["bg"])

        self.nb.add(self.tab_run, text="▶ Запуск")
        self.nb.add(self.tab_tune, text="⚙ Тюнинг")
        self.nb.add(self.tab_coords, text="📍 Координаты")
        self.nb.add(self.tab_stats, text="📊 Статистика")
        self.nb.add(self.tab_tg, text="📡 Telegram")
        self.nb.add(self.tab_config, text="⚙ Конфиг")
        self.nb.add(self.tab_logs, text="📋 Журнал")

        # ── Items Monitor (Admin mode) ──────────────────────────────────────────
        self._item_monitor: Optional["ItemSaleMonitor"] = None
        self._item_sale_tab: Optional["ItemSaleTab"] = None
        if _ITEM_MONITOR_AVAILABLE:
            try:
                self.tab_items = tk.Frame(self.nb, bg=self.colors["bg"])
                self.nb.add(self.tab_items, text="🔍 Монитор")
            except Exception:
                pass

        self.tab_run_inner = self._make_scrollable_tab(self.tab_run, bg=self.colors["bg"])
        self._build_run_tab()
        self._build_tune_tab()
        self._build_coords_tab()
        try:
            self._update_plate_anchor_status()
        except Exception:
            pass
        self._build_stats_tab()
        self._build_tg_tab()
        self._build_config_tab()
        self._build_logs_tab()
        self._build_items_tab()  # Items Monitor (Admin mode)
        try:
            self.nb.select(self.tab_stats)
        except Exception:
            pass

    # ---------- Run tab ----------
    def _build_run_tab(self):
        parent = getattr(self, "tab_run_inner", self.tab_run)

        # ── TOP STATUS BANNER ────────────────────────────────────────────────
        banner = tk.Frame(parent, bg=self.colors["panel"],
                          highlightthickness=1, highlightbackground=self.colors["border"])
        banner.pack(fill="x", padx=10, pady=(10, 4))
        # Accent top stripe on banner
        tk.Frame(banner, bg=self.colors["accent"], height=3).pack(fill="x", side="top")

        banner_inner = tk.Frame(banner, bg=self.colors["panel"])
        banner_inner.pack(fill="x", padx=14, pady=10)

        # Title
        tk.Label(banner_inner, text="▶ Wiwang Poster — Авторазмещение",
                 fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        # Status indicator group (right side)
        status_grp = tk.Frame(banner_inner, bg=self.colors["panel"])
        status_grp.pack(side="right")

        self.status_var = tk.StringVar(value="⏹ ОСТАНОВЛЕН")
        self._pulse_canvas = tk.Canvas(status_grp, width=16, height=16,
                                        bg=self.colors["panel"], highlightthickness=0)
        self._pulse_canvas.pack(side="left", padx=(0, 6))
        self._pulse_dot = self._pulse_canvas.create_oval(3, 3, 13, 13,
                                                          fill=self.colors["panel2"], outline="")
        self._pulse_state = False
        self.after(350, self._pulse_tick)

        tk.Label(status_grp, textvariable=self.status_var,
                 fg=self.colors["accent"], bg=self.colors["panel"],
                 font=("Segoe UI", 12, "bold"), width=12, anchor="w").pack(side="left")

        # ── METRIC CARDS ROW ─────────────────────────────────────────────────
        cards_row = tk.Frame(parent, bg=self.colors["bg"])
        cards_row.pack(fill="x", padx=10, pady=(4, 4))

        _c = self.colors
        _card_defs = [
            ("tg_today_var",   "💰 Доход сегодня",   "—",  _c["accent"]),
            ("tg_active_cnt",  "🚗 Активных аренд",  "—",  _c["success"]),
            ("fastscan_vis",   "👁 Машин онлайн",   "—",  _c["accent2"]),
            ("decision_cnt",   "⚖ Решений принято",  "—",  _c["warning"]),
        ]
        self._run_metric_vars = {}
        for attr, title, init, accent_col in _card_defs:
            card = tk.Frame(cards_row, bg=_c["panel"],
                            highlightthickness=1, highlightbackground=_c["border"])
            card.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            # Left accent bar (Grafana card style)
            tk.Frame(card, bg=accent_col, width=4).pack(fill="y", side="left")
            body = tk.Frame(card, bg=_c["panel"])
            body.pack(fill="both", expand=True, padx=10, pady=8)
            tk.Label(body, text=title.upper(), bg=_c["panel"],
                     fg=_c["muted"], font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value=init)
            self._run_metric_vars[attr] = var
            tk.Label(body, textvariable=var, bg=_c["panel"],
                     fg=_c["fg"], font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(2, 0))

        # ── CONTROL BUTTONS ──────────────────────────────────────────────────
        ctrl = tk.Frame(parent, bg=self.colors["panel"],
                        highlightthickness=1, highlightbackground=self.colors["border"])
        ctrl.pack(fill="x", padx=10, pady=4)
        tk.Frame(ctrl, bg=self.colors["accent"], height=2).pack(fill="x", side="top")

        btns = tk.Frame(ctrl, bg=self.colors["panel"])
        btns.pack(fill="x", padx=12, pady=10)

        # Primary action buttons — bigger and more prominent
        ttk.Button(btns, text="▶  Запустить / Продолжить", style="Success.TButton",
                   command=self.start_or_resume).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="⏸  Пауза", style="Warning.TButton",
                   command=self.pause).pack(side="left", padx=6)
        ttk.Button(btns, text="⏹  СТОП  (F9)", style="Danger.TButton",
                   command=self.stop_hard).pack(side="left", padx=6)

        # Divider
        tk.Frame(btns, bg=self.colors["border"], width=1).pack(side="left", fill="y", padx=10)

        ttk.Button(btns, text="💥 Нюдж фокус", command=self.nudge_now).pack(side="left", padx=4)
        ttk.Button(btns, text="👁 Виз.отладка", command=self.open_visual_debug).pack(side="left", padx=4)
        ttk.Button(btns, text="🔄 Сброс обработанных", command=self.reset_processed).pack(side="left", padx=4)
        ttk.Button(btns, text="🔄 Сброс хэшей", command=self.reset_photo_hashes).pack(side="left", padx=4)

        ttk.Button(btns, text="📂 Открыть папку", command=self.open_user_dir).pack(side="right", padx=4)
        ttk.Button(btns, text="📊 archive.csv", command=self.open_archive).pack(side="right", padx=4)

        # ── RECENT ACTIVITY FEED ─────────────────────────────────────────────
        feed_frame = tk.Frame(parent, bg=self.colors["panel"],
                              highlightthickness=1, highlightbackground=self.colors["border"])
        feed_frame.pack(fill="x", padx=10, pady=4)
        feed_hdr = tk.Frame(feed_frame, bg=self.colors["panel"])
        feed_hdr.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(feed_hdr, text="ПОСЛЕДНИЕ СОБЫТИЯ", bg=self.colors["panel"],
                 fg=self.colors["muted"], font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Frame(feed_hdr, bg=self.colors["border"], height=1).pack(fill="x", side="bottom", pady=(4, 0))

        self._run_feed_list = tk.Listbox(
            feed_frame,
            height=8,
            bg=self.colors["panel2"],
            fg=self.colors["fg"],
            selectbackground=self.colors["accent"],
            activestyle="none",
            relief="flat",
            font=("Consolas", 9),
            highlightthickness=0,
        )
        feed_sb = ttk.Scrollbar(feed_frame, orient="vertical",
                                command=self._run_feed_list.yview)
        self._run_feed_list.configure(yscrollcommand=feed_sb.set)
        self._run_feed_list.pack(side="left", fill="x", expand=True, padx=(12, 0), pady=(0, 10))
        feed_sb.pack(side="left", fill="y", pady=(0, 10))
        self._run_feed_entries = []  # list of (ts_str, text) tuples
        self.after(900, self._refresh_run_feed)

        # Anchor + blacklists section (below the feed)
        self._build_run_tab_anchor_section(parent)

    def _refresh_run_feed(self):
        """Keep Run tab feed in sync with recent log events and live metrics cards."""
        try:
            # Update live metric cards from shared state
            now_ts = time.time()
            try:
                income = tg_rent_tracker.income_summary(now_ts)
                self._run_metric_vars["tg_today_var"].set(f"${income.get('today', 0):.0f}")
            except Exception:
                pass
            try:
                active = tg_rent_tracker.active_rentals(now_ts)
                self._run_metric_vars["tg_active_cnt"].set(str(active.get("count", 0)))
            except Exception:
                pass
            try:
                with METRICS_LOCK:
                    vis = METRICS_STATE.get("fastscan_visible", 0)
                self._run_metric_vars["fastscan_vis"].set(str(vis))
            except Exception:
                pass
            try:
                with METRICS_LOCK:
                    cands = METRICS_STATE.get("candidates_found", 0)
                self._run_metric_vars["decision_cnt"].set(str(cands))
            except Exception:
                pass

            # Drain recent log lines into feed (max 10 shown)
            try:
                new_lines = []
                while True:
                    line = LOG_QUEUE.get_nowait()
                    ts = time.strftime("%H:%M:%S")
                    new_lines.append(f"[{ts}]  {line}")
            except Exception:
                pass
            if new_lines:
                self._run_feed_entries.extend(new_lines)
                self._run_feed_entries = self._run_feed_entries[-10:]  # keep last 10
                try:
                    self._run_feed_list.delete(0, "end")
                    for entry in self._run_feed_entries:
                        self._run_feed_list.insert("end", entry)
                    self._run_feed_list.see("end")
                except Exception:
                    pass
        except Exception:
            pass
        if not getattr(self, "_closing", False):
            self.after(900, self._refresh_run_feed)

    def _build_run_tab_anchor_section(self, parent):
        """Build the form anchor section (called from _build_run_tab continuation)."""
        anchor = self._panel(parent)
        anchor.pack(fill="x", padx=10, pady=10)

        top = tk.Frame(anchor, bg=self.colors["panel"])
        top.pack(fill="x", padx=12, pady=(12, 6))
        self._label(top, "📌 Якорь формы (рекомендуется)", bold=True).pack(side="left")
        self.anchor_status = tk.StringVar(value=("✅ Установлен" if ANCHOR_FORM_PATH.exists() else "❌ НЕ ЗАДАН"))
        tk.Label(top, textvariable=self.anchor_status,
                 fg=(self.colors["accent"] if ANCHOR_FORM_PATH.exists() else self.colors["danger"]),
                 bg=self.colors["panel"], font=("Segoe UI", 10, "bold")).pack(side="right")

        tip = tk.Frame(anchor, bg=self.colors["panel"])
        tip.pack(fill="x", padx=12, pady=(0, 10))
        self._label(
            tip,
            "💡 Якорь нужен, чтобы бот определял открыта ли форма размещения.\n"
            "Как: Открой форму объявления, наведи мышь на стабильный элемент формы и нажми \u00abПривязать».",
            muted=True,
        ).pack(anchor="w")

        act = tk.Frame(anchor, bg=self.colors["panel"])
        act.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(act, text="📌 Привязать якорь", command=self.capture_form_anchor).pack(side="left", padx=6)
        ttk.Button(act, text="🗑 Удалить якорь", command=self.delete_form_anchor).pack(side="left", padx=6)

        # --- Blacklists (skip posting / skip renting) ---
        bl = ttk.LabelFrame(parent, text="🚫 Чёрный список (не размещать / не сдавать)")
        bl.pack(fill="both", expand=False, padx=10, pady=(0, 10))

        # helpers (local callbacks keep GUI robust)
        def _refresh_blacklists_ui():
            try:
                self.bl_vehicle_list.delete(0, "end")
            except Exception:
                return

            blv = [str(x).strip() for x in (self.cfg.get("blacklist_vehicles") or []) if str(x).strip()]
            for s in blv:
                self.bl_vehicle_list.insert("end", s)

            # plates
            self._bl_plate_items = []
            try:
                self.bl_plate_list.delete(0, "end")
            except Exception:
                pass
            for it in (self.cfg.get("plate_blacklist") or []):
                try:
                    if isinstance(it, dict):
                        label = str(it.get("label") or "").strip() or "plate"
                        path = str(it.get("path") or it.get("file") or "").strip()
                    else:
                        label = Path(str(it)).stem
                        path = str(it)
                    if not path:
                        continue
                    self._bl_plate_items.append({"label": label, "path": path})
                    self.bl_plate_list.insert("end", f"{label} | {Path(path).name}")
                except Exception:
                    continue

        def _bl_add_vehicle():
            s = (self.bl_vehicle_entry.get() or "").strip()
            if not s:
                return
            cur = [str(x).strip() for x in (self.cfg.get("blacklist_vehicles") or []) if str(x).strip()]
            if s not in cur:
                cur.append(s)
                self._apply_cfg_patch({"blacklist_vehicles": cur})
            self.bl_vehicle_entry.set("")
            _refresh_blacklists_ui()

        def _bl_remove_vehicle_selected():
            try:
                sel = list(self.bl_vehicle_list.curselection())
            except Exception:
                sel = []
            if not sel:
                return
            cur = [str(x).strip() for x in (self.cfg.get("blacklist_vehicles") or []) if str(x).strip()]
            keep = [v for i, v in enumerate(cur) if i not in sel]
            self._apply_cfg_patch({"blacklist_vehicles": keep})
            _refresh_blacklists_ui()

        def _bl_clear_vehicles():
            self._apply_cfg_patch({"blacklist_vehicles": []})
            _refresh_blacklists_ui()

        def _bl_capture_vehicle_to_blacklist():
            """Select a screen region (car card or name), auto-match it to a template, and add to vehicle blacklist."""
            if pyautogui is None:
                messagebox.showerror("Missing", "pyautogui not installed")
                return

            thr = float(self.cfg.get("vehicle_blacklist_capture_threshold", 0.80) or 0.80)

            def _done(region):
                if not region:
                    return
                try:
                    shot = pyautogui.screenshot(region=(int(region[0]), int(region[1]), int(region[2]), int(region[3])))
                except Exception as e:
                    messagebox.showerror("Capture", f"Failed to capture region: {e}")
                    return

                stem, score = match_capture_to_templates(self.cfg, shot)
                ts = time.strftime("%Y%m%d_%H%M%S")
                safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", (stem or "unknown"))[:40].strip("_") or "unknown"
                fn = f"{ts}_{safe}_{score:.3f}.png"
                out = VEHICLE_BLACKLIST_DIR / fn
                try:
                    shot.save(out)
                except Exception:
                    pass

                if stem and score >= thr:
                    cur = [str(x).strip() for x in (self.cfg.get("blacklist_vehicles") or []) if str(x).strip()]
                    if stem not in cur:
                        cur.append(stem)
                        self._apply_cfg_patch({"blacklist_vehicles": cur})
                        _refresh_blacklists_ui()
                    try:
                        messagebox.showinfo("Blacklist", f"Added to vehicle blacklist: {stem} (score={score:.3f})")
                    except Exception:
                        pass
                else:
                    try:
                        messagebox.showwarning(
                            "Blacklist",
                            f"Couldn't confidently identify template (best={stem or 'none'} score={score:.3f}).\n"
                            f"Saved capture to: {out.name}\n"
                            "You can manually type the template name and press Add."
                        )
                    except Exception:
                        pass

            def _open(_pos=None):
                try:
                    RegionDrawer(self, title="Capture vehicle (card/name) for blacklist", on_done=_done, fixed_size=None)
                except Exception as e:
                    messagebox.showerror("Draw", f"Cannot open drawer: {e}")

            self._run_capture_sequence("Draw vehicle blacklist", _open, show_ui_before=True)

        def _bl_capture_plate_to_blacklist():
            # Requires you to be on the FORM screen (plate visible).
            def _capture(_pos=None):
                live, dbg = _grab_plate_value_live(self.cfg)
                if live is None:
                    messagebox.showwarning("Plate capture", "Can't capture plate. Set PLATE region and keep the form visible.")
                    return
                label = (self.bl_plate_label.get() or "").strip() or "plate"
                safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", label)[:40].strip("_") or "plate"
                ts = time.strftime("%Y%m%d_%H%M%S")
                fn = f"{ts}_{safe}.png"
                out = PLATE_BLACKLIST_DIR / fn
                try:
                    live.save(out)
                except Exception as e:
                    messagebox.showerror("Plate capture", f"Failed to save: {e}")
                    return
                cur = list(self.cfg.get("plate_blacklist") or [])
                cur.append({"label": label, "path": str(Path("plate_blacklist") / fn)})
                self._apply_cfg_patch({"plate_blacklist": cur})
                self.bl_plate_label.set("")
                _refresh_blacklists_ui()
                messagebox.showinfo("Plate blacklist", f"Saved: {out}")

            self._run_capture_sequence("Capture plate blacklist", _capture, show_ui_before=False)

        def _bl_remove_plate_selected():
            try:
                sel = list(self.bl_plate_list.curselection())
            except Exception:
                sel = []
            if not sel:
                return
            cur = list(self.cfg.get("plate_blacklist") or [])
            keep = [v for i, v in enumerate(cur) if i not in sel]
            self._apply_cfg_patch({"plate_blacklist": keep})
            _refresh_blacklists_ui()

        def _bl_clear_plates():
            self._apply_cfg_patch({"plate_blacklist": []})
            _refresh_blacklists_ui()

        def _bl_open_plate_folder():
            try:
                p = PLATE_BLACKLIST_DIR
                if sys.platform.startswith("win"):
                    os.startfile(str(p))  # type: ignore[attr-defined]
            except Exception:
                pass

        # Vehicles list
        row1 = ttk.Frame(bl)
        row1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row1, text="🚗 Чёрный список машин (не размещать) — сопоставление по изображению:").pack(anchor="w")
        ttk.Label(row1, text="💡 Как: открой СПИСОк → обведи прямоугольник на карточке машины → Нажми «Захватить+Добавить».").pack(anchor="w")

        veh_box = ttk.Frame(row1)
        veh_box.pack(fill="x")
        self.bl_vehicle_list = tk.Listbox(veh_box, height=6, selectmode="extended")
        veh_sb = ttk.Scrollbar(veh_box, orient="vertical", command=self.bl_vehicle_list.yview)
        self.bl_vehicle_list.configure(yscrollcommand=veh_sb.set)
        self.bl_vehicle_list.pack(side="left", fill="x", expand=True)
        veh_sb.pack(side="left", fill="y")

        veh_ctl = ttk.Frame(bl)
        veh_ctl.pack(fill="x", padx=8, pady=(4, 8))
        self.bl_vehicle_entry = tk.StringVar(value="")
        ttk.Entry(veh_ctl, textvariable=self.bl_vehicle_entry).pack(side="left", fill="x", expand=True)
        ttk.Button(veh_ctl, text="➕ Добавить", command=_bl_add_vehicle).pack(side="left", padx=6)
        ttk.Button(veh_ctl, text="📷 Захватить+Добавить", command=_bl_capture_vehicle_to_blacklist).pack(side="left")
        ttk.Button(veh_ctl, text="➖ Удалить выбранные", command=_bl_remove_vehicle_selected).pack(side="left")
        ttk.Button(veh_ctl, text="🗑 Очистить", command=_bl_clear_vehicles).pack(side="left", padx=6)

        sep = ttk.Separator(bl, orient="horizontal")
        sep.pack(fill="x", padx=8, pady=(2, 6))

        # Plates list
        row2 = ttk.Frame(bl)
        row2.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(row2, text="🔖 Чёрный список номеров (НЕ СДАВАТЬ) — сопоставление по значению номера:").pack(anchor="w")
        ttk.Label(row2, text="💡 Как: Коорд→Область НОМЕРА + Якорь \u00abГос.Номер:\u00bb → открой ФОРМУ → Нажми «Захватить».").pack(anchor="w")

        plate_box = ttk.Frame(row2)
        plate_box.pack(fill="x")
        self.bl_plate_list = tk.Listbox(plate_box, height=6, selectmode="extended")
        plate_sb = ttk.Scrollbar(plate_box, orient="vertical", command=self.bl_plate_list.yview)
        self.bl_plate_list.configure(yscrollcommand=plate_sb.set)
        self.bl_plate_list.pack(side="left", fill="x", expand=True)
        plate_sb.pack(side="left", fill="y")

        plate_ctl = ttk.Frame(bl)
        plate_ctl.pack(fill="x", padx=8, pady=(4, 8))
        self.bl_plate_label = tk.StringVar(value="")
        ttk.Entry(plate_ctl, textvariable=self.bl_plate_label).pack(side="left", fill="x", expand=True)
        ttk.Button(plate_ctl, text="📷 Захватить номер", command=_bl_capture_plate_to_blacklist).pack(side="left", padx=6)
        ttk.Button(plate_ctl, text="➖ Удалить выбранные", command=_bl_remove_plate_selected).pack(side="left")
        ttk.Button(plate_ctl, text="🗑 Очистить", command=_bl_clear_plates).pack(side="left", padx=6)

        thr_row = ttk.Frame(bl)
        thr_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(thr_row, text="Порог совпадения номера:").pack(side="left")
        self.bl_plate_thr = tk.DoubleVar(value=float(self.cfg.get("plate_blacklist_confidence", 0.94)))
        thr_spin = ttk.Spinbox(thr_row, from_=0.80, to=0.99, increment=0.01, textvariable=self.bl_plate_thr, width=6)
        thr_spin.pack(side="left", padx=6)
        ttk.Button(thr_row, text="🧪 Тест", command=self._bl_test_plate_now).pack(side="left", padx=8)
        def _save_thr(*_a):
            try:
                self._apply_cfg_patch({"plate_blacklist_confidence": float(self.bl_plate_thr.get())})
            except Exception:
                pass
        try:
            thr_spin.configure(command=_save_thr)
        except Exception:
            pass
        ttk.Button(thr_row, text="📂 Открыть папку", command=_bl_open_plate_folder).pack(side="right")

        _refresh_blacklists_ui()

        self._build_live_metrics_panel()
        self._build_limits_panel()


    def _build_live_metrics_panel(self):
        parent = getattr(self, "tab_run_inner", self.tab_run)
        box = ttk.LabelFrame(parent, text="📊 ЛАЙВ МЕТРИКИ")
        box.pack(fill="x", padx=10, pady=(0, 10))

        self.tg_status_var = tk.StringVar(value="📡 TG-трекер: —")
        self.tg_income_var = tk.StringVar(value="💰 Доход: —")
        self.tg_active_var = tk.StringVar(value="🚗 Активных аренд: —")
        self.plate_registry_var = tk.StringVar(value="🔖 Реестр номеров: —")
        self.fastscan_var = tk.StringVar(value="🔍 Быстроскан: —")
        self.decision_var = tk.StringVar(value="⚖ Счётчики решений: —")

        tk.Label(box, textvariable=self.tg_status_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=(6, 2))
        tk.Label(box, textvariable=self.plate_registry_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=2)
        tk.Label(box, textvariable=self.fastscan_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=2)
        tk.Label(box, textvariable=self.tg_active_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=2)
        tk.Label(box, textvariable=self.tg_income_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=2)
        tk.Label(box, textvariable=self.decision_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w", padx=10, pady=(2, 6))

        self.after(800, self._refresh_live_metrics)

    def _refresh_live_metrics(self):
        try:
            now_ts = time.time()
            tg_status = tg_rent_tracker.get_status()
            tg_enabled = bool(tg_status.get("enabled"))
            tg_state = tg_status.get("state", "stopped")
            tg_last = tg_status.get("last_event_ts")
            last_str = "—"
            if tg_last:
                try:
                    last_dt = datetime.fromisoformat(str(tg_last))
                    last_str = last_dt.strftime("%H:%M:%S")
                except Exception:
                    last_str = str(tg_last)
            self.tg_status_var.set(f"📡 TG-трекер: {'вкл' if tg_enabled else 'выкл'} | сост. = {tg_state} | последнее событие = {last_str}")

            plate_enabled = plate_registry.is_enabled()
            plate_count = plate_registry.count_entries()
            self.plate_registry_var.set(f"🔖 Реестр номеров: {'вкл' if plate_enabled else 'выкл'} | записей = {plate_count}")

            with METRICS_LOCK:
                last_scan = METRICS_STATE.get("fastscan_last_ts", 0.0)
                visible = METRICS_STATE.get("fastscan_visible", 0)
                free = METRICS_STATE.get("fastscan_free", 0)
                counters = {
                    "candidates_found": METRICS_STATE.get("candidates_found", 0),
                    "blocked_never_rent": METRICS_STATE.get("blocked_never_rent", 0),
                    "blocked_over_limit": METRICS_STATE.get("blocked_over_limit", 0),
                    "blocked_no_plate": METRICS_STATE.get("blocked_no_plate", 0),
                    "blocked_low_confidence": METRICS_STATE.get("blocked_low_confidence", 0),
                    "blocked_unknown_plate": METRICS_STATE.get("blocked_unknown_plate", 0),
                }

            scan_str = "—"
            if last_scan:
                try:
                    scan_str = time.strftime("%H:%M:%S", time.localtime(float(last_scan)))
                except Exception:
                    scan_str = str(last_scan)
            self.fastscan_var.set(f"🔍 Быстроскан: последний = {scan_str} | видно = {visible} | свободно = {free}")

            active = tg_rent_tracker.active_rentals(now_ts)
            next_end = active.get("next_end_ts")
            next_str = "—"
            if next_end:
                try:
                    next_str = time.strftime("%H:%M:%S", time.localtime(float(next_end)))
                except Exception:
                    next_str = str(next_end)
            self.tg_active_var.set(f"🚗 Активных аренд (TG): {active.get('count', 0)} | окончание = {next_str}")

            income = tg_rent_tracker.income_summary(now_ts)
            self.tg_income_var.set(f"💰 Доход: сегодня = {income.get('today', 0):.0f} | 7 дней = {income.get('week', 0):.0f}")

            self.decision_var.set(
                "⚖ Счётчики решений: "
                f"найдено = {counters['candidates_found']} | "
                f"запрещ (аренда) = {counters['blocked_never_rent']} | "
                f"превышен лимит = {counters['blocked_over_limit']} | "
                f"нет номера = {counters['blocked_no_plate']} | "
                f"низкая уверенность = {counters['blocked_low_confidence']} | "
                f"неизвестный номер = {counters['blocked_unknown_plate']}"
            )
        except Exception:
            pass
        finally:
            try:
                if not getattr(self, "_closing", False):
                    self.after(800, self._refresh_live_metrics)
            except Exception:
                pass

    def _build_limits_panel(self):
        parent = getattr(self, "tab_run_inner", self.tab_run)
        box = ttk.LabelFrame(parent, text="⚖ ЛИМИТЫ / ПРАВИЛА")
        box.pack(fill="x", padx=10, pady=(0, 10))

        limits = _limits_cfg(self.cfg)

        def _update_limits(key: str, value: Any) -> None:
            cur = dict(_limits_cfg(self.cfg))
            cur[key] = value
            self._apply_cfg_patch({"limits": cur})

        def _update_flag(section: str, key: str, value: Any) -> None:
            cur = dict(self.cfg.get(section, {}) if isinstance(self.cfg.get(section), dict) else {})
            cur[key] = value
            self._apply_cfg_patch({section: cur})

        row0 = tk.Frame(box, bg=self.colors["panel"])
        row0.pack(fill="x", padx=10, pady=(6, 2))

        self.tg_toggle_var = tk.BooleanVar(value=bool(self.cfg.get("tg_tracker_cfg", {}).get("enabled", False)))
        ttk.Checkbutton(row0, text="📡 Включить TG-трекер", variable=self.tg_toggle_var,
                        command=lambda: _update_flag("tg_tracker_cfg", "enabled", bool(self.tg_toggle_var.get()))).pack(side="left", padx=6)

        self.plate_toggle_var = tk.BooleanVar(value=bool(self.cfg.get("plate_registry_cfg", {}).get("enabled", False)))
        ttk.Checkbutton(row0, text="🔖 Включить реестр номеров", variable=self.plate_toggle_var,
                        command=lambda: _update_flag("plate_registry_cfg", "enabled", bool(self.plate_toggle_var.get()))).pack(side="left", padx=6)

        env_tip = tk.Label(row0, text="ENV: TG_TRACKER_ENABLED / PLATE_REGISTRY_ENABLED", fg=self.colors["muted"], bg=self.colors["panel"])
        env_tip.pack(side="right", padx=6)

        map_box = ttk.LabelFrame(box, text="🔄 Связь номер → ключ машины")
        map_box.pack(fill="x", padx=10, pady=(8, 6))

        map_list_frame = tk.Frame(map_box, bg=self.colors["panel"])
        map_list_frame.pack(fill="x", padx=8, pady=(6, 4))
        self.plate_map_list = tk.Listbox(map_list_frame, height=5, selectmode="extended")
        map_sb = ttk.Scrollbar(map_list_frame, orient="vertical", command=self.plate_map_list.yview)
        self.plate_map_list.configure(yscrollcommand=map_sb.set)
        self.plate_map_list.pack(side="left", fill="x", expand=True)
        map_sb.pack(side="left", fill="y")

        map_form = tk.Frame(map_box, bg=self.colors["panel"])
        map_form.pack(fill="x", padx=8, pady=(0, 6))
        self.map_plate_var = tk.StringVar(value="")
        self.map_vehicle_var = tk.StringVar(value="")
        ttk.Entry(map_form, textvariable=self.map_plate_var, width=12).pack(side="left", padx=(0, 6))
        ttk.Entry(map_form, textvariable=self.map_vehicle_var, width=18).pack(side="left", padx=(0, 6))

        def _refresh_plate_map():
            try:
                self.plate_map_list.delete(0, "end")
            except Exception:
                return
            data = plate_registry.list_mappings()
            for plate, entry in sorted(data.items()):
                vehicle_key = ""
                if isinstance(entry, dict):
                    vehicle_key = str(entry.get("vehicle_key") or "")
                self.plate_map_list.insert("end", f"{plate} -> {vehicle_key}")

        def _add_plate_map():
            plate = (self.map_plate_var.get() or "").strip()
            vehicle = (self.map_vehicle_var.get() or "").strip()
            if not plate or not vehicle:
                return
            if plate_registry.upsert_mapping(plate, vehicle):
                self.map_plate_var.set("")
                self.map_vehicle_var.set("")
                _refresh_plate_map()

        def _remove_plate_map():
            try:
                sel = list(self.plate_map_list.curselection())
            except Exception:
                sel = []
            if not sel:
                return
            for idx in reversed(sel):
                try:
                    item = self.plate_map_list.get(idx)
                    plate = str(item).split("->", 1)[0].strip()
                    if plate:
                        plate_registry.remove_mapping(plate)
                except Exception:
                    continue
            _refresh_plate_map()

        ttk.Button(map_form, text="➕ Добавить / Обновить", command=_add_plate_map).pack(side="left", padx=(6, 6))
        ttk.Button(map_form, text="➖ Удалить выбранные", command=_remove_plate_map).pack(side="left")
        ttk.Button(map_form, text="🔄 Обновить", command=_refresh_plate_map).pack(side="left", padx=(6, 0))

        _refresh_plate_map()

        row1 = tk.Frame(box, bg=self.colors["panel"])
        row1.pack(fill="x", padx=10, pady=2)

        self.hard_block_unknown_var = tk.BooleanVar(value=bool(limits.get("hard_block_unknown_plate", False)))
        ttk.Checkbutton(row1, text="🚫 Блок неизвестных номеров", variable=self.hard_block_unknown_var,
                        command=lambda: _update_limits("hard_block_unknown_plate", bool(self.hard_block_unknown_var.get()))).pack(side="left", padx=6)

        self.hard_block_no_plate_var = tk.BooleanVar(value=bool(limits.get("hard_block_no_plate", False)))
        ttk.Checkbutton(row1, text="🚫 Блок без номера", variable=self.hard_block_no_plate_var,
                        command=lambda: _update_limits("hard_block_no_plate", bool(self.hard_block_no_plate_var.get()))).pack(side="left", padx=6)

        row2 = tk.Frame(box, bg=self.colors["panel"])
        row2.pack(fill="x", padx=10, pady=2)

        self.use_tg_truth_var = tk.BooleanVar(value=bool(limits.get("use_tg_active_truth", False)))
        ttk.Checkbutton(row2, text="📡 TG-аренды как источник истины", variable=self.use_tg_truth_var,
                        command=lambda: _update_limits("use_tg_active_truth", bool(self.use_tg_truth_var.get()))).pack(side="left", padx=6)

        self.use_fastscan_truth_var = tk.BooleanVar(value=bool(limits.get("use_fastscan_truth", False)))
        ttk.Checkbutton(row2, text="🔍 Быстроскан как источник истины", variable=self.use_fastscan_truth_var,
                        command=lambda: _update_limits("use_fastscan_truth", bool(self.use_fastscan_truth_var.get()))).pack(side="left", padx=6)

        row3 = tk.Frame(box, bg=self.colors["panel"])
        row3.pack(fill="x", padx=10, pady=2)

        self.max_active_var = tk.StringVar(value=str(limits.get("max_active_rentals_per_vehicle") or ""))
        self.cooldown_var = tk.StringVar(value=str(limits.get("cooldown_minutes_after_return") or ""))
        self.max_daily_var = tk.StringVar(value=str(limits.get("max_daily_hours") or ""))
        self.max_weekly_var = tk.StringVar(value=str(limits.get("max_weekly_hours") or ""))

        tk.Label(row3, text="Макс. аренд на машину", bg=self.colors["panel"], fg=self.colors["fg"]).pack(side="left", padx=(0, 6))
        max_active_entry = ttk.Entry(row3, textvariable=self.max_active_var, width=8)
        max_active_entry.pack(side="left")
        tk.Label(row3, text="Охлаждение (мин)", bg=self.colors["panel"], fg=self.colors["fg"]).pack(side="left", padx=(12, 6))
        cooldown_entry = ttk.Entry(row3, textvariable=self.cooldown_var, width=8)
        cooldown_entry.pack(side="left")
        tk.Label(row3, text="Макс. часов/день", bg=self.colors["panel"], fg=self.colors["fg"]).pack(side="left", padx=(12, 6))
        max_daily_entry = ttk.Entry(row3, textvariable=self.max_daily_var, width=8)
        max_daily_entry.pack(side="left")
        tk.Label(row3, text="Макс. часов/неделю", bg=self.colors["panel"], fg=self.colors["fg"]).pack(side="left", padx=(12, 6))
        max_weekly_entry = ttk.Entry(row3, textvariable=self.max_weekly_var, width=8)
        max_weekly_entry.pack(side="left")

        def _apply_numeric_limits(*_a):
            def _num(val):
                v = str(val or "").strip()
                return float(v) if v else None
            _update_limits("max_active_rentals_per_vehicle", _num(self.max_active_var.get()))
            _update_limits("cooldown_minutes_after_return", _num(self.cooldown_var.get()))
            _update_limits("max_daily_hours", _num(self.max_daily_var.get()))
            _update_limits("max_weekly_hours", _num(self.max_weekly_var.get()))

        for entry in (max_active_entry, cooldown_entry, max_daily_entry, max_weekly_entry):
            entry.bind("<FocusOut>", _apply_numeric_limits, add="+")

        row4 = tk.Frame(box, bg=self.colors["panel"])
        row4.pack(fill="x", padx=10, pady=(2, 6))
        tk.Label(row4, text="Мин. уверенность номера", bg=self.colors["panel"], fg=self.colors["fg"]).pack(side="left", padx=(0, 6))
        self.min_conf_var = tk.StringVar(value=str(limits.get("min_plate_confidence") or ""))
        min_conf_entry = ttk.Entry(row4, textvariable=self.min_conf_var, width=8)
        min_conf_entry.pack(side="left")

        def _apply_min_conf(*_a):
            v = str(self.min_conf_var.get() or "").strip()
            _update_limits("min_plate_confidence", float(v) if v else None)

        min_conf_entry.bind("<FocusOut>", _apply_min_conf, add="+")


    def _make_section_frame(self, parent, title: str) -> tk.Frame:
        """Create a bordered labeled section frame for the Tuning tab."""
        outer = tk.Frame(parent, bg=self.colors["panel"],
                         highlightthickness=1, highlightbackground=self.colors["border"])
        outer.pack(fill="x", padx=0, pady=(0, 8))
        # Section header
        hdr = tk.Frame(outer, bg=self.colors["panel2"])
        hdr.pack(fill="x")
        tk.Label(hdr, text=title.upper(), bg=self.colors["panel2"],
                 fg=self.colors["muted"], font=("Segoe UI", 8, "bold")).pack(
            side="left", padx=10, pady=5)
        body = tk.Frame(outer, bg=self.colors["panel"])
        body.pack(fill="x", padx=8, pady=(4, 8))
        return body

    # ---------- Tuning tab (LIVE sliders, scrollable) ----------
    def _build_tune_tab(self):
        outer = self._panel(self.tab_tune)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        canvas = tk.Canvas(outer, bg=self.colors["panel"], highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        inner = tk.Frame(canvas, bg=self.colors["panel"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(_e=None):
            try:
                canvas.itemconfigure(win, width=canvas.winfo_width())
            except Exception:
                pass

        def _on_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            _sync_width()

        inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _sync_width)

        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        cfg = self.cfg_provider()

        def row_scale(parent, title, key, frm, to, fmt="{:.2f}"):
            r = tk.Frame(parent, bg=self.colors["panel"])
            r.pack(fill="x", padx=12, pady=7)

            self._label(r, title, bold=True).pack(side="left")
            val = tk.StringVar(value=fmt.format(float(cfg.get(key, 0.0))))

            def on_move(v):
                try:
                    f = float(v)
                except Exception:
                    return
                val.set(fmt.format(f))
                self._apply_cfg_patch({key: f})

            s = ttk.Scale(r, from_=frm, to=to, orient="horizontal", command=on_move)
            s.set(float(cfg.get(key, (frm + to) / 2)))
            s.pack(side="left", fill="x", expand=True, padx=12)

            tk.Label(r, textvariable=val, fg=self.colors["muted"], bg=self.colors["panel"],
                     font=("Segoe UI", 10, "bold"), width=9, anchor="e").pack(side="right")
            return s

        def row_choice(parent, title, key, options):
            r = tk.Frame(parent, bg=self.colors["panel"])
            r.pack(fill="x", padx=12, pady=7)
            self._label(r, title, bold=True).pack(side="left")

            var = tk.StringVar(value=str(cfg.get(key, options[0])))

            def on_sel(*_):
                self._apply_cfg_patch({key: var.get()})

            om = ttk.OptionMenu(r, var, var.get(), *options, command=lambda *_: on_sel())
            om.pack(side="left", padx=12)
            return var

        def row_bool(parent, title, key):
            r = tk.Frame(parent, bg=self.colors["panel"])
            r.pack(fill="x", padx=12, pady=7)
            var = tk.BooleanVar(value=bool(cfg.get(key, False)))

            def on_toggle():
                self._apply_cfg_patch({key: bool(var.get())})

            cb = ttk.Checkbutton(r, text=title, variable=var, command=on_toggle)
            cb.pack(side="left")
            return var

        def _format_list(value: Any) -> str:
            if isinstance(value, (list, tuple)):
                return ", ".join(str(v) for v in value)
            return str(value or "")

        def _parse_float_list(raw: str) -> Optional[List[float]]:
            text = str(raw or "").strip()
            if not text:
                return []
            cleaned = text.strip().strip("[]()")
            parts = [p for p in re.split(r"[,\s;]+", cleaned) if p]
            if not parts:
                return []
            out: List[float] = []
            for part in parts:
                try:
                    out.append(float(part))
                except Exception:
                    return None
            return out

        # ── SECTION: Behavior ─────────────────────────────────────────────────
        sec_behavior = self._make_section_frame(inner, "📋 ПОВЕДЕНИЕ  —  режим работы бота")
        row_bool(sec_behavior, "Бесконечный цикл  (loop_mode: авторазмещение без остановки)", "loop_mode")
        row_choice(sec_behavior, "Дедупликация объявлений", "dedupe_policy", ["off", "on_success", "always"])
        row_bool(sec_behavior, "Принудит. дедупликация в цикле  (не рекомендуется)", "dedupe_force_in_loop")

        # --- Debug Slow Mode: checkbox + delay slider in one row ---
        _dsm_row = tk.Frame(sec_behavior, bg=self.colors["panel"])
        _dsm_row.pack(fill="x", padx=4, pady=5)
        _dsm_var = tk.BooleanVar(value=bool(cfg.get("debug_slow_mode", False)))
        _dsm_delay_val = tk.StringVar(value="{:.1f}s".format(float(cfg.get("debug_slow_delay", 1.5))))

        def _dsm_toggle():
            self._apply_cfg_patch({"debug_slow_mode": bool(_dsm_var.get())})

        def _dsm_on_move(v):
            try:
                f = float(v)
            except Exception:
                return
            _dsm_delay_val.set("{:.1f}s".format(f))
            self._apply_cfg_patch({"debug_slow_delay": f})

        ttk.Checkbutton(_dsm_row, text="🐢 МЕДЛЕННЫЙ РЕЖИМ (отладка)", variable=_dsm_var,
                        command=_dsm_toggle).pack(side="left")
        _dsm_scale = ttk.Scale(_dsm_row, from_=0.5, to=5.0, orient="horizontal",
                               command=_dsm_on_move)
        _dsm_scale.set(float(cfg.get("debug_slow_delay", 1.5)))
        _dsm_scale.pack(side="left", fill="x", expand=True, padx=12)
        tk.Label(_dsm_row, textvariable=_dsm_delay_val, fg=self.colors["muted"],
                 bg=self.colors["panel"], font=("Segoe UI", 10, "bold"),
                 width=9, anchor="e").pack(side="right")

        row_scale(sec_behavior, "⚡ МАСТЕР-СКОРОСТЬ  (выше = быстрее)", "speed", 0.35, 3.00, fmt="{:.2f}x")

        # ── SECTION: Timing ──────────────────────────────────────────────────
        sec_timing = self._make_section_frame(inner, "⏱ ТАЙМИНГИ  —  все значения BASE (÷ скорость)")
        row_scale(sec_timing, "Задержка после клика на машину", "after_vehicle_click_delay", 0.4, 6.0, fmt="{:.2f}s")
        row_scale(sec_timing, "Таймаут ожидания формы", "form_anchor_timeout", 2.0, 20.0, fmt="{:.1f}s")
        row_scale(sec_timing, "Интервал между постами (мин)", "post_interval_min", 0.1, 10.0, fmt="{:.2f}s")
        row_scale(sec_timing, "Интервал между постами (макс)", "post_interval_max", 0.1, 10.0, fmt="{:.2f}s")
        row_scale(sec_timing, "Задержка повтора при не найдено", "not_found_retry_delay", 1.0, 60.0, fmt="{:.1f}s")
        row_scale(sec_timing, "Задержка повтора при ошибке", "error_retry_delay", 0.5, 20.0, fmt="{:.1f}s")
        row_scale(sec_timing, "Пауза между циклами", "cycle_sleep", 0.2, 5.0, fmt="{:.2f}s")
        row_scale(sec_timing, "Задержка открытия диалога файла", "file_dialog_open_delay", 0.6, 6.0, fmt="{:.2f}s")
        row_scale(sec_timing, "Задержка после ввода пути файла", "file_dialog_after_enter_path", 0.2, 4.0, fmt="{:.2f}s")

        # ── SECTION: Recognition ─────────────────────────────────────────────
        sec_recog = self._make_section_frame(inner, "🔍 РАСПОЗНАВАНИЕ / БЫСТРОСКАН")
        row_scale(sec_recog, "Порог совпадения шаблонов", "confidence", 0.60, 0.99, fmt="{:.3f}")
        row_bool(sec_recog, "Быстроскан: только видимые", "fast_scan_visible_only")
        row_bool(sec_recog, "Быстроскан: предварительная сборка", "fast_scan_prebuild_current")
        row_bool(sec_recog, "Быстроскан: полный экран при промахе", "fast_scan_fullscreen_on_miss")
        row_scale(sec_recog, "Быстроскан: мин. порог резервного поиска", "fast_scan_fallback_min", 0.40, 0.95, fmt="{:.2f}")
        row_bool(sec_recog, "Поиск машины полным экраном при промахе", "vehicle_locate_fullscreen_on_miss")
        row_scale(sec_recog, "Кол-во попыток поиска полным экраном", "vehicle_locate_fullscreen_tries", 1.0, 5.0, fmt="{:.0f}")

        # ── SECTION: Input Reliability ─────────────────────────────────────────
        sec_input = self._make_section_frame(inner, "⌨ ВВОД ТЕКСТА  —  клавиатура / буфер обмена")
        row_bool(sec_input, "Игнорировать раскладку клавиатуры  (RU/EN: безопасная вставка)", "ignore_keyboard_layout")
        row_bool(sec_input, "Проверять вставку  (Ctrl+A/Ctrl+C)", "paste_verify")
        row_bool(sec_input, "Разрешить непроверенную вставку  (резерв)", "paste_allow_unverified")
        row_bool(sec_input, "Запрос номера вручную при ошибке чтения", "plate_read_prompt_on_fail")
        row_scale(sec_input, "Задержка фокусировки поля", "field_focus_delay", 0.02, 0.60, fmt="{:.2f}s")
        row_scale(sec_input, "Попыток фокусировки поля", "field_retries", 1, 5, fmt="{:.0f}")
        row_scale(sec_input, "Попыток вставки", "paste_retries", 1, 5, fmt="{:.0f}")
        row_scale(sec_input, "Задержка между попытками вставки", "paste_retry_delay", 0.02, 0.50, fmt="{:.2f}s")
        row_scale(sec_input, "Задержка непроверенной вставки", "paste_unverified_delay", 0.02, 0.50, fmt="{:.2f}s")

        # ── SECTION: Bump (My Ads) ────────────────────────────────────────────
        sec_bump = self._make_section_frame(inner, "⬆ БАМП  —  клик в Мои Объявления при простое")
        row_bool(sec_bump, "Включить бамп", "bump_enabled")
        row_scale(sec_bump, "Бамп после простоя (сек, 90=1:30)", "bump_idle_after", 10.0, 600.0, fmt="{:.0f}s")
        row_scale(sec_bump, "Задержка клика бампа", "bump_click_delay", 0.10, 3.00, fmt="{:.2f}s")
        row_scale(sec_bump, "Задержка возврата бампа", "bump_back_delay", 0.10, 3.00, fmt="{:.2f}s")

        # Footer hint
        tip = tk.Frame(inner, bg=self.colors["panel"])
        tip.pack(fill="x", padx=4, pady=(4, 12))
        self._label(
            tip,
            "⚡ Все значения BASE. Реальные задержки = BASE ÷ скорость. Изменения применяются мгновенно.",
            muted=True,
        ).pack(anchor="w")

        skip_roots = {
            "coords",
            "telegram",
            "tg_tracker_cfg",
        }
        skip_paths = {
            ("car_dir",),
            ("blacklist_vehicles",),
            ("plate_blacklist",),
            ("plate_blacklist_confidence",),
            ("bump_points",),
            ("bump_use_points",),
            ("vehicle_region",),
            ("plate_region",),
            ("plate_label_anchor_region",),
            ("stall_region",),
            ("stall_timeout_s",),
            ("tg_tracker_cfg", "enabled"),
        }
        sections = self._collect_settings_sections(
            exclude_roots=skip_roots,
            exclude_paths=skip_paths,
            allowed_sections={
                "📋 Цикл / Поведение",
                "⏱ Тайминги",
                "⌨ Ввод / Раскладка",
                "📂 Диалог файлов",
                "⬆ Бамп",
                "🖥 Интерфейс",
                "⚙ Прочее",
            },
        )
        self._build_settings_panel(
            inner,
            title="Дополнительные настройки (LIVE) — Тюнинг",
            sections=sections,
            padx=12,
            pady=(8, 12),
        )

    # ---------- Coords tab ----------
    def _build_coords_tab(self):
        p = self._panel(self.tab_coords)
        p.pack(fill="both", expand=True, padx=10, pady=10)

        top = tk.Frame(p, bg=self.colors["panel"])
        top.pack(fill="x", padx=12, pady=12)
        self._label(top, "📂 Папка с шаблонами машин:", bold=True).pack(side="left")
        self.car_dir_var = tk.StringVar(value=str(self.cfg.get("car_dir", r"C:\sale\car")))
        e = tk.Entry(top, textvariable=self.car_dir_var, width=62,
                     bg=self.colors["panel2"], fg=self.colors["fg"],
                     insertbackground=self.colors["fg"], relief="flat")
        e.pack(side="left", padx=10)

        def on_car_dir_change(*_):
            self._apply_cfg_patch({"car_dir": self.car_dir_var.get().strip()})

        self.car_dir_var.trace_add("write", on_car_dir_change)
        ttk.Button(top, text="📁 Обзор…", command=self.browse_car_dir).pack(side="left")

        self._label(p, "📍 Захват: нажми Захватить → наведи мышь на нужную точку → подожди 3 сек.", muted=True)\
            .pack(anchor="w", padx=12, pady=(0, 8))

        body = tk.Frame(p, bg=self.colors["panel"])
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        canvas = tk.Canvas(body, bg=self.colors["panel"], highlightthickness=0)
        sb = tk.Scrollbar(body, orient="vertical", command=canvas.yview,
                          bg=self.colors["panel"], troughcolor=self.colors["panel2"])
        inner = tk.Frame(canvas, bg=self.colors["panel"])
        inner.bind("<Configure>", lambda e2: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.coord_vars: Dict[str, Tuple[tk.StringVar, tk.StringVar]] = {}
        coords = self.cfg.get("coords", {})

        # Русские названия координат
        _COORD_NAMES_RU = {
            "create":          "Кнопка «Создать»",
            "create_rent":     "Кнопка «Аренда»",
            "comment":         "Поле комментария",
            "price":           "Поле цены",
            "submit":          "Кнопка «Опубликовать»",
            "confirm":         "Кнопка «Подтвердить»",
            "back":            "Кнопка «Назад»",
            "photo":           "Поле фото",
            "search":          "Поле поиска",
            "first_item":      "Первый элемент",
            "my_ads":          "Мои объявления",
            "my_ads_btn":      "Кнопка «Мои объявл.»",
        }

        row = 0
        for key in sorted(coords.keys()):
            x, y = coords[key]
            xv = tk.StringVar(value=str(x))
            yv = tk.StringVar(value=str(y))
            self.coord_vars[key] = (xv, yv)

            display_name = _COORD_NAMES_RU.get(key, key)
            tk.Label(inner, text=display_name, fg=self.colors["fg"], bg=self.colors["panel"],
                     font=("Segoe UI", 10, "bold"), width=22, anchor="w").grid(row=row, column=0, sticky="w", pady=5)

            ex = tk.Entry(inner, textvariable=xv, width=7, bg=self.colors["panel2"], fg=self.colors["fg"],
                          insertbackground=self.colors["fg"], relief="flat")
            ey = tk.Entry(inner, textvariable=yv, width=7, bg=self.colors["panel2"], fg=self.colors["fg"],
                          insertbackground=self.colors["fg"], relief="flat")
            ex.grid(row=row, column=1, padx=(8, 6))
            ey.grid(row=row, column=2, padx=(0, 12))

            def apply_coords_change(*_):
                coords_new = dict(self.cfg_provider().get("coords", {}))
                for k2, (xv2, yv2) in self.coord_vars.items():
                    try:
                        coords_new[k2] = [int(xv2.get()), int(yv2.get())]
                    except Exception:
                        pass
                self._apply_cfg_patch({"coords": coords_new})

            xv.trace_add("write", apply_coords_change)
            yv.trace_add("write", apply_coords_change)

            ttk.Button(inner, text="📌 Захватить", command=lambda k=key: self.capture_coord(k)).grid(row=row, column=3, padx=6)
            ttk.Button(inner, text="🖱 Тест", command=lambda k=key: self.test_click(k)).grid(row=row, column=4, padx=6)

            row += 1

        # ---------------- Extra capture: regions + bump points ----------------
        # separator
        tk.Frame(inner, bg=self.colors["panel2"], height=1).grid(row=row, column=0, columnspan=5, sticky="ew", pady=(12, 12))
        row += 1

        
        tk.Label(inner, text="🔍 Область поиска машин:", fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=28, anchor="w").grid(row=row, column=0, sticky="w", pady=4)

        self.vehicle_region_var = tk.StringVar(value=str(self.cfg.get("vehicle_region")))
        tk.Label(inner, textvariable=self.vehicle_region_var, fg=self.colors["muted"], bg=self.colors["panel"],
                 font=("Consolas", 9), width=28, anchor="w").grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        btnv = tk.Frame(inner, bg=self.colors["panel"]); btnv.grid(row=row, column=3, padx=6, sticky="w")
        ttk.Button(btnv, text="🖊 Нарисовать", command=self.draw_vehicle_region).pack(fill="x")
        ttk.Button(btnv, text="🗑 Очистить", command=lambda: self._apply_cfg_patch({"vehicle_region": None}) or self._refresh_regions_ui()).pack(fill="x", pady=(4,0))
        row += 1

        tk.Label(inner, text="🔖 Область гос. номера (180x40):", fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=28, anchor="w").grid(row=row, column=0, sticky="w", pady=4)

        self.plate_region_var = tk.StringVar(value=str(self.cfg.get("plate_region")))
        tk.Label(inner, textvariable=self.plate_region_var, fg=self.colors["muted"], bg=self.colors["panel"],
                 font=("Consolas", 9), width=28, anchor="w").grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        btnp = tk.Frame(inner, bg=self.colors["panel"]); btnp.grid(row=row, column=3, padx=6, sticky="w")
        ttk.Button(btnp, text="📌 Захватить", command=self.capture_plate_region).pack(fill="x")
        ttk.Button(btnp, text="🖊 Нарисовать", command=self.draw_plate_region).pack(fill="x", pady=(4,0))
        row += 1

        tk.Label(inner, text="📌 Якорь «Гос.Номер:»:", fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=28, anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        self.plate_anchor_status_var = tk.StringVar(value=("✅ Установлен" if PLATE_LABEL_ANCHOR_PATH.exists() else "❌ Отсутствует"))
        tk.Label(inner, textvariable=self.plate_anchor_status_var, fg=self.colors["muted"], bg=self.colors["panel"],
                 font=("Consolas", 9), width=28, anchor="w").grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        btna = tk.Frame(inner, bg=self.colors["panel"]); btna.grid(row=row, column=3, padx=6, sticky="w")
        ttk.Button(btna, text="📌 Захватить", command=self.capture_plate_label_anchor).pack(fill="x")
        ttk.Button(btna, text="🗑 Очистить", command=self.clear_plate_label_anchor).pack(fill="x", pady=(4,0))
        row += 1

        tk.Label(inner, text="👁 Область сторожевика (не поиск машин):", fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=28, anchor="w").grid(row=row, column=0, sticky="w", pady=4)

        self.stall_region_var = tk.StringVar(value=str(self.cfg.get("stall_region")))
        tk.Label(inner, textvariable=self.stall_region_var, fg=self.colors["muted"], bg=self.colors["panel"],
                 font=("Consolas", 9), width=28, anchor="w").grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        btns = tk.Frame(inner, bg=self.colors["panel"]); btns.grid(row=row, column=3, padx=6, sticky="w")
        ttk.Button(btns, text="📌 Захватить", command=self.capture_stall_region).pack(fill="x")
        ttk.Button(btns, text="🖊 Нарисовать", command=self.draw_stall_region).pack(fill="x", pady=(4,0))

        # watchdog timeout input
        self.stall_timeout_var = tk.StringVar(value=str(self.cfg.get("stall_timeout_s", 25)))
        tk.Entry(inner, textvariable=self.stall_timeout_var, width=6, bg=self.colors["panel2"], fg=self.colors["fg"],
                 insertbackground=self.colors["fg"], relief="flat").grid(row=row, column=4, padx=6)

        def on_stall_timeout_change(*_):
            try:
                v = float(self.stall_timeout_var.get().strip())
            except Exception:
                return
            self._apply_cfg_patch({"stall_timeout_s": v})

        self.stall_timeout_var.trace_add("write", on_stall_timeout_change)

        row += 1
        tk.Frame(inner, bg=self.colors["panel2"], height=1).grid(row=row, column=0, columnspan=5, sticky="ew", pady=(12, 12))
        row += 1

        tk.Label(inner, text="⬆ Точки бампа:", fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=14, anchor="w").grid(row=row, column=0, sticky="w", pady=4)

        self.bump_points_list = tk.Listbox(inner, height=5, width=38, bg=self.colors["panel2"], fg=self.colors["fg"],
                                           selectbackground=self.colors["accent"], activestyle="none")
        self.bump_points_list.grid(row=row, column=1, columnspan=2, rowspan=3, sticky="w", padx=(8, 0))

        btns = tk.Frame(inner, bg=self.colors["panel"])
        btns.grid(row=row, column=3, rowspan=3, sticky="n", padx=6)
        ttk.Button(btns, text="➕ Добавить", command=self.capture_bump_point).pack(fill="x", pady=2)
        ttk.Button(btns, text="➖ Убрать", command=self.remove_bump_point).pack(fill="x", pady=2)
        ttk.Button(btns, text="🗑 Очистить", command=self.clear_bump_points).pack(fill="x", pady=2)
        ttk.Button(btns, text="🧪 Тест бампа", command=self.test_bump_points).pack(fill="x", pady=2)

        # show/use toggle
        self.bump_use_points_var = tk.BooleanVar(value=bool(self.cfg.get("bump_use_points", False)))
        ttk.Checkbutton(inner, text="Использовать точки для бампа", variable=self.bump_use_points_var,
                        command=lambda: self._apply_cfg_patch({"bump_use_points": bool(self.bump_use_points_var.get())})
                        ).grid(row=row, column=4, sticky="w")

        self._refresh_bump_points_ui()

        # bump_points_list имеет rowspan=3 — сдвигаем row на 3 позиции вперёд
        row += 3

        # ── Items Monitor: координаты (хранятся плоско в cfg, не в cfg["coords"]) ────
        tk.Frame(inner, bg=self.colors["panel2"], height=1).grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=(12, 12))
        row += 1

        tk.Label(inner, text="🔍 Монитор предметов (Items)", fg=self.colors["accent"], bg=self.colors["panel"],
                 font=("Segoe UI", 10, "bold"), width=28, anchor="w").grid(
            row=row, column=0, columnspan=5, sticky="w", pady=(0, 4))
        row += 1

        # (ключ_cfg, подпись, дефолт_x, дефолт_y)
        ITEM_COORDS = [
            ("items_tab_x",         "items_tab_y",         "Меню→Предметы",         105,  268),
            ("search_x",            "search_y",            "Поиск",                 385,   32),
            ("first_card_x",        "first_card_y",        "Первая карточка",        345,  190),
            ("form_price_x",        "form_price_y",        "Форма: цена",            714,  311),
            ("form_submit_x",       "form_submit_y",       "Форма: Оплатить",        607,  664),
            ("delete_lot_btn_x",    "delete_lot_btn_y",    "Корзина (удалить лот)",  457,  503),
            ("confirm_delete_btn_x","confirm_delete_btn_y","Подтвердить удаление",   622,  478),
            ("add_lot_btn_x",       "add_lot_btn_y",       "Добавить лот",           341,  503),
            ("table_action_x",      None,                  "Таблица: Действие X",    1350, None),
            ("table_player_x",      None,                  "Таблица: Игрок X",       810,  None),
        ]

        self._item_coord_vars: Dict[str, tk.StringVar] = {}

        for kx, ky, label, def_x, def_y in ITEM_COORDS:
            cfg_now = self.cfg_provider()
            xv = tk.StringVar(value=str(cfg_now.get(kx, def_x)))
            self._item_coord_vars[kx] = xv

            tk.Label(inner, text=label + ":", fg=self.colors["fg"], bg=self.colors["panel"],
                     font=("Segoe UI", 10), width=24, anchor="w").grid(
                row=row, column=0, sticky="w", pady=3)

            ex = tk.Entry(inner, textvariable=xv, width=7,
                          bg=self.colors["panel2"], fg=self.colors["fg"],
                          insertbackground=self.colors["fg"], relief="flat")
            ex.grid(row=row, column=1, padx=(8, 6))

            if ky is not None:
                yv = tk.StringVar(value=str(cfg_now.get(ky, def_y)))
                self._item_coord_vars[ky] = yv
                ey = tk.Entry(inner, textvariable=yv, width=7,
                              bg=self.colors["panel2"], fg=self.colors["fg"],
                              insertbackground=self.colors["fg"], relief="flat")
                ey.grid(row=row, column=2, padx=(0, 12))
            else:
                yv = None

            def _make_save(kx_=kx, xv_=xv, ky_=ky, yv_=yv):
                def _save(*_):
                    patch = {}
                    try:
                        patch[kx_] = int(xv_.get())
                    except ValueError:
                        pass
                    if ky_ is not None and yv_ is not None:
                        try:
                            patch[ky_] = int(yv_.get())
                        except ValueError:
                            pass
                    if patch:
                        self._apply_cfg_patch(patch)
                return _save

            _save_fn = _make_save()
            xv.trace_add("write", _save_fn)
            if yv is not None:
                yv.trace_add("write", _save_fn)

            def _make_capture(kx_=kx, xv_=xv, ky_=ky, yv_=yv):
                def _do_capture():
                    def _apply(pos):
                        x_, y_ = pos
                        xv_.set(str(x_))
                        if ky_ is not None and yv_ is not None:
                            yv_.set(str(y_))
                        log(f"[Items Capture] {kx_}={x_}" + (f", {ky_}={y_}" if ky_ else ""))
                    self._run_capture_sequence(f"Capture {kx_}", _apply, show_ui_before=False)
                return _do_capture

            def _make_test(kx_=kx, xv_=xv, ky_=ky, yv_=yv):
                def _do_test():
                    try:
                        x_ = int(xv_.get())
                        y_ = int(yv_.get()) if (ky_ and yv_) else 0
                        init_pyautogui()
                        if ky_ and y_:
                            click_xy([x_, y_])
                            log(f"[Items Test] клик {kx_} → ({x_}, {y_})")
                        else:
                            log(f"[Items Test] {kx_} X={x_} (нет Y — клик не выполнен)")
                    except Exception as e:
                        log(f"[Items Test] ошибка: {e}")
                return _do_test

            ttk.Button(inner, text="📌 Захватить",
                       command=_make_capture()).grid(row=row, column=3, padx=6)
            ttk.Button(inner, text="🖱 Тест",
                       command=_make_test()).grid(row=row, column=4, padx=6)
            row += 1

        extra_sections = self._collect_settings_sections(
            exclude_roots={"telegram", "tg_tracker_cfg"},
            exclude_paths={
                ("coords",),
                ("vehicle_region",),
                ("plate_region",),
                ("plate_label_anchor_region",),
                ("stall_region",),
                ("stall_timeout_s",),
                ("bump_points",),
                ("bump_use_points",),
            },
            allowed_sections={
                "🔍 Быстроскан / Машины",
                "🔖 Номера / Реестр",
                "👁 Сторожевой",
            },
        )
        self._build_settings_panel(
            p,
            title="Детекция / регионы / watchdog (LIVE)",
            sections=extra_sections,
            padx=12,
            pady=(0, 12),
        )


    # ---------- Stats tab ----------
    def _build_stats_tab(self):
        p = self._panel(self.tab_stats)
        p.pack(fill="both", expand=True, padx=10, pady=10)

        canvas = tk.Canvas(p, bg=self.colors["panel"], highlightthickness=0)
        vbar = ttk.Scrollbar(p, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        inner = tk.Frame(canvas, bg=self.colors["panel"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(_e=None):
            try:
                canvas.itemconfigure(win, width=canvas.winfo_width())
            except Exception:
                pass

        def _sync_scroll(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        # ── STATS DASHBOARD HEADER ───────────────────────────────────────────────
        dash_hdr = tk.Frame(inner, bg=self.colors["panel"],
                            highlightthickness=1, highlightbackground=self.colors["border"])
        dash_hdr.pack(fill="x", padx=12, pady=(12, 4))
        tk.Frame(dash_hdr, bg=self.colors["accent2"], height=3).pack(fill="x", side="top")
        hdr_inner = tk.Frame(dash_hdr, bg=self.colors["panel"])
        hdr_inner.pack(fill="x", padx=14, pady=8)
        tk.Label(hdr_inner, text="📊 СТАТИСТИКА РАЗМЕЩЕНИЙ", bg=self.colors["panel"],
                 fg=self.colors["fg"], font=("Segoe UI", 11, "bold")).pack(side="left")
        self.run_state_var = tk.StringVar(value='○ ожидание')
        lbl_run = tk.Label(hdr_inner, textvariable=self.run_state_var,
                           fg=self.colors['accent'], bg=self.colors['panel'],
                           font=("Segoe UI", 9))
        lbl_run.pack(side='right')
        Tooltip(lbl_run, 'Текущее состояние бота.')

        def _tick_run_indicator():
            try:
                running = bool(getattr(self, 'loop_thread', None)) and bool(getattr(self, 'loop_thread').is_alive()) and self.run_event.is_set() and (not self.stop_event.is_set())
                paused = not self.run_event.is_set()
                if running and not paused:
                    cur = self.run_state_var.get()
                    self.run_state_var.set('● работает' if cur.startswith('○') else '○ работает')
                elif paused and (not self.stop_event.is_set()):
                    self.run_state_var.set('○ пауза')
                else:
                    self.run_state_var.set('○ ожидание')
            except Exception:
                pass
            if not getattr(self, "_closing", False):
                try:
                    self.after(450, _tick_run_indicator)
                except Exception:
                    pass

        try:
            self.after(450, _tick_run_indicator)
        except Exception:
            pass

        # ── METRIC CARDS ROW ─────────────────────────────────────────────────────
        stats_cards_row = tk.Frame(inner, bg=self.colors["bg"])
        stats_cards_row.pack(fill="x", padx=12, pady=(0, 4))

        self.total_posts_var = tk.StringVar(value="0")
        self.total_rev_var = tk.StringVar(value="0.00")
        self._stats_avg_var = tk.StringVar(value="—")
        self._stats_best_var = tk.StringVar(value="—")

        _sc = self.colors
        _stats_card_defs = [
            (self.total_posts_var, "Всего постов",   _sc["accent"]),
            (self.total_rev_var,   "Общий доход",    _sc["success"]),
            (self._stats_avg_var,  "Средняя цена",   _sc["accent2"]),
            (self._stats_best_var, "Лучшая машина",  _sc["warning"]),
        ]
        for var, title, accent_col in _stats_card_defs:
            card = tk.Frame(stats_cards_row, bg=_sc["panel"],
                            highlightthickness=1, highlightbackground=_sc["border"])
            card.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            tk.Frame(card, bg=accent_col, width=4).pack(fill="y", side="left")
            cbody = tk.Frame(card, bg=_sc["panel"])
            cbody.pack(fill="both", expand=True, padx=10, pady=8)
            tk.Label(cbody, text=title.upper(), bg=_sc["panel"],
                     fg=_sc["muted"], font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(cbody, textvariable=var, bg=_sc["panel"],
                     fg=_sc["fg"], font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(2, 0))

        # ── TIME PERIOD SELECTOR + SORT CONTROLS ──────────────────────────────
        ctrl = tk.Frame(inner, bg=self.colors["panel"],
                        highlightthickness=1, highlightbackground=self.colors["border"])
        ctrl.pack(fill="x", padx=12, pady=(0, 6))
        ctrl_inner = tk.Frame(ctrl, bg=self.colors["panel"])
        ctrl_inner.pack(fill="x", padx=12, pady=6)

        self.stats_window_var = tk.StringVar(value="7d")
        self.pop_metric_var = tk.StringVar(value="posts")  # posts|revenue
        self.stats_search_var = tk.StringVar(value="")
        self.sort_key_var = tk.StringVar(value="posts")  # vehicle|posts|revenue|avg_price|last_price|pop_delta|last_posted_at
        self.sort_desc_var = tk.BooleanVar(value=True)

        self._label(ctrl_inner, "Период:", muted=True).pack(side="left")
        self.cmb_window = ttk.Combobox(ctrl_inner, textvariable=self.stats_window_var, width=5, state="readonly",
                                       values=["всё время", "1h", "24h", "7d", "30d", "90d"])
        self.cmb_window.pack(side="left", padx=(4, 12))
        self.cmb_window.bind("<<ComboboxSelected>>", lambda e: self._refresh_stats(force=True))

        self._label(ctrl_inner, "Рост по:", muted=True).pack(side="left")
        self.cmb_pop = ttk.Combobox(ctrl_inner, textvariable=self.pop_metric_var, width=8, state="readonly",
                                    values=["posts", "revenue"])
        self.cmb_pop.pack(side="left", padx=(4, 12))
        Tooltip(self.cmb_pop, "Pop Δ\nposts = сравнение постов с прошлым окном\nrevenue = сравнение выручки с прошлым окном")
        self.cmb_pop.bind("<<ComboboxSelected>>", lambda e: self._refresh_stats(force=True))

        self._label(ctrl_inner, "Сортировка:", muted=True).pack(side="left")
        self.cmb_sort = ttk.Combobox(ctrl_inner, textvariable=self.sort_key_var, width=12, state="readonly",
                                     values=["score","posts_hr","rev_hr","posts","revenue","pop_delta","avg_price","last_price","vehicle","last_posted_at"])
        self.cmb_sort.pack(side="left", padx=(4, 6))
        self.cmb_sort.bind("<<ComboboxSelected>>", lambda e: self._refresh_stats(force=True))

        ttk.Checkbutton(ctrl_inner, text="По убыванию", variable=self.sort_desc_var, command=lambda: self._refresh_stats(force=True)).pack(side="left", padx=(0, 12))

        self._label(ctrl_inner, "Поиск:", muted=True).pack(side="left")
        ent = ttk.Entry(ctrl_inner, textvariable=self.stats_search_var, width=18)
        ent.pack(side="left", padx=(4, 10))
        ent.bind("<KeyRelease>", lambda e: self._refresh_stats(force=True))

        ttk.Button(ctrl_inner, text="📊 archive.csv", command=self.open_archive).pack(side="right", padx=4)
        ttk.Button(ctrl_inner, text="📈 Графики", command=lambda: open_charts_popup(self)).pack(side="right", padx=4)

        # --- table ---
        cols = ("vehicle", "posts", "revenue", "posts_hr", "rev_hr", "score", "avg_price", "last_price", "pop_delta", "last_posted_at")
        table = tk.Frame(inner, bg=self.colors["panel"])
        table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=16)
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        headings = [
    ("vehicle", "Машина"),
    ("posts", "Постов"),
    ("revenue", "Σ доход"),
    ("posts_hr", "Пост/ч"),
    ("rev_hr", "$/ч"),
    ("score", "Оценка"),
    ("avg_price", "Ср.цена"),
    ("last_price", "Посл."),
    ("pop_delta", "Рост Δ"),
    ("last_posted_at", "Последний пост"),
]
        for k, title in headings:
            # clickable headings to sort quickly
            if k in self.tree["columns"]:
                self.tree.heading(k, text=title, command=lambda c=k: self._on_stats_heading_click(c))
        self.tree.column("vehicle", width=220, anchor="w")
        self.tree.column("posts", width=70, anchor="center")
        self.tree.column("revenue", width=110, anchor="e")
        self.tree.column("posts_hr", width=90, anchor="e")
        self.tree.column("rev_hr", width=90, anchor="e")
        self.tree.column("score", width=80, anchor="e")
        self.tree.column("avg_price", width=90, anchor="e")
        self.tree.column("last_price", width=90, anchor="e")
        self.tree.column("pop_delta", width=90, anchor="center")
        self.tree.column("last_posted_at", width=170, anchor="center")
        self.tree.bind("<<TreeviewSelect>>", self._on_vehicle_select)
        # Grafana-style alternating row shading
        _alt_bg = self.colors.get("panel2", "#0f3460")
        self.tree.tag_configure("oddrow",  background=self.colors["panel"])
        self.tree.tag_configure("evenrow", background=_alt_bg)

        # --- details ---
        det = tk.Frame(inner, bg=self.colors["panel"])
        det.pack(fill="x", padx=12, pady=(0, 12))

        self.sel_title_var = tk.StringVar(value="Выберите машину для просмотра деталей…")
        tk.Label(det, textvariable=self.sel_title_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(anchor="w")

        # tabs inside stats details
        self.stats_nb = ttk.Notebook(det)
        self.stats_nb.pack(fill="both", expand=False, pady=(8, 0))

        self.tab_details = tk.Frame(self.stats_nb, bg=self.colors["panel"])
        self.tab_editor = tk.Frame(self.stats_nb, bg=self.colors["panel"])
        self.tab_ai = tk.Frame(self.stats_nb, bg=self.colors["panel"])

        self.stats_nb.add(self.tab_details, text="Детали")
        self.stats_nb.add(self.tab_editor, text="Редактор")
        self.stats_nb.add(self.tab_ai, text="AI цены (A)")
        self.sel_stats_var = tk.StringVar(value="")
        tk.Label(det, textvariable=self.sel_stats_var, fg=self.colors["muted"], bg=self.colors["panel"]).pack(anchor="w", pady=(2, 6))

        self.sel_recent = scrolledtext.ScrolledText(
            self.tab_details,
            height=6,
            bg=self.colors["panel2"],
            fg=self.colors["fg"],
            insertbackground=self.colors["fg"],
            relief="flat",
        )
        self.sel_recent.pack(fill="x")
        self.sel_recent.insert("end", "Recent posts will appear here.\n")
        self.sel_recent.config(state="disabled")

        # build editor + AI panels
        self.tab_editor_inner = self._make_scrollable_tab(self.tab_editor, bg=self.colors["panel"])
        self._build_editor_ui()
        self._build_pricing_ai_ui()
        # --- inline charts (same page as stats) ---
        charts = tk.Frame(inner, bg=self.colors["panel"])
        charts.pack(fill="both", expand=False, padx=12, pady=(0, 12))

        tk.Label(charts, text="📈 Инфографика (текущее окно)", fg=self.colors["muted"], bg=self.colors["panel"]).pack(anchor="w", pady=(0, 6))
        self._inline_canvas = tk.Canvas(charts, bg=self.colors["panel2"], highlightthickness=0, height=240)
        self._inline_canvas.pack(fill="both", expand=True)
        self._inline_canvas.bind("<Configure>", lambda _e=None: self._redraw_inline_charts())
        Tooltip(self._inline_canvas, "Инфографика\nОбновляется при смене Window/Sort/Weight/Search.\nВыбери машину — дальше увидишь, что происходит с ценой.")

        ui_sections = self._collect_settings_sections(
            exclude_roots={"telegram", "tg_tracker_cfg"},
            allowed_sections={"🖥 Интерфейс", "🔐 Админ"},
        )
        self._build_settings_panel(
            inner,
            title="UI / Admin настройки (LIVE)",
            sections=ui_sections,
            padx=12,
            pady=(0, 12),
        )
    # ---------- Logs tab ----------

    # ---------- Settings helpers ----------
    def _flatten_settings_items(self, data: Any, prefix: Tuple[Any, ...]) -> List[Tuple[Tuple[Any, ...], Any]]:
        items: List[Tuple[Tuple[Any, ...], Any]] = []
        if isinstance(data, dict):
            for key in sorted(data.keys(), key=lambda v: str(v).lower()):
                items.extend(self._flatten_settings_items(data[key], prefix + (key,)))
            return items
        if isinstance(data, list):
            return [(prefix, list(data))]
        return [(prefix, data)]

    def _get_default_for_path(self, path: Tuple[Any, ...]) -> Any:
        cur: Any = DEFAULT_CONFIG
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return None
        return cur

    def _humanize_key(self, key: Any) -> str:
        text = str(key).replace("_", " ").strip()
        text = text.replace("tg ", "TG ").replace("ui ", "UI ").replace("api ", "API ")
        return text[:1].upper() + text[1:] if text else str(key)

    def _settings_tooltip(self, path: Tuple[Any, ...], value: Any, default: Any) -> str:
        key_path = ".".join(str(p) for p in path)
        return (
            f"Ключ: {key_path}\n"
            f"Тип: {type(value).__name__}\n"
            f"Текущее: {value}\n"
            f"По умолчанию: {default}\n"
            "LIVE: изменения применяются сразу."
        )

    def _numeric_range(self, path: Tuple[Any, ...], value: float) -> Tuple[float, float, float]:
        key = str(path[-1]).lower()
        base = abs(float(value)) if value is not None else 0.0
        if any(token in key for token in ("confidence", "threshold", "ratio", "min", "max")) and base <= 2.0:
            return 0.0, 1.0, 0.01
        if any(token in key for token in ("delay", "timeout", "sleep", "interval", "cooldown")):
            upper = max(5.0, base * 4.0)
            return 0.0, upper, 0.01
        if any(token in key for token in ("retries", "tries", "workers", "rows", "cols", "pages", "count", "tab_count")):
            upper = max(10.0, base * 4.0)
            return 0.0, upper, 1.0
        if any(token in key for token in ("x", "y", "w", "h", "dx", "dy")):
            upper = max(1000.0, base * 2.0)
            return 0.0, upper, 1.0
        upper = max(5.0, base * 3.0)
        return 0.0, upper, 0.01

    def _add_setting_row(self, parent: tk.Widget, path: Tuple[Any, ...], value: Any) -> None:
        default = self._get_default_for_path(path)
        label = self._humanize_key(path[-1])
        tooltip = self._settings_tooltip(path, value, default)
        row = tk.Frame(parent, bg=self.colors["panel"])
        row.pack(fill="x", padx=10, pady=6)

        name = tk.Label(row, text=label, fg=self.colors["fg"], bg=self.colors["panel"], anchor="w", width=28)
        name.pack(side="left", padx=(6, 10))
        Tooltip(name, tooltip)

        box = tk.Frame(row, bg=self.colors["panel"])
        box.pack(side="left", fill="x", expand=True)

        if value is None:
            var = tk.StringVar(value="")
            ent = ttk.Entry(box, textvariable=var)
            ent.pack(side="left", fill="x", expand=True)
            Tooltip(ent, tooltip)

            def _commit_none(*_):
                raw = var.get().strip()
                if not raw:
                    self._update_cfg_path(path, None)
                    return
                try:
                    parsed = float(raw)
                    if parsed.is_integer():
                        parsed = int(parsed)
                    self._update_cfg_path(path, parsed)
                except Exception:
                    self._update_cfg_path(path, raw)

            ent.bind("<Return>", _commit_none, add="+")
            ent.bind("<FocusOut>", _commit_none, add="+")
            return

        if isinstance(value, bool):
            var = tk.BooleanVar(value=bool(value))
            cb = ttk.Checkbutton(
                box,
                variable=var,
                command=lambda: self._update_cfg_path(path, bool(var.get())),
            )
            cb.pack(side="left")
            Tooltip(cb, tooltip)
            return

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value is None:
                var = tk.StringVar(value="")
                ent = ttk.Entry(box, textvariable=var, width=12)
                ent.pack(side="left")
                Tooltip(ent, tooltip)

                def _apply_noneable(*_):
                    raw = var.get().strip()
                    if not raw:
                        self._update_cfg_path(path, None)
                        return
                    try:
                        parsed = float(raw)
                    except Exception:
                        return
                    if isinstance(value, int):
                        parsed = int(parsed)
                    self._update_cfg_path(path, parsed)

                ent.bind("<Return>", _apply_noneable, add="+")
                ent.bind("<FocusOut>", _apply_noneable, add="+")
                return

            var = tk.DoubleVar(value=float(value))
            frm, to, step = self._numeric_range(path, float(value))

            def _on_scale(v):
                try:
                    num = float(v)
                except Exception:
                    return
                if isinstance(value, int):
                    num = int(round(num))
                    var.set(num)
                self._update_cfg_path(path, num)

            scale = ttk.Scale(box, from_=frm, to=to, orient="horizontal", command=_on_scale)
            scale.set(float(value))
            scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
            Tooltip(scale, tooltip)

            entry_var = tk.StringVar(value=str(value))
            ent = ttk.Entry(box, textvariable=entry_var, width=10)
            ent.pack(side="left")
            Tooltip(ent, tooltip)

            def _commit_entry(*_):
                raw = entry_var.get().strip()
                if not raw:
                    return
                try:
                    parsed = float(raw)
                except Exception:
                    return
                if isinstance(value, int):
                    parsed = int(round(parsed))
                scale.set(parsed)
                self._update_cfg_path(path, parsed)

            ent.bind("<Return>", _commit_entry, add="+")
            ent.bind("<FocusOut>", _commit_entry, add="+")
            return

        if isinstance(value, list):
            var = tk.StringVar(value=", ".join(str(v) for v in value))
            ent = ttk.Entry(box, textvariable=var)
            ent.pack(side="left", fill="x", expand=True)
            Tooltip(ent, tooltip)

            def _commit_list(*_):
                raw = var.get().strip()
                if not raw:
                    self._update_cfg_path(path, [])
                    return
                parts = [p for p in re.split(r"[,\s;]+", raw) if p]
                parsed: List[Any] = []
                for part in parts:
                    try:
                        num = float(part)
                        if num.is_integer():
                            parsed.append(int(num))
                        else:
                            parsed.append(num)
                    except Exception:
                        parsed.append(part)
                self._update_cfg_path(path, parsed)

            ent.bind("<Return>", _commit_list, add="+")
            ent.bind("<FocusOut>", _commit_list, add="+")
            return

        var = tk.StringVar(value="" if value is None else str(value))
        ent = ttk.Entry(box, textvariable=var)
        ent.pack(side="left", fill="x", expand=True)
        Tooltip(ent, tooltip)

        def _commit_text(*_):
            self._update_cfg_path(path, var.get())

        ent.bind("<Return>", _commit_text, add="+")
        ent.bind("<FocusOut>", _commit_text, add="+")

    def _update_cfg_path(self, path: Tuple[Any, ...], value: Any, extra_changed: Optional[Set[str]] = None) -> None:
        if not path:
            return
        cfg = self.cfg_provider()
        root_key = path[0]
        if len(path) == 1:
            cfg[root_key] = value
            changed = {str(root_key)}
            if extra_changed:
                changed |= set(extra_changed)
            self._apply_cfg_patch_with_keys({root_key: cfg[root_key]}, changed)
            return
        root = cfg.get(root_key, {})
        if not isinstance(root, dict):
            root = {}
        cursor = root
        for key in path[1:-1]:
            if not isinstance(cursor.get(key), dict):
                cursor[key] = {}
            cursor = cursor[key]
        cursor[path[-1]] = value
        changed = {str(root_key), str(path[-1])}
        if extra_changed:
            changed |= set(extra_changed)
        self._apply_cfg_patch_with_keys({root_key: root}, changed)

    def _classify_setting(self, path: Tuple[Any, ...]) -> str:
        root = str(path[0])
        key = str(path[-1]).lower()
        if root.startswith("log_"):
            return "📋 Логирование"
        if root in {"telegram", "tg_tracker_cfg"} or root.startswith("tg_"):
            return "📡 Telegram"
        if root == "limits":
            return "⚖ Лимиты"
        if root.startswith("fast_scan") or root.startswith("vehicle_"):
            return "🔍 Быстроскан / Машины"
        if root.startswith("plate_") or root.startswith("plate_registry"):
            return "🔖 Номера / Реестр"
        if root.startswith("file_dialog"):
            return "📂 Диалог файлов"
        if root.startswith("bump_"):
            return "⬆ Бамп"
        if root.startswith("watchdog") or root.startswith("stall_"):
            return "👁 Сторожевой"
        if root.startswith("type_") or root.startswith("field_") or root.startswith("paste_") or root.startswith("layout_switch") or root in {
            "ignore_keyboard_layout",
            "use_clipboard_paste",
            "force_layout_typed_input",
        }:
            return "⌨ Ввод / Раскладка"
        if any(token in key for token in ("delay", "timeout", "interval", "sleep", "cooldown")):
            return "⏱ Тайминги"
        if root in {"loop_mode", "dedupe_policy", "dedupe_force_in_loop", "refresh_on_empty_sweep"}:
            return "📋 Цикл / Поведение"
        if root in {"metrics_gui", "ui_prefs"}:
            return "🖥 Интерфейс"
        if root == "admin_gate":
            return "🔐 Админ"
        return "⚙ Прочее"

    def _collect_settings_sections(
        self,
        *,
        exclude_roots: Optional[Set[str]] = None,
        exclude_paths: Optional[Set[Tuple[Any, ...]]] = None,
        allowed_sections: Optional[Set[str]] = None,
    ) -> Dict[str, List[Tuple[Tuple[Any, ...], Any]]]:
        cfg = self.cfg_provider()
        items = self._flatten_settings_items(cfg, prefix=())
        sections: Dict[str, List[Tuple[Tuple[Any, ...], Any]]] = {}
        for path, value in items:
            if exclude_roots and str(path[0]) in exclude_roots:
                continue
            if exclude_paths and path in exclude_paths:
                continue
            section = self._classify_setting(path)
            if allowed_sections and section not in allowed_sections:
                continue
            sections.setdefault(section, []).append((path, value))
        return sections

    def _build_settings_tabs(self, parent: tk.Widget, sections: Dict[str, List[Tuple[Tuple[Any, ...], Any]]]) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        for section in sorted(sections.keys(), key=lambda v: v.lower()):
            tab = tk.Frame(nb, bg=self.colors["panel"])
            nb.add(tab, text=section)
            inner = self._make_scrollable_tab(tab, bg=self.colors["panel"])
            for path, value in sections[section]:
                self._add_setting_row(inner, path, value)

    def _build_settings_panel(
        self,
        parent: tk.Widget,
        *,
        title: str,
        sections: Dict[str, List[Tuple[Tuple[Any, ...], Any]]],
        padx: int = 12,
        pady: Tuple[int, int] = (8, 12),
    ) -> None:
        if not sections:
            return
        panel = ttk.LabelFrame(parent, text=title)
        panel.pack(fill="both", expand=True, padx=padx, pady=pady)
        self._build_settings_tabs(panel, sections)

    # ---------- Config (ALL) tab ----------
    def _build_config_tab(self):
        """Вкладка Config (ALL) — все настройки конфига в виде вкладок по секциям."""
        p = self._panel(self.tab_config)
        p.pack(fill="both", expand=True, padx=10, pady=10)

        # Header
        hdr = tk.Frame(p, bg=self.colors["panel2"])
        hdr.pack(fill="x", padx=0, pady=(0, 8))
        tk.Label(hdr, text="⚙ КОНФИГУРАЦИЯ (JSON)",
                 fg=self.colors["fg"], bg=self.colors["panel2"],
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=12, pady=8)

        # Warning banner
        warn = tk.Frame(p, bg=self.colors.get("warning_bg", "#2d2005"),
                        highlightthickness=1, highlightbackground=self.colors.get("warning", "#e6b800"))
        warn.pack(fill="x", padx=0, pady=(0, 10))
        tk.Label(warn,
                 text="⚠  Изменяйте только если знаете что делаете. "
                      "Ошибочные значения могут нарушить работу бота.",
                 fg=self.colors.get("warning", "#e6b800"),
                 bg=self.colors.get("warning_bg", "#2d2005"),
                 font=("Segoe UI", 9)).pack(side="left", padx=12, pady=6)

        try:
            # Собираем все секции настроек (кроме telegram и tg_tracker)
            sections = self._collect_settings_sections(
                exclude_roots={"telegram", "tg_tracker_cfg"},
            )
            if sections:
                self._build_settings_tabs(p, sections)
            else:
                tk.Label(p, text="Нет настроек для отображения",
                         fg=self.colors["muted"], bg=self.colors["panel"]).pack(pady=20)
        except Exception as _ex:
            log(f"[Config tab] ошибка построения: {_ex}")
            tk.Label(p, text=f"Ошибка: {_ex}",
                     fg=self.colors["danger"], bg=self.colors["panel"]).pack(pady=20)

    # ---------- Telegram tab ----------
    def _build_tg_tab(self):
        outer = self._panel(self.tab_tg)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # Make this tab scrollable (same pattern as Tuning tab)
        canvas = tk.Canvas(outer, bg=self.colors["panel"], highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        inner = tk.Frame(canvas, bg=self.colors["panel"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(_e=None):
            try:
                canvas.itemconfigure(win, width=canvas.winfo_width())
            except Exception:
                pass

        def _sync_scroll(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        p = inner

        # ── CONNECTION STATUS CARD ────────────────────────────────────────────
        conn_card = tk.Frame(p, bg=self.colors["panel"],
                             highlightthickness=1, highlightbackground=self.colors["border"])
        conn_card.pack(fill="x", pady=(8, 6))
        tk.Frame(conn_card, bg=self.colors["accent"], height=3).pack(fill="x", side="top")
        conn_inner = tk.Frame(conn_card, bg=self.colors["panel"])
        conn_inner.pack(fill="x", padx=14, pady=10)

        # Title + live status indicator
        tg_title_row = tk.Frame(conn_inner, bg=self.colors["panel"])
        tg_title_row.pack(fill="x")
        tk.Label(tg_title_row, text="📡 Telegram — Трекер аренды",
                 fg=self.colors["fg"], bg=self.colors["panel"],
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self.lbl_tg_status = tk.Label(tg_title_row, text="Статус: —",
                                       fg=self.colors["muted"], bg=self.colors["panel"],
                                       font=("Segoe UI", 9))
        self.lbl_tg_status.pack(side="right")

        self._label(
            conn_inner,
            f"Data dir: {str(USER_DIR)}",
            muted=True,
        ).pack(anchor="w", pady=(4, 0))
        self._label(
            conn_inner,
            "Читает уведомления об аренде от Majestic бота. Пишет rentals.csv + rentals_summary.json.",
            muted=True,
        ).pack(anchor="w")

        # ── INCOME METRIC CARDS ─────────────────────────────────────────────
        tg_cards_row = tk.Frame(p, bg=self.colors["bg"])
        tg_cards_row.pack(fill="x", pady=(0, 6))

        _tc = self.colors
        _tg_card_defs = [
            ("tg_card_today",  "Сегодня",     "—",  _tc["accent"]),
            ("tg_card_week",   "Эта неделя",  "—",  _tc["accent2"]),
            ("tg_card_active", "Сейчас",      "—",  _tc["success"]),
            ("tg_card_rph",    "Avg $/час",   "—",  _tc["warning"]),
        ]
        self._tg_card_vars = {}
        for attr, title, init, accent_col in _tg_card_defs:
            card = tk.Frame(tg_cards_row, bg=_tc["panel"],
                            highlightthickness=1, highlightbackground=_tc["border"])
            card.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            tk.Frame(card, bg=accent_col, width=4).pack(fill="y", side="left")
            cbody = tk.Frame(card, bg=_tc["panel"])
            cbody.pack(fill="both", expand=True, padx=10, pady=8)
            tk.Label(cbody, text=title.upper(), bg=_tc["panel"],
                     fg=_tc["muted"], font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value=init)
            self._tg_card_vars[attr] = var
            tk.Label(cbody, textvariable=var, bg=_tc["panel"],
                     fg=_tc["fg"], font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(2, 0))

        # ── QUICK CONTROL BUTTONS ─────────────────────────────────────────────
        btns = tk.Frame(p, bg=self.colors["panel"],
                        highlightthickness=1, highlightbackground=self.colors["border"])
        btns.pack(fill="x", pady=(0, 8))
        btns_inner = tk.Frame(btns, bg=self.colors["panel"])
        btns_inner.pack(fill="x", padx=12, pady=8)

        tg = self.cfg.get("telegram", {})
        if not isinstance(tg, dict):
            tg = {}

        # Name mapping: Majestic car name -> your local key (folder/stat item)
        self.tg_name_map = dict(tg.get("name_map", {})) if isinstance(tg.get("name_map", {}), dict) else {}
        self._tg_map_dialog_open = False
        self._tg_last_view_refresh_ts = 0.0

        # Vars
        self.tg_enabled_var = tk.BooleanVar(value=bool(tg.get("enabled", False)))
        self.tg_api_id_var = tk.StringVar(value=str(tg.get("api_id", "")) if tg.get("api_id") else "")
        self.tg_api_hash_var = tk.StringVar(value=str(tg.get("api_hash", "")) if tg.get("api_hash") else "")
        self.tg_chat_contains_var = tk.StringVar(value=str(tg.get("chat_title_contains", "Majestic")))
        self.tg_session_name_var = tk.StringVar(value=str(tg.get("session_name", "majestic_session")))
        self.tg_out_csv_var = tk.StringVar(value=str(tg.get("output_csv", "rentals.csv")))
        self.tg_out_json_var = tk.StringVar(value=str(tg.get("output_json", "rentals_summary.json")))
        self.tg_show_hash_var = tk.BooleanVar(value=False)

        form = tk.Frame(p, bg=self.colors["panel"])
        form.pack(fill="x", pady=(0, 10))

        def _row(r, label, widget):
            tk.Label(form, text=label, fg=self.colors["muted"], bg=self.colors["panel"]).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=4)
            widget.grid(row=r, column=1, sticky="we", pady=4)

        form.grid_columnconfigure(1, weight=1)

        ttk.Checkbutton(form, text="📡 Включить трекер", variable=self.tg_enabled_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ent_id = ttk.Entry(form, textvariable=self.tg_api_id_var)
        _row(1, "api_id:", ent_id)

        ent_hash = ttk.Entry(form, textvariable=self.tg_api_hash_var, show="*")
        _row(2, "api_hash:", ent_hash)

        def _toggle_hash():
            try:
                show = self.tg_show_hash_var.get()
                ent_hash.configure(show="" if show else "*")
            except Exception:
                pass

        ttk.Checkbutton(form, text="Показать api_hash", variable=self.tg_show_hash_var, command=_toggle_hash).grid(row=2, column=2, sticky="w", padx=(10, 0))

        ent_chat = ttk.Entry(form, textvariable=self.tg_chat_contains_var)
        _row(3, "Название чата содержит:", ent_chat)

        ent_sess = ttk.Entry(form, textvariable=self.tg_session_name_var)
        _row(4, "Имя сессии:", ent_sess)

        ent_csv = ttk.Entry(form, textvariable=self.tg_out_csv_var)
        _row(5, "Выходной CSV:", ent_csv)

        ent_json = ttk.Entry(form, textvariable=self.tg_out_json_var)
        _row(6, "Выходной JSON:", ent_json)

        tracker_box = ttk.LabelFrame(p, text="⚙ Настройки трекера (tg_tracker_cfg)")
        tracker_box.pack(fill="x", pady=(0, 12))
        tracker_inner = tk.Frame(tracker_box, bg=self.colors["panel"])
        tracker_inner.pack(fill="x", padx=8, pady=6)
        tg_tracker_cfg = self.cfg.get("tg_tracker_cfg", {})
        if isinstance(tg_tracker_cfg, dict):
            for path, value in self._flatten_settings_items(tg_tracker_cfg, prefix=("tg_tracker_cfg",)):
                self._add_setting_row(tracker_inner, path, value)

        def _collect_cfg():
            base = dict(self.cfg.get("telegram", {})) if isinstance(self.cfg.get("telegram", {}), dict) else {}
            base["enabled"] = bool(self.tg_enabled_var.get())
            try:
                base["api_id"] = int(str(self.tg_api_id_var.get()).strip() or "0")
            except Exception:
                base["api_id"] = 0
            base["api_hash"] = str(self.tg_api_hash_var.get()).strip()
            base["chat_title_contains"] = str(self.tg_chat_contains_var.get()).strip()
            base["session_name"] = str(self.tg_session_name_var.get()).strip() or "majestic_session"
            base["output_csv"] = str(self.tg_out_csv_var.get()).strip() or "rentals.csv"
            base["output_json"] = str(self.tg_out_json_var.get()).strip() or "rentals_summary.json"
            base["name_map"] = dict(self.tg_name_map)
            return base

        def _save_only():
            new_tg = _collect_cfg()
            self._apply_cfg_patch({"telegram": new_tg})
            self.tg_tracker.update_name_map(new_tg.get("name_map", {}))
            self._tg_rebuild_summary_from_csv()
            self._tg_refresh_views()
            self._refresh_tg_status(schedule=False)

        def _start():
            new_tg = _collect_cfg()
            self._apply_cfg_patch({"telegram": new_tg})
            self.tg_tracker.update_name_map(new_tg.get("name_map", {}))
            self.tg_tracker.start(new_tg)
            self._refresh_tg_status(schedule=False)

        def _stop():
            self.tg_tracker.stop()
            self._refresh_tg_status(schedule=False)

        ttk.Button(btns_inner, text="💾 Сохранить", style="Accent.TButton", command=_save_only).pack(side="right", padx=4)
        ttk.Button(btns_inner, text="⏹ Стоп", style="Danger.TButton", command=_stop).pack(side="right", padx=4)
        ttk.Button(btns_inner, text="▶ Старт", style="Success.TButton", command=_start).pack(side="right", padx=4)
        ttk.Button(btns_inner, text="📂 Открыть папку", command=lambda: self.open_folder(str(USER_DIR))).pack(side="right", padx=4)
        ttk.Button(btns_inner, text="🔄 Обновить", command=self._tg_refresh_views).pack(side="left", padx=4)

        # Mapping UI
        map_box = tk.Frame(p, bg=self.colors["panel"])
        map_box.pack(fill="x", pady=(0, 12))

        self._label(map_box, "🔄 Маппинг имён (Majestic → локальный ключ/папка)", bold=True).pack(anchor="w")
        self._label(map_box, "При новом неизвестном имени машины появится окно для привязки.", muted=True).pack(anchor="w", pady=(0, 6))

        cols = ("majestic", "local")
        self.tg_map_tree = ttk.Treeview(map_box, columns=cols, show="headings", height=7)
        self.tg_map_tree.heading("majestic", text="Имя в Majestic")
        self.tg_map_tree.heading("local", text="Локальный ключ")
        self.tg_map_tree.column("majestic", width=420, stretch=True)
        self.tg_map_tree.column("local", width=240, stretch=False)
        self.tg_map_tree.pack(fill="x", pady=(0, 6))

        map_btns = tk.Frame(map_box, bg=self.colors["panel"])
        map_btns.pack(fill="x")

        ttk.Button(map_btns, text="➕ Добавить", command=lambda: self._tg_open_map_dialog("" )).pack(side="left", padx=4)
        ttk.Button(map_btns, text="✏ Изменить", command=self._tg_edit_selected_mapping).pack(side="left", padx=4)
        ttk.Button(map_btns, text="➖ Удалить", command=self._tg_remove_selected_mapping).pack(side="left", padx=4)
        ttk.Button(map_btns, text="🔄 Пересчитать из CSV", command=lambda: (self._tg_rebuild_summary_from_csv(), self._tg_refresh_views())).pack(side="left", padx=4)

        # Stats UI
        stats_box = tk.Frame(p, bg=self.colors["panel"])
        stats_box.pack(fill="x", pady=(0, 12))

        self._label(stats_box, "📊 Статистика TG (из rentals_summary.json)", bold=True).pack(anchor="w")
        self.lbl_tg_totals = self._label(stats_box, "Итого: -", muted=True)
        self.lbl_tg_totals.pack(anchor="w", pady=(0, 6))

        self.tg_stats_tree = ttk.Treeview(stats_box, columns=("key","revenue","hours","events","rph"), show="headings", height=9)
        for c, t, w in [
            ("key","Ключ машины",260),
            ("revenue","Доход ($)",130),
            ("hours","Часы",80),
            ("events","Событий",80),
            ("rph","$/час",90),
        ]:
            self.tg_stats_tree.heading(c, text=t)
            self.tg_stats_tree.column(c, width=w, stretch=(c=="key"))
        self.tg_stats_tree.pack(fill="x", pady=(0, 8))

        self._label(stats_box, "🏆 Топ арендаторов", bold=False).pack(anchor="w")
        self.tg_renter_tree = ttk.Treeview(stats_box, columns=("renter","revenue","hours","events"), show="headings", height=6)
        for c, t, w in [
            ("renter","Арендатор",260),
            ("revenue","Доход ($)",130),
            ("hours","Часы",80),
            ("events","Событий",80),
        ]:
            self.tg_renter_tree.heading(c, text=t)
            self.tg_renter_tree.column(c, width=w, stretch=(c=="renter"))
        self.tg_renter_tree.pack(fill="x", pady=(0, 6))

        # Recent events
        recent_box = tk.Frame(p, bg=self.colors["panel"])
        recent_box.pack(fill="x", pady=(0, 6))
        self._label(recent_box, "⏱ Последние события TG (из rentals.csv)", bold=True).pack(anchor="w")
        self.tg_recent = tk.Listbox(recent_box, height=9)
        self.tg_recent.pack(fill="x", pady=(0, 4))

        self._tg_refresh_mapping_tree()
        self._tg_refresh_views()
        self._refresh_tg_status(schedule=True)

    def _tg_norm_name(self, s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^\[[^\]]+\]\s*", "", s)
        s = re.sub(r"\s*\[[^\]]+\]\s*$", "", s)
        s = re.sub(r"\s*\([^)]+\)\s*$", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.casefold()

    def _tg_list_local_keys(self) -> List[str]:
        keys: Set[str] = set()
        try:
            car_dir = str(self.cfg.get("car_dir", r"C:\\sale\\car"))
            p = Path(car_dir)
            if p.exists():
                for ch in p.iterdir():
                    if ch.is_dir():
                        keys.add(ch.name)
        except Exception:
            pass
        try:
            st = Stats.load()
            for k in (st.get("items", {}) or {}).keys():
                keys.add(str(k))
        except Exception:
            pass
        return sorted(keys, key=lambda x: x.lower())

    def _tg_refresh_mapping_tree(self) -> None:
        if not hasattr(self, "tg_map_tree"):
            return
        try:
            selected_vals = set()
            try:
                for sel in self.tg_map_tree.selection():
                    vals = self.tg_map_tree.item(sel, "values") or ()
                    if vals:
                        selected_vals.add(str(vals[0] or ""))
            except Exception:
                selected_vals = set()

            for iid in self.tg_map_tree.get_children():
                self.tg_map_tree.delete(iid)
            for k in sorted(self.tg_name_map.keys(), key=lambda x: x.lower()):
                iid = self.tg_map_tree.insert("", "end", values=(k, self.tg_name_map.get(k, "")))
                if k in selected_vals:
                    try:
                        self.tg_map_tree.selection_add(iid)
                    except Exception:
                        pass
        except Exception:
            pass

    def _tg_edit_selected_mapping(self) -> None:
        try:
            sel = self.tg_map_tree.selection()
            if not sel:
                return
            vals = self.tg_map_tree.item(sel[0], "values") or ()
            if not vals:
                return
            majestic = str(vals[0] or "")
            local = str(vals[1] or "")
            self._tg_open_map_dialog(majestic, preset_local=local)
        except Exception:
            pass

    def _tg_remove_selected_mapping(self) -> None:
        try:
            sel = self.tg_map_tree.selection()
            if not sel:
                return
            vals = self.tg_map_tree.item(sel[0], "values") or ()
            if not vals:
                return
            majestic = str(vals[0] or "")
            if not majestic:
                return
            if not messagebox.askyesno("Remove mapping", f"Remove mapping for\n\n{majestic}\n\n?"):
                return
            self.tg_name_map.pop(majestic, None)
            self._tg_save_name_map()
        except Exception:
            pass

    def _tg_save_name_map(self) -> None:
        cfg = self.cfg_provider()
        tg = dict(cfg.get("telegram", {})) if isinstance(cfg.get("telegram", {}), dict) else {}
        tg["name_map"] = dict(self.tg_name_map)
        self._apply_cfg_patch({"telegram": tg})
        try:
            self.tg_tracker.update_name_map(tg.get("name_map", {}))
        except Exception:
            pass
        self._tg_rebuild_summary_from_csv()
        self._tg_refresh_mapping_tree()
        self._tg_refresh_views()

    def _tg_open_map_dialog(self, majestic_name: str, preset_local: str = "") -> None:
        if self._tg_map_dialog_open:
            return
        self._tg_map_dialog_open = True

        majestic_name = (majestic_name or "").strip()

        top = tk.Toplevel(self)
        top.title("Привязка имени Majestic")
        top.configure(bg=self.colors["panel"])
        top.geometry("620x330")
        top.transient(self)

        tk.Label(top, text="Majestic name:", fg=self.colors["muted"], bg=self.colors["panel"]).pack(anchor="w", padx=12, pady=(12, 2))
        var_name = tk.StringVar(value=majestic_name)
        ent_name = ttk.Entry(top, textvariable=var_name)
        ent_name.pack(fill="x", padx=12)

        tk.Label(top, text="Local key (folder/stats):", fg=self.colors["muted"], bg=self.colors["panel"]).pack(anchor="w", padx=12, pady=(12, 2))

        all_keys = self._tg_list_local_keys()

        var_filter = tk.StringVar(value="")
        ent_filter = ttk.Entry(top, textvariable=var_filter)
        ent_filter.pack(fill="x", padx=12)

        var_local = tk.StringVar(value=preset_local or (all_keys[0] if all_keys else ""))
        cmb = ttk.Combobox(top, textvariable=var_local, values=all_keys, state="normal")
        cmb.pack(fill="x", padx=12, pady=(6, 0))

        # --- Clipboard shortcuts (layout-independent) + context menu ---
        # Tk on Windows can treat Ctrl+V/C differently under non-latin keyboard layouts.
        # We bind by virtual keycodes (A=65, C=67, V=86, X=88) so it works in RU layout too.
        def _w_get(w):
            try:
                return w.get()
            except Exception:
                return ""

        def _copy(_e=None, w=None):
            try:
                try:
                    txt = w.selection_get()
                except Exception:
                    txt = _w_get(w)
                if txt:
                    w.clipboard_clear()
                    w.clipboard_append(txt)
            except Exception:
                pass
            return "break"

        def _paste(_e=None, w=None):
            try:
                txt = w.clipboard_get()
            except Exception:
                return "break"
            try:
                w.delete("sel.first", "sel.last")
            except Exception:
                pass
            try:
                w.insert("insert", txt)
            except Exception:
                try:
                    w.set(txt)  # ttk.Combobox fallback
                except Exception:
                    pass
            return "break"

        def _cut(_e=None, w=None):
            _copy(_e, w)
            try:
                w.delete("sel.first", "sel.last")
            except Exception:
                pass
            return "break"

        def _select_all(_e=None, w=None):
            try:
                w.selection_range(0, "end")
                w.icursor("end")
            except Exception:
                pass
            return "break"

        def _bind_clip(w):
            def _on_key(e):
                try:
                    if (e.state & 0x4) == 0:  # Control not pressed
                        return
                except Exception:
                    return
                kc = getattr(e, "keycode", None)
                if kc == 65:   # Ctrl+A
                    return _select_all(e, w)
                if kc == 67:   # Ctrl+C
                    return _copy(e, w)
                if kc == 86:   # Ctrl+V
                    return _paste(e, w)
                if kc == 88:   # Ctrl+X
                    return _cut(e, w)
                return

            w.bind("<KeyPress>", _on_key, add=True)
            w.bind("<Shift-Insert>", lambda e: _paste(e, w), add=True)
            w.bind("<Control-Insert>", lambda e: _copy(e, w), add=True)

            menu = tk.Menu(top, tearoff=0)
            menu.add_command(label="Cut", command=lambda: _cut(None, w))
            menu.add_command(label="Copy", command=lambda: _copy(None, w))
            menu.add_command(label="Paste", command=lambda: _paste(None, w))
            menu.add_separator()
            menu.add_command(label="Select all", command=lambda: _select_all(None, w))

            def _popup(e):
                try:
                    menu.tk_popup(e.x_root, e.y_root)
                finally:
                    try:
                        menu.grab_release()
                    except Exception:
                        pass

            # Right click paste menu
            w.bind("<Button-3>", _popup, add=True)

        _bind_clip(ent_name)
        _bind_clip(ent_filter)
        _bind_clip(cmb)

        try:
            ent_name.focus_set()
            ent_name.selection_range(0, "end")
        except Exception:
            pass


        # suggest closest matches
        try:
            import difflib
            nm = self._tg_norm_name(majestic_name)
            if nm and all_keys:
                sugg = difflib.get_close_matches(nm, [self._tg_norm_name(k) for k in all_keys], n=5, cutoff=0.35)
                # map back to original keys
                norm_to_key = {self._tg_norm_name(k): k for k in all_keys}
                sugg_keys = [norm_to_key.get(s, "") for s in sugg if norm_to_key.get(s, "")]
                if sugg_keys and not preset_local:
                    var_local.set(sugg_keys[0])
        except Exception:
            pass

        def _apply_filter(*_a):
            q = self._tg_norm_name(var_filter.get())
            if not q:
                cmb.configure(values=all_keys)
                return
            filtered = [k for k in all_keys if q in self._tg_norm_name(k)]
            cmb.configure(values=filtered)

        try:
            var_filter.trace_add("write", _apply_filter)
        except Exception:
            pass

        msg = self._label(
            top,
            "Tip: keep 'Local key' exactly equal to your folder name in car_dir (or existing stats item key).\n"
            "This is how TG income will attach to your main stats.",
            muted=True,
        )
        msg.pack(anchor="w", padx=12, pady=(10, 0))

        btns = tk.Frame(top, bg=self.colors["panel"])
        btns.pack(fill="x", padx=12, pady=12)

        def _save():
            maj = var_name.get().strip()
            loc = var_local.get().strip()
            if not maj:
                messagebox.showerror("Error", "Majestic name is empty")
                return
            if not loc:
                messagebox.showerror("Error", "Local key is empty")
                return
            self.tg_name_map[maj] = loc
            self._tg_save_name_map()
            try:
                top.destroy()
            finally:
                self._tg_map_dialog_open = False

        def _cancel():
            try:
                top.destroy()
            finally:
                self._tg_map_dialog_open = False

        ttk.Button(btns, text="Отмена", command=_cancel).pack(side="right", padx=6)
        ttk.Button(btns, text="💾 Сохранить маппинг", command=_save).pack(side="right", padx=6)

        try:
            ent_name.focus_set()
        except Exception:
            pass

        def _on_close():
            self._tg_map_dialog_open = False
            try:
                top.destroy()
            except Exception:
                pass

        top.protocol("WM_DELETE_WINDOW", _on_close)

    def _tg_rebuild_summary_from_csv(self) -> None:
        """Recompute rentals_summary.json from rentals.csv using current name_map (so mapping changes are retroactive)."""
        try:
            cfg = self.cfg_provider()
            tg = cfg.get("telegram", {}) if isinstance(cfg.get("telegram", {}), dict) else {}
            csv_path = USER_DIR / str(tg.get("output_csv", "rentals.csv"))
            json_path = USER_DIR / str(tg.get("output_json", "rentals_summary.json"))
            if not csv_path.exists():
                return

            name_map_norm = {self._tg_norm_name(k): str(v) for k, v in (self.tg_name_map or {}).items() if str(k).strip() and str(v).strip()}

            summary: Dict[str, Any] = {"by_plate": {}, "by_renter": {}, "by_car_key": {}, "unknown_cars": {}, "totals": {"revenue": 0, "hours": 0, "events": 0}}

            with csv_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    if (row.get("type") or "").strip() != "RENT_OUT":
                        continue
                    car_raw = (row.get("car") or "").strip()
                    plate = (row.get("plate") or "").strip()
                    renter = (row.get("renter") or "").strip()
                    try:
                        price = int(str(row.get("price") or "0").replace(" ", "").replace(",", ""))
                    except Exception:
                        price = 0
                    try:
                        hours = int(str(row.get("hours") or "0").strip() or "0")
                    except Exception:
                        hours = 0
                    car_key = (row.get("car_key") or "").strip()
                    if not car_key:
                        car_key = name_map_norm.get(self._tg_norm_name(car_raw), "")

                    # Use the same accumulator logic as the tracker (price is TOTAL)
                    revenue = int(price)
                    by_plate = summary.setdefault("by_plate", {})
                    by_renter = summary.setdefault("by_renter", {})
                    by_car_key = summary.setdefault("by_car_key", {})
                    unknown_cars = summary.setdefault("unknown_cars", {})
                    totals = summary.setdefault("totals", {"revenue": 0, "hours": 0, "events": 0})

                    p = by_plate.get(plate) or {"revenue": 0, "hours": 0, "events": 0, "last_renter": "", "last_price": 0, "last_hours": 0, "last_car": ""}
                    p["revenue"] += revenue
                    p["hours"] += hours
                    p["events"] += 1
                    p["last_renter"] = renter
                    p["last_price"] = price
                    p["last_hours"] = hours
                    p["last_car"] = car_raw
                    by_plate[plate] = p

                    if renter:
                        rr = by_renter.get(renter) or {"revenue": 0, "hours": 0, "events": 0, "last_plate": "", "last_car": ""}
                        rr["revenue"] += revenue
                        rr["hours"] += hours
                        rr["events"] += 1
                        rr["last_plate"] = plate
                        rr["last_car"] = car_raw
                        by_renter[renter] = rr

                    if car_key:
                        c = by_car_key.get(car_key) or {"revenue": 0, "hours": 0, "events": 0, "last_car_raw": "", "last_plate": "", "last_renter": ""}
                        c["revenue"] += revenue
                        c["hours"] += hours
                        c["events"] += 1
                        c["last_car_raw"] = car_raw
                        c["last_plate"] = plate
                        c["last_renter"] = renter
                        by_car_key[car_key] = c
                    else:
                        u = unknown_cars.get(car_raw) or {"events": 0, "last_plate": "", "last_price": 0, "last_hours": 0, "last_renter": ""}
                        u["events"] += 1
                        u["last_plate"] = plate
                        u["last_price"] = price
                        u["last_hours"] = hours
                        u["last_renter"] = renter
                        unknown_cars[car_raw] = u

                    totals["revenue"] += revenue
                    totals["hours"] += hours
                    totals["events"] += 1

            json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _tg_refresh_views(self) -> None:
        """Refresh mapping tree, totals, per-car stats and recent events list."""
        try:
            self._tg_refresh_mapping_tree()
        except Exception:
            pass

        # Load summary
        try:
            cfg = self.cfg_provider()
            tg = cfg.get("telegram", {}) if isinstance(cfg.get("telegram", {}), dict) else {}
            json_path = USER_DIR / str(tg.get("output_json", "rentals_summary.json"))
            summary = {}
            if json_path.exists():
                summary = json.loads(json_path.read_text(encoding="utf-8", errors="ignore") or "{}")
            totals = summary.get("totals", {}) or {}
            rev = int(totals.get("revenue", 0) or 0)
            hrs = int(totals.get("hours", 0) or 0)
            ev = int(totals.get("events", 0) or 0)
            if hasattr(self, "lbl_tg_totals"):
                self.lbl_tg_totals.configure(text=f"Totals: ${rev:,} | hours={hrs} | events={ev}")
            # Update dashboard income cards
            try:
                now_ts = time.time()
                income = tg_rent_tracker.income_summary(now_ts)
                active = tg_rent_tracker.active_rentals(now_ts)
                tg_cards = getattr(self, "_tg_card_vars", {})
                if tg_cards:
                    tg_cards["tg_card_today"].set(f"${income.get('today', 0):.0f}")
                    tg_cards["tg_card_week"].set(f"${income.get('week', 0):.0f}")
                    tg_cards["tg_card_active"].set(str(active.get("count", 0)))
                    # Avg $/hour from totals
                    _rph = (rev / hrs) if hrs else 0.0
                    tg_cards["tg_card_rph"].set(f"${_rph:.2f}")
            except Exception:
                pass
        except Exception:
            summary = {}

        # Per-car-key stats
        try:
            if hasattr(self, "tg_stats_tree"):
                for iid in self.tg_stats_tree.get_children():
                    self.tg_stats_tree.delete(iid)
                items = (summary.get("by_car_key", {}) or {}).items()
                rows = []
                for k, v in items:
                    try:
                        r = int(v.get("revenue", 0) or 0)
                        h = int(v.get("hours", 0) or 0)
                        e = int(v.get("events", 0) or 0)
                        rph = (r / h) if h else 0.0
                        rows.append((k, r, h, e, rph))
                    except Exception:
                        continue
                rows.sort(key=lambda x: x[1], reverse=True)
                for k, r, h, e, rph in rows[:50]:
                    self.tg_stats_tree.insert("", "end", values=(k, f"{r:,}", h, e, f"{rph:.1f}"))
        except Exception:
            pass

        # Top renters
        try:
            if hasattr(self, "tg_renter_tree"):
                for iid in self.tg_renter_tree.get_children():
                    self.tg_renter_tree.delete(iid)
                items = (summary.get("by_renter", {}) or {}).items()
                rows = []
                for k, v in items:
                    try:
                        r = int(v.get("revenue", 0) or 0)
                        h = int(v.get("hours", 0) or 0)
                        e = int(v.get("events", 0) or 0)
                        rows.append((k, r, h, e))
                    except Exception:
                        continue
                rows.sort(key=lambda x: x[1], reverse=True)
                for k, r, h, e in rows[:30]:
                    self.tg_renter_tree.insert("", "end", values=(k, f"{r:,}", h, e))
        except Exception:
            pass

        # Recent events tail
        try:
            cfg = self.cfg_provider()
            tg = cfg.get("telegram", {}) if isinstance(cfg.get("telegram", {}), dict) else {}
            csv_path = USER_DIR / str(tg.get("output_csv", "rentals.csv"))
            if hasattr(self, "tg_recent"):
                self.tg_recent.delete(0, "end")
                if csv_path.exists():
                    # read last ~4000 bytes to avoid loading huge files
                    with csv_path.open("rb") as fb:
                        fb.seek(0, os.SEEK_END)
                        size = fb.tell()
                        fb.seek(max(0, size - 50000))
                        tail = fb.read().decode("utf-8", errors="ignore").splitlines()
                    # Keep last N rows (skip header)
                    lines = [ln for ln in tail if ln.strip()]
                    # Try to parse as CSV by scanning from the end
                    # We'll just show raw lines for robustness.
                    for ln in lines[-12:]:
                        self.tg_recent.insert("end", ln[:180])
        except Exception:
            pass

    def _refresh_tg_status(self, schedule: bool = True) -> None:
        try:
            st = self.tg_tracker.status()
            state = st.get("state", "-")
            last = st.get("last_event", "-")
            if hasattr(self, "lbl_tg_status"):
                self.lbl_tg_status.configure(text=f"Сост: {state} | посл.: {last}")
            # Unknown-car popup (one at a time)
            if not getattr(self, "_tg_map_dialog_open", False):
                unk = self.tg_tracker.pop_unknown()
                if unk:
                    self._tg_open_map_dialog(unk)
        except Exception:
            pass

        # refresh views periodically (cheap enough)
        try:
            now = time.time()
            if now - float(getattr(self, "_tg_last_view_refresh_ts", 0.0)) > 2.0:
                self._tg_last_view_refresh_ts = now
                self._tg_refresh_views()
        except Exception:
            pass

        if schedule and not getattr(self, "_closing", False):
            try:
                self.after(1200, lambda: self._refresh_tg_status(schedule=True))
            except Exception:
                pass


    def _build_logs_tab(self):
        p = self._panel(self.tab_logs)
        p.pack(fill="both", expand=True, padx=10, pady=10)

        # Header row with title and controls
        top = tk.Frame(p, bg=self.colors["panel"])
        top.pack(fill="x", padx=12, pady=(10, 6))

        # Section title
        tk.Label(top, text="📋 ЖУРНАЛ СОБЫТИЙ", bg=self.colors["panel"],
                 fg=self.colors["muted"], font=("Segoe UI", 8, "bold")).pack(side="left")

        logging_box = ttk.LabelFrame(p, text="⚙ Настройки логирования (LIVE)")
        logging_box.pack(fill="x", padx=12, pady=(0, 8))
        log_sections = self._collect_settings_sections(allowed_sections={"📋 Логирование"})
        self._build_settings_tabs(logging_box, log_sections)

        # Control buttons row
        ctrl_row = tk.Frame(p, bg=self.colors["panel"])
        ctrl_row.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(ctrl_row, text="🗑 Очистить", command=lambda: self.log_view.delete("1.0","end")).pack(side="left", padx=(0, 6))
        ttk.Button(ctrl_row, text="📂 Открыть файл лога", command=self.open_log).pack(side="left", padx=6)

        # --- reset / cleanup controls (double + hold) ---
        self.reset_armed_var = tk.BooleanVar(value=False)
        self.reset_status_var = tk.StringVar(value="Сброс: ЗАБЛОКИРОВАН")

        ttk.Checkbutton(
            ctrl_row,
            text="⚠ ВЗВЕСТИ СБРОС (10с)",
            variable=self.reset_armed_var,
            command=self._on_reset_arm_changed,
        ).pack(side="left", padx=(14, 6))

        self.btn_clear_log_file = self._make_hold_button(
            ctrl_row,
            text="ДЕРЖАТЬ 2с: Очистить файл лога",
            hold_ms=2000,
            command=self._clear_log_file_action,
        )
        self.btn_clear_log_file.pack(side="left", padx=6)

        self.btn_reset_stats = self._make_hold_button(
            ctrl_row,
            text="ДЕРЖАТЬ 2с: Сброс СТАТИСТИКИ (stats+archive)",
            hold_ms=2000,
            command=self._reset_stats_action,
        )
        self.btn_reset_stats.pack(side="left", padx=6)

        tk.Label(ctrl_row, textvariable=self.reset_status_var, fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="right")

        self.log_view = scrolledtext.ScrolledText(
            p,
            height=28,
            bg=self.colors["panel2"],
            fg=self.colors["fg"],
            insertbackground=self.colors["fg"],
            relief="flat",
        )
        self.log_view.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_view.insert("end", f"Logs: {LOG_PATH}\n")
        self.log_view.see("end")

        self._on_reset_arm_changed()

    # ── Items Monitor tab (Admin mode) ───────────────────────────────────
    def _build_items_tab(self):
        """Вкладка Items Monitor — мониторинг продажи предметов на маркетплейсе.
        Инициализирует ItemSaleMonitor с прямой передачей self.run_event/self.stop_event.
        """
        if not _ITEM_MONITOR_AVAILABLE:
            return
        tab_parent = getattr(self, "tab_items", None)
        if tab_parent is None:
            return
        try:
            self._item_monitor = ItemSaleMonitor(
                cfg_provider     = self.cfg_provider,
                run_event        = self.run_event,
                stop_event       = self.stop_event,
                log              = log,  # log — глобальная функция модуля, не self.log
                on_status_change = self._on_item_status_change,
                on_stats_update  = self._on_item_stats_update,  # обновление статистики
                standalone       = True,  # работает независимо от бота аренды
                loop_idle_event  = self.loop_idle_event,  # координация: ждём пока бот простаивает
                items_busy_event = self.items_busy_event,  # блокируем LoopManager на время цикла предметов
            )
            self._item_monitor.start()

            self._item_sale_tab = ItemSaleTab(
                tab_parent,
                cfg_provider = self.cfg_provider,
                cfg_saver    = lambda cfg: self._apply_cfg_patch(cfg),
                log          = log,  # log — глобальная функция модуля
            )
            self._item_sale_tab.pack(fill="both", expand=True)
            # Передаём ссылку на монитор во вкладку — для кнопок Старт/Стоп
            self._item_sale_tab.set_monitor(self._item_monitor)
            log("[Items Monitor] вкладка загружена")
        except Exception as _ex:
            log(f"[Items Monitor] ошибка инициализации: {_ex}")

    def _restart_item_monitor(self) -> None:
        """
        Останавливает текущий ItemSaleMonitor (если жив) и создаёт новый.
        Вызывается из start_or_resume() после stop_event.clear() чтобы:
          - убить старый поток (он мог выжить при standalone=True)
          - создать свежий объект с очищенным stop_event
        """
        if not _ITEM_MONITOR_AVAILABLE:
            return
        if self._item_sale_tab is None:
            return  # вкладка не была построена — нечего перезапускать
        try:
            # Явно останавливаем старый монитор — при standalone=True он мог выжить
            # (stop_event уже clear к этому моменту, поэтому стопаем через его own stop_event)
            if self._item_monitor is not None and self._item_monitor.is_alive():
                self._item_monitor.stop()  # взводит внутренний _stop_event старого объекта
                # Даём потоку секунду умереть (sleep_coop просыпается каждые 0.5с)
                old_thread = getattr(self._item_monitor, "_thread", None)
                if old_thread is not None:
                    old_thread.join(timeout=2.0)
                log("[Items Monitor] старый монитор остановлен")

            # Создаём новый объект с чистым состоянием
            self._item_monitor = ItemSaleMonitor(
                cfg_provider     = self.cfg_provider,
                run_event        = self.run_event,
                stop_event       = self.stop_event,
                log              = log,
                on_status_change = self._on_item_status_change,
                on_stats_update  = self._on_item_stats_update,
                standalone       = True,
                loop_idle_event  = self.loop_idle_event,
                items_busy_event = self.items_busy_event,  # блокируем LoopManager на время цикла предметов
            )
            self._item_monitor.start()
            # Обновляем ссылку во вкладке чтобы кнопки Старт/Стоп работали
            self._item_sale_tab.set_monitor(self._item_monitor)
            log("[Items Monitor] монитор перезапущен")
        except Exception as _ex:
            log(f"[Items Monitor] ошибка перезапуска: {_ex}")

    def _on_item_status_change(self, results) -> None:
        """Каллбак от ItemSaleMonitor — маршалируем в GUI-поток через after()."""
        if self._item_sale_tab is not None:
            try:
                self.after(0, lambda r=results: self._item_sale_tab.update_results(r))
            except Exception:
                pass

    def _on_item_stats_update(self, stats: dict) -> None:
        """Каллбак статистики от ItemSaleMonitor — маршалируем в GUI-поток."""
        if self._item_sale_tab is not None:
            try:
                self.after(0, lambda s=stats: self._item_sale_tab.update_stats(s))
            except Exception:
                pass

    def nudge_now(self):
        if pyautogui is None:
            messagebox.showerror("Missing", "pyautogui not installed")
            return
        cfg = self.cfg_provider()
        ok = nudge_focus_to_create(cfg)
        log(f"GUI: nudge_now -> {'OK' if ok else 'FAIL'}")

    def reset_processed(self):
        if messagebox.askyesno("Reset processed", f"Delete {PROCESSED_PATH.name}?"):
            try:
                if PROCESSED_PATH.exists():
                    PROCESSED_PATH.unlink()
                log("processed.json reset")
                messagebox.showinfo("Reset", "processed.json deleted.")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def open_visual_debug(self):
        """Open a small window with an annotated screenshot (vehicle_region + FASTSCAN hits)."""
        try:
            cfg = self.cfg_provider() if hasattr(self, "cfg_provider") else {}
        except Exception:
            cfg = {}

        # Build fresh fast-scan cache so the picture matches reality
        try:
            if cfg.get("fast_scan_enabled", True):
                fast_scan_build(cfg, list_templates(cfg))
        except Exception:
            pass

        try:
            import PIL.ImageTk as ImageTk
            from PIL import ImageDraw
        except Exception as e:
            try:
                messagebox.showerror("Visual Debug", f"PIL/ImageTk missing: {e}")
            except Exception:
                pass
            return

        region = _region_to_tuple(cfg.get("vehicle_region"))
        origin = (0, 0)
        try:
            if region:
                origin = (int(region[0]), int(region[1]))
                shot = pyautogui.screenshot(region=region)
            else:
                shot = pyautogui.screenshot()
        except Exception as e:
            try:
                messagebox.showerror("Visual Debug", f"Screenshot failed: {e}")
            except Exception:
                pass
            return

        img = shot.copy()
        draw = ImageDraw.Draw(img)

        # Draw regions
        try:
            pr = _region_to_tuple(cfg.get("plate_region"))
            if pr:
                x, y, w, h = pr
                draw.rectangle((x - origin[0], y - origin[1], x - origin[0] + w, y - origin[1] + h), outline="yellow", width=2)
        except Exception:
            pass
        try:
            sr = _region_to_tuple(cfg.get("stall_region"))
            if sr:
                x, y, w, h = sr
                draw.rectangle((x - origin[0], y - origin[1], x - origin[0] + w, y - origin[1] + h), outline="cyan", width=2)
        except Exception:
            pass

        # Draw FASTSCAN hits
        try:
            with FAST_SCAN_LOCK:
                pos_map = dict(FAST_SCAN_CACHE.get("pos") or {})
            for stem, hits in pos_map.items():
                for (cx, cy, sc) in hits[:5]:  # cap per stem
                    px = int(cx - origin[0])
                    py = int(cy - origin[1])
                    if px < 0 or py < 0 or px >= img.size[0] or py >= img.size[1]:
                        continue
                    r = 8
                    draw.ellipse((px - r, py - r, px + r, py + r), outline="lime", width=2)
                    draw.text((px + 10, py - 10), f"{stem} {sc:.2f}", fill="lime")
        except Exception:
            pass

        # Show window
        win = tk.Toplevel(self)
        win.title("Визуальная отладка")
        win.configure(bg=self.colors["panel"])

        # Scrollable canvas
        canvas = tk.Canvas(win, bg=self.colors["panel"], highlightthickness=0)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        hsb = ttk.Scrollbar(win, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)

        tk_img = ImageTk.PhotoImage(img)
        canvas._tk_img = tk_img  # keep ref
        canvas.create_image(0, 0, image=tk_img, anchor="nw")
        canvas.configure(scrollregion=(0, 0, img.size[0], img.size[1]))

    def reset_photo_hashes(self):
        if messagebox.askyesno("Reset dedupe", f"Delete {PHOTO_HASHES_PATH.name}?"):
            try:
                if PHOTO_HASHES_PATH.exists():
                    PHOTO_HASHES_PATH.unlink()
                log("photo_hashes.json reset")
                messagebox.showinfo("Reset", "photo_hashes.json deleted.")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def start_or_resume(self):
        if pyautogui is None:
            messagebox.showerror("Missing", "pyautogui not installed.\nInstall: pip install pyautogui opencv-python")
            return

        cfg_now = self.cfg_provider()
        if bool(cfg_now.get("start_nudge_enabled", True)):
            nudge_focus_to_create(cfg_now)

        if self.loop_thread and self.loop_thread.is_alive():
            self.run_event.set()
            self.status_var.set("▶ РАБОТАЕТ")
            log("GUI: resume")
            return

        self.stop_event.clear()
        self.run_event.set()

        # Перезапускаем ItemSaleMonitor — его поток мог умереть из-за stop_event
        self._restart_item_monitor()

        IdleTracker.mark_activity()

        self.loop_thread = LoopManager(
            cfg_provider=self.cfg_provider,
            run_event=self.run_event,
            stop_event=self.stop_event,
            log=log,
            list_templates=list_templates,
            is_valid_item=is_valid_item,
            enter_create_rent=enter_create_rent,  # use cooldown-aware version (force was spamming UI)
            post_one_item=post_one_item,
            load_processed=load_processed,
            save_processed=save_processed,
            load_photo_hashes=load_photo_hashes,
            save_photo_hashes=save_photo_hashes,
            record_post=Stats.record_post,
            append_archive_row=append_archive_row,
            pre_sweep_hook=loop_pre_sweep_hook,
            watchdog_tick=loop_watchdog_tick,
            visible_templates_provider=fast_scan_visible_templates,
            loop_idle_event=self.loop_idle_event,    # передаём событие простоя в менеджер цикла
            items_busy_event=self.items_busy_event,  # ждём завершения цикла предметов перед sweep
        )
        self.loop_thread.start()

        self.status_var.set("▶ РАБОТАЕТ")
        log("GUI: started")

    def pause(self):
        self.run_event.clear()
        self.status_var.set("⏸ ПАУЗА")
        log("GUI: paused")

    def toggle_pause_resume(self):
        if self.run_event.is_set():
            self.pause()
        else:
            self.start_or_resume()

    def stop_hard(self):
        self.run_event.clear()
        self.stop_event.set()
        self.status_var.set("⏹ СТОП…")
        log("GUI: stop requested")

    def browse_car_dir(self):
        ddd = filedialog.askdirectory(title="Select car_dir (png + folders)")
        if ddd:
            self.car_dir_var.set(ddd)

    def _wait_for_stable_mouse(self, hold_seconds: float = 5.0, poll: float = 0.1):
        if pyautogui is None:
            raise RuntimeError("pyautogui not installed")
        try:
            last_pos = pyautogui.position()
        except Exception as e:
            raise RuntimeError(str(e)) from e
        stable_since = time.time()
        while True:
            time.sleep(poll)
            try:
                pos = pyautogui.position()
            except Exception:
                pos = last_pos
            if pos != last_pos:
                stable_since = time.time()
                last_pos = pos
            if time.time() - stable_since >= hold_seconds:
                return last_pos

    def _run_capture_sequence(self, label: str, fn, *, show_ui_before: bool = True):
        if pyautogui is None:
            messagebox.showerror("Missing", "pyautogui not installed")
            return None
        try:
            self.iconify()
            self.update_idletasks()
        except Exception:
            pass
        log(f"{label}: hold mouse still for 5 seconds (moving resets timer).")
        try:
            pos = self._wait_for_stable_mouse(5.0)
        except Exception as e:
            try:
                self.deiconify()
            except Exception:
                pass
            messagebox.showerror("Capture", str(e))
            return None
        if show_ui_before:
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass
        try:
            return fn(pos)
        finally:
            if not show_ui_before:
                try:
                    self.deiconify()
                    self.lift()
                except Exception:
                    pass

    def capture_coord(self, key: str):
        def _apply(pos):
            x, y = pos
            xv, yv = self.coord_vars[key]
            xv.set(str(x))
            yv.set(str(y))
            log(f"Captured {key} = [{x}, {y}]")
        self._run_capture_sequence(f"Capture {key}", _apply, show_ui_before=False)


    
    def _refresh_regions_ui(self):
        try:
            self.plate_region_var.set(str(self.cfg.get("plate_region")))
        except Exception:
            pass
        try:
            self.stall_region_var.set(str(self.cfg.get("stall_region")))
        except Exception:
            pass
        try:
            if hasattr(self, "vehicle_region_var"):
                self.vehicle_region_var.set(str(self.cfg.get("vehicle_region")))
        except Exception:
            pass

    def _open_drawer(self, title: str, key: str, fixed_size: Optional[Tuple[int, int]] = None):
        def _done(region):
            if region is None:
                return
            self._apply_cfg_patch({key: region})
            self._refresh_regions_ui()

        def _open(_pos=None):
            try:
                RegionDrawer(self, title=title, on_done=_done, fixed_size=fixed_size)
            except Exception as e:
                messagebox.showerror("Draw", f"Cannot open drawer: {e}")

        self._run_capture_sequence(f"Draw {key}", _open, show_ui_before=True)

    def draw_vehicle_region(self):
        self._open_drawer("Select VEHICLE region", "vehicle_region", fixed_size=None)

    def draw_stall_region(self):
        self._open_drawer("Select STALL region", "stall_region", fixed_size=None)

    def draw_plate_region(self):
        self._open_drawer("Select PLATE region", "plate_region")

    def capture_plate_label_anchor(self):
        """Draw a rectangle over the text 'Гос.Номер:' (label only) and save it as a template anchor."""
        def _done(region):
            try:
                rx, ry, rw, rh = [int(v) for v in region]
                img = pyautogui.screenshot(region=(rx, ry, rw, rh))
                img.save(PLATE_LABEL_ANCHOR_PATH)
                self._apply_cfg_patch({"plate_label_anchor_region": [rx, ry, rw, rh]})
                log(f"Saved plate label anchor: {PLATE_LABEL_ANCHOR_PATH}")
                self._update_plate_anchor_status()
            except Exception as e:
                messagebox.showerror("Anchor", f"Failed to save anchor: {e}")

        def _open(_pos=None):
            try:
                RegionDrawer(self, title="Select plate label anchor (Гос.Номер:)", on_done=_done, fixed_size=None)
            except Exception as e:
                messagebox.showerror("Draw", f"Cannot open drawer: {e}")

        self._run_capture_sequence("Draw plate label anchor", _open, show_ui_before=True)

    def clear_plate_label_anchor(self):
        try:
            if PLATE_LABEL_ANCHOR_PATH.exists():
                PLATE_LABEL_ANCHOR_PATH.unlink()
                log("Cleared plate label anchor")
        except Exception:
            pass
        self._update_plate_anchor_status()

    def _update_plate_anchor_status(self):
        if hasattr(self, "plate_anchor_status_var"):
            self.plate_anchor_status_var.set("✅ Установлен" if PLATE_LABEL_ANCHOR_PATH.exists() else "❌ Отсутствует")



    def _bl_test_plate_now(self):
        """Capture current plate and show best blacklist match + whether it would block."""
        try:
            live, dbg = _grab_plate_value_live(self.cfg)
            if live is None:
                messagebox.showwarning("Plate test", "Can't capture plate. Open the FORM screen and set PLATE region first.")
                return
            thr = float(self.cfg.get("plate_blacklist_confidence", 0.94))
            best_label, best_score, _p = plate_blacklist_best(self.cfg)
            blocked = bool(best_label and best_score >= thr)

            msg = f"Label anchor: {'OK' if PLATE_LABEL_ANCHOR_PATH.exists() else 'missing'}\n"
            msg += f"Live extraction: label_found={dbg.get('label_found')} score={dbg.get('label_score',0):.3f}\n"
            msg += f"Best blacklist match: {best_label or 'none'} score={best_score:.3f} (thr={thr:.3f})\n"
            msg += ("RESULT: BLOCKED (НЕ СДАВАТЬ)" if blocked else "RESULT: NOT blocked")

            messagebox.showinfo("Plate test", msg)
        except Exception as e:
            messagebox.showerror("Plate test", str(e))
    def _capture_plate_ref_for_vehicle(self):


        """Capture current plate_region from screen into <vehicle_folder>/plate_ref.png."""
        folder = None
        try:
            folder = self._selected_folder()
        except Exception:
            folder = None
        if not folder:
            try:
                messagebox.showinfo("Select", "Select a vehicle first.")
            except Exception:
                pass
            return

        if pyautogui is None:
            messagebox.showerror("Missing", "pyautogui is not installed.")
            return
        region = _region_to_tuple(self.cfg.get("plate_region"))
        if region is None:
            messagebox.showerror("Missing", "plate_region is not set. Use Coords -> Draw Plate region first.")
            return

        def _capture(_pos=None):
            try:
                shot, dbg = _grab_plate_value_live(self.cfg)
                if shot is None:
                    shot = pyautogui.screenshot(region=region)
                out = folder / "plate_ref.png"
                shot.save(out)
                log(f"EDITOR: saved plate_ref.png for {folder.name}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to capture plate_ref.png: {e}")
                return

            # update preview
            try:
                self._update_plate_preview()
            except Exception:
                pass

        self._run_capture_sequence("Capture plate ref", _capture, show_ui_before=False)

    def _update_plate_preview(self):
        """Update Plate sanity preview (live plate crop vs saved ref) for selected vehicle."""
        # If UI not built yet, ignore
        if not hasattr(self, "plate_preview_status_var"):
            return

        folder = None
        try:
            folder = self._selected_folder()
        except Exception:
            folder = None
        if not folder:
            self.plate_preview_status_var.set("Превью номера: выберите машину")
            return

        region = _region_to_tuple(self.cfg.get("plate_region"))
        if region is None:
            self.plate_preview_status_var.set("Превью номера: задайте регион номера")
            self._set_plate_preview_images(None, None)
            return

        live_img = None
        if pyautogui is not None:
            try:
                live_img = pyautogui.screenshot(region=region)
            except Exception:
                live_img = None

        ref_path = folder / "plate_ref.png"
        ref_img = None
        if ref_path.exists():
            try:
                from PIL import Image
                ref_img = Image.open(ref_path).convert("RGB")
            except Exception:
                ref_img = None

        self._set_plate_preview_images(live_img, ref_img)

        # Compute status
        required = False
        try:
            sched = load_schedule(folder)
            required = bool(sched.get("plate_require", False))
        except Exception:
            required = False

        expected_txt = ""
        try:
            expected_txt = str(load_schedule(folder).get("plate_text", "")).strip()
        except Exception:
            expected_txt = ""

        status_bits = []
        if required:
            status_bits.append("REQUIRED")
        if ref_img is not None:
            status_bits.append("ref=OK")
        elif expected_txt:
            status_bits.append("ref=— (OCR)")
        else:
            status_bits.append("ref=—")

        # live evaluation
        verdict = "—"
        extra = ""
        try:
            # template match if possible
            if live_img is not None and ref_img is not None and HAS_OPENCV and cv2 is not None and np is not None:
                arr = np.array(live_img)
                if arr.ndim == 3:
                    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
                else:
                    gray = arr
                # load ref gray
                ref_arr = np.array(ref_img)
                if ref_arr.ndim == 3:
                    ref_gray = cv2.cvtColor(ref_arr, cv2.COLOR_RGB2GRAY)
                else:
                    ref_gray = ref_arr
                res = cv2.matchTemplate(gray, ref_gray, cv2.TM_CCOEFF_NORMED)
                _minv, maxv, _minloc, _maxloc = cv2.minMaxLoc(res)
                thr = float(self.cfg.get("plate_confidence", 0.82))
                verdict = "OK" if float(maxv) >= thr else "MISMATCH"
                extra = f"  score={float(maxv):.2f} thr={thr:.2f}"
            elif live_img is None:
                verdict = "NO LIVE"
            else:
                verdict = "LIVE OK"
        except Exception:
            verdict = "ERR"

        self.plate_preview_status_var.set(f"Превью номера: {verdict}  [{' '.join(status_bits)}]{extra}")

    def _set_plate_preview_images(self, live_img, ref_img):
        """Internal: render preview images into labels."""
        try:
            from PIL import Image
        except Exception:
            return

        def _prep(img):
            if img is None:
                return None
            try:
                if not hasattr(img, "size"):
                    return None
                # normalize to RGB and resize
                im = img
                if hasattr(im, "convert"):
                    im = im.convert("RGB")
                im = im.copy()
                im.thumbnail((340, 90))
                bg = Image.new("RGB", (340, 90), (15, 23, 42))
                bg.paste(im, (0, max(0, (90 - im.size[1]) // 2)))
                im = bg
                return im
            except Exception:
                return None

        live_pre = _prep(live_img)
        ref_pre = _prep(ref_img)

        # keep references to avoid gc
        self._plate_live_photo = None
        self._plate_ref_photo = None

        if hasattr(self, "lbl_plate_live") and live_pre is not None:
            try:
                self._plate_live_photo = ImageTk.PhotoImage(live_pre)
                self.lbl_plate_live.configure(image=self._plate_live_photo, text="")
            except Exception:
                pass
        elif hasattr(self, "lbl_plate_live"):
            self.lbl_plate_live.configure(image="", text="(no live)")

        if hasattr(self, "lbl_plate_ref") and ref_pre is not None:
            try:
                self._plate_ref_photo = ImageTk.PhotoImage(ref_pre)
                self.lbl_plate_ref.configure(image=self._plate_ref_photo, text="")
            except Exception:
                pass
        elif hasattr(self, "lbl_plate_ref"):
            self.lbl_plate_ref.configure(image="", text="(no ref)")

    def capture_plate_region(self):
        def _apply(pos):
            x, y = pos
            w = int(self.cfg.get("plate_w", 320))
            h = int(self.cfg.get("plate_h", 60))
            region = [int(x), int(y), w, h]
            self._apply_cfg_patch({"plate_region": region})
            if hasattr(self, "plate_region_var"):
                self.plate_region_var.set(str(region))
            log(f"GUI: plate_region captured: {region}")
        self._run_capture_sequence("Capture plate region", _apply, show_ui_before=False)

    def capture_stall_region(self):
        def _apply(pos):
            x, y = pos
            w = int(self.cfg.get("stall_w", 260))
            h = int(self.cfg.get("stall_h", 120))
            region = [int(x), int(y), w, h]
            self._apply_cfg_patch({"stall_region": region})
            if hasattr(self, "stall_region_var"):
                self.stall_region_var.set(str(region))
            log(f"GUI: stall_region captured: {region}")
        self._run_capture_sequence("Capture stall region", _apply, show_ui_before=False)

    def _refresh_bump_points_ui(self):
        if not hasattr(self, "bump_points_list"):
            return
        self.bump_points_list.delete(0, tk.END)
        pts = self.cfg.get("bump_points") or []
        if isinstance(pts, list):
            for i, p in enumerate(pts):
                try:
                    self.bump_points_list.insert(tk.END, f"{i+1:02d}: {int(p[0])}, {int(p[1])}")
                except Exception:
                    continue

    def capture_bump_point(self):
        def _apply(pos):
            x, y = pos
            pts = list(self.cfg.get("bump_points") or [])
            pts.append([int(x), int(y)])
            self._apply_cfg_patch({"bump_points": pts})
            self._refresh_bump_points_ui()
            log(f"GUI: bump point added: {[int(x), int(y)]}")
        self._run_capture_sequence("Capture bump point", _apply, show_ui_before=False)

    def remove_bump_point(self):
        if not hasattr(self, "bump_points_list"):
            return
        sel = self.bump_points_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        pts = list(self.cfg.get("bump_points") or [])
        if 0 <= idx < len(pts):
            pts.pop(idx)
            self._apply_cfg_patch({"bump_points": pts})
            self._refresh_bump_points_ui()

    def clear_bump_points(self):
        self._apply_cfg_patch({"bump_points": []})
        self._refresh_bump_points_ui()

    def test_bump_points(self):
        # Immediate single run of bump using current settings (non-blocking)
        try:
            bump_my_ads(self.cfg_provider(), self.run_event, self.stop_event)
        except Exception as e:
            messagebox.showerror("BUMP", f"Error: {e}")

    def test_click(self, key: str):
        cfg = self.cfg_provider()
        xy = cfg["coords"].get(key)
        if not xy:
            return
        log(f"Test click {key} -> {xy}")
        init_pyautogui()
        click_xy(xy)

    def capture_form_anchor(self):
        def _apply(pos):
            init_pyautogui()
            x, y = pos
            w, h = 320, 120
            x0 = max(0, x - w // 2)
            y0 = max(0, y - h // 2)
            try:
                img = pyautogui.screenshot(region=(x0, y0, w, h))
                img.save(str(ANCHOR_FORM_PATH))
                log(f"Saved FORM anchor: {ANCHOR_FORM_PATH}")
                self.anchor_status.set("OK")
                messagebox.showinfo("Saved", f"Saved anchor:\n{ANCHOR_FORM_PATH}")
            except Exception as e:
                messagebox.showerror("Capture failed", str(e))
        self._run_capture_sequence("Capture FORM anchor", _apply, show_ui_before=False)

    def delete_form_anchor(self):
        try:
            if ANCHOR_FORM_PATH.exists():
                ANCHOR_FORM_PATH.unlink()
            self.anchor_status.set("NOT SET")
            log("FORM anchor deleted")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def open_user_dir(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(USER_DIR))  # type: ignore
            else:
                messagebox.showinfo("Folder", str(USER_DIR))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def open_log(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_PATH))  # type: ignore
            else:
                messagebox.showinfo("Log", str(LOG_PATH))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def open_archive(self):
        try:
            if not ARCHIVE_CSV_PATH.exists():
                messagebox.showinfo(
                    "Archive",
                    f"archive.csv ещё не создан.\nСоздастся после первого успешного поста.\n{ARCHIVE_CSV_PATH}",
                )
                return
            if sys.platform.startswith("win"):
                os.startfile(str(ARCHIVE_CSV_PATH))  # type: ignore
            else:
                messagebox.showinfo("Archive", str(ARCHIVE_CSV_PATH))
        except Exception as e:
            messagebox.showerror("Error", str(e))
    # ---------- Enhanced analytics (archive-based) ----------
    def _load_archive_events(self, force: bool = False):
        """
        Load event-level rows from archive.csv:
        each row = {"dt": datetime, "stem": str, "price_value": float, "price_raw": str}
        Cached by file mtime.
        """
        try:
            if not ARCHIVE_CSV_PATH.exists():
                self._archive_cache_mtime = None
                self._archive_cache_events = []
                return []

            mtime = ARCHIVE_CSV_PATH.stat().st_mtime
            if (not force) and getattr(self, "_archive_cache_mtime", None) == mtime:
                return getattr(self, "_archive_cache_events", [])

            evts = []
            with open(ARCHIVE_CSV_PATH, "r", encoding="utf-8", errors="ignore", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    try:
                        ts = (row.get("timestamp") or "").strip()
                        stem = (row.get("stem") or "").strip()
                        pr = (row.get("price_raw") or "").strip()
                        pv = float(row.get("price_value") or 0.0)
                        if not ts or not stem:
                            continue
                        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        evts.append({"dt": dt, "stem": stem, "price_value": pv, "price_raw": pr})
                    except Exception:
                        continue

            evts.sort(key=lambda e: e["dt"])
            self._archive_cache_mtime = mtime
            self._archive_cache_events = evts
            return evts
        except Exception as e:
            log(f"archive load error: {e}")
            return []

    @staticmethod
    def _load_pick_attempt_events(self, force: bool = False):
        """Load rows from pick_attempts.csv (selection attempts before success)."""
        try:
            if not PICK_ATTEMPTS_CSV_PATH.exists():
                self._pick_cache_mtime = None
                self._pick_cache_events = []
                return []

            mtime = PICK_ATTEMPTS_CSV_PATH.stat().st_mtime
            if (not force) and getattr(self, "_pick_cache_mtime", None) == mtime:
                return getattr(self, "_pick_cache_events", [])

            evts = []
            with open(PICK_ATTEMPTS_CSV_PATH, "r", encoding="utf-8", errors="ignore", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    try:
                        dt = datetime.strptime(str(row.get("timestamp", "")).strip(), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    stem = str(row.get("stem", "")).strip()
                    if not stem:
                        continue
                    try:
                        attempts = int(float(row.get("attempts", 0)))
                    except Exception:
                        attempts = 0
                    try:
                        success = bool(int(str(row.get("success", "0")).strip() or "0"))
                    except Exception:
                        success = False
                    evts.append({"dt": dt, "stem": stem, "attempts": attempts, "success": success})

            evts.sort(key=lambda e: e["dt"], reverse=True)
            self._pick_cache_mtime = mtime
            self._pick_cache_events = evts
            return evts
        except Exception:
            self._pick_cache_mtime = None
            self._pick_cache_events = []
            return []


    
    def _format_pick_stats(self, vehicle: str) -> str:
        try:
            m = getattr(self, "_pick_stats_by_vehicle", {}) or {}
            it = m.get(vehicle)
            if not it:
                return ""
            avg = float(it.get("avg", 0.0))
            last = int(it.get("last", 0))
            return f"  pick_avg={avg:.2f}  pick_last={last}"
        except Exception:
            return ""

    def _on_stats_heading_click(self, col: str) -> None:
        # Clicking a column header sets sort key; clicking again toggles ASC/DESC.
        try:
            if not hasattr(self, "sort_key_var") or not hasattr(self, "sort_desc_var"):
                return
            cur = self.sort_key_var.get()
            if cur == col:
                self.sort_desc_var.set(not bool(self.sort_desc_var.get()))
            else:
                self.sort_key_var.set(col)
                # default: numeric columns DESC, name ASC
                if col == "vehicle" or col == "last_posted_at":
                    self.sort_desc_var.set(False)
                else:
                    self.sort_desc_var.set(True)
            self._refresh_stats(force=True)
        except Exception:
            pass

    def _on_vehicle_select(self, _evt=None):
        # Ignore selection events while we repaint the tree
        if getattr(self, "_suppress_select_event", False):
            return
        v = None
        try:
            sel = self.tree.selection()
            if sel:
                vals = self.tree.item(sel[0], "values")
                if vals:
                    v = str(vals[0])
        except Exception:
            v = None
        if v:
            self._selected_vehicle_cache = v
            self._last_manual_select_ts = time.time()
        self._refresh_selected_details()

    def _refresh_selected_details(self):
        try:
            if not hasattr(self, "_events_by_vehicle"):
                return
            sel = self.tree.selection()
            if not sel:
                self.sel_title_var.set("Select a vehicle to see details…")
                self.sel_stats_var.set("")
                self._set_recent_text("Recent posts will appear here.\n")
                return

            vals = self.tree.item(sel[0], "values")
            if not vals:
                return
            vehicle = str(vals[0])
            evts = self._events_by_vehicle.get(vehicle, []) or []
            if not evts:
                self.sel_title_var.set(vehicle)
                self.sel_stats_var.set("No events in selected window.")
                self._set_recent_text("")
                return

            prices = [float(e["price_value"]) for e in evts]
            posts = len(evts)
            revenue = sum(prices)
            avgp = revenue / posts if posts else 0.0
            minp = min(prices) if prices else 0.0
            maxp = max(prices) if prices else 0.0
            medp = statistics.median(prices) if prices else 0.0
            uniq = len(set(prices))
            first_dt = evts[-1]["dt"]
            last_dt = evts[0]["dt"]

            # price distribution (top 5)
            dist = {}
            for p in prices:
                dist[p] = dist.get(p, 0) + 1
            top_dist = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            dist_str = ", ".join([f"{k:.0f}×{v}" for k, v in top_dist])

            win = self.stats_window_var.get()
            self.sel_title_var.set(f"{vehicle}  (window: {win})")
            self.sel_stats_var.set(
                f"posts={posts}  Σ={revenue:.2f}  avg={avgp:.2f}  min={minp:.2f}  max={maxp:.2f}  median={medp:.2f}  unique_prices={uniq}  "
                f"first={first_dt.strftime('%Y-%m-%d %H:%M:%S')}…{last_dt.strftime('%Y-%m-%d %H:%M:%S')}  top_prices: {dist_str}"
                f"{self._format_pick_stats(vehicle)}"
            )

            # recent list
            lines = []
            for e in evts[:15]:
                lines.append(f"{e['dt'].strftime('%Y-%m-%d %H:%M:%S')}   ${float(e['price_value']):.2f}")
            self._set_recent_text("\n".join(lines) + ("\n" if lines else ""))

            # Keep editor + AI + previews in sync with current selection
            # IMPORTANT: do NOT overwrite editor fields while user is typing (plate/base/multipliers etc.)
            try:
                if not getattr(self, '_editor_is_typing', False):
                    if (vehicle != getattr(self, '_last_editor_loaded_vehicle', '')) or (not getattr(self, '_editor_dirty', False)):
                        self._load_editor_from_files()
                        self._last_editor_loaded_vehicle = vehicle
            except Exception:
                pass
            try:
                self._update_multiplier_previews()
            except Exception:
                pass
            try:
                self._refresh_suggestions()
            except Exception:
                pass

        except Exception as e:
            log(f"details error: {e}")

    def _set_recent_text(self, s: str):
        try:
            self.sel_recent.config(state="normal")
            self.sel_recent.delete("1.0", "end")
            self.sel_recent.insert("end", s)
            self.sel_recent.config(state="disabled")
        except Exception:
            pass

    def open_charts(self):
        # Make sure stats were computed at least once (so charts have data)
        try:
            self._refresh_stats(force=True)
        except Exception:
            pass
        try:
            if getattr(self, "_charts_win", None) and self._charts_win.winfo_exists():
                self._charts_win.focus_force()
                return

            w = tk.Toplevel(self)
            w.title("Stats charts")
            w.geometry("1100x620")
            w.configure(bg=self.colors["bg"])
            self._charts_win = w

            top = tk.Frame(w, bg=self.colors["panel"])
            top.pack(fill="x", padx=10, pady=10)
            tk.Label(
                top,
                text="Графики отражают текущие фильтры статистики.",
                fg=self.colors["muted"],
                bg=self.colors["panel"],
            ).pack(side="left", padx=8)

            canvas = tk.Canvas(w, bg=self.colors["panel2"], highlightthickness=0)
            canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))

            # Redraw on resize, and once after layout settles (Canvas size becomes valid)
            canvas.bind("<Configure>", lambda _e: self._draw_charts(canvas))
            w.after(120, lambda: self._draw_charts(canvas))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _draw_charts(self, canvas: "tk.Canvas"):
        try:
            canvas.delete("all")
            cw = int(canvas.winfo_width() or 1100)
            ch = int(canvas.winfo_height() or 560)
            pad = 18

            # Try to use the already computed table rows
            rows = getattr(self, "_stats_rows", None) or []
            if not rows:
                # One forced refresh can help if user clicked Charts immediately after launch
                try:
                    self._refresh_stats(force=True)
                    rows = getattr(self, "_stats_rows", None) or []
                except Exception:
                    rows = []

            left_x0 = pad
            left_x1 = cw // 2 - pad
            right_x0 = cw // 2 + pad
            right_x1 = cw - pad

            # Titles
            mid_y = ch // 2
            canvas.create_text(left_x0, pad, anchor="nw",
                               text="Топ по Σ цене", fill=self.colors["fg"],
                               font=("Segoe UI", 12, "bold"))
            canvas.create_text(left_x0, mid_y + pad, anchor="nw",
                               text="Топ по постам", fill=self.colors["fg"],
                               font=("Segoe UI", 12, "bold"))
            canvas.create_text(right_x0, pad, anchor="nw",
                               text="Посты за день (21 день)", fill=self.colors["fg"],
                               font=("Segoe UI", 12, "bold"))

            top_rev = sorted(rows, key=lambda r: float(r.get("revenue", 0.0)), reverse=True)[:10]
            top_posts = sorted(rows, key=lambda r: int(r.get("posts", 0)), reverse=True)[:10]

            def draw_bars(x0, y0, x1, data, value_key, fmt):
                if not data:
                    canvas.create_text(x0, y0 + 30, anchor="nw",
                                       text="Нет данных за текущий период.", fill=self.colors["muted"])
                    return
                bar_h = 18
                gap = 8
                maxv = max(float(d.get(value_key, 0.0)) for d in data) or 1.0
                for i, d in enumerate(data):
                    yy = y0 + 35 + i * (bar_h + gap)
                    val = float(d.get(value_key, 0.0))
                    bw = int((x1 - x0 - 230) * (val / maxv))
                    canvas.create_rectangle(x0, yy, x0 + bw, yy + bar_h, outline="", fill="#2a2f3a")
                    label = str(d.get("vehicle", "?"))
                    canvas.create_text(x0 + bw + 8, yy + bar_h/2, anchor="w",
                                       text=f"{label}  —  {fmt(val)}",
                                       fill=self.colors["fg"], font=("Segoe UI", 10))

            draw_bars(left_x0, pad, left_x1, top_rev, "revenue", lambda v: f"${v:,.0f}")
            draw_bars(left_x0, mid_y + pad, left_x1, top_posts, "posts", lambda v: f"{int(v)}")

            # Daily posts line from archive.csv (raw events)
            try:
                evts = self._load_archive_events(force=False)
            except Exception:
                evts = []

            if not evts:
                canvas.create_text(right_x0, pad + 30, anchor="nw",
                                   text="Данные archive.csv отсутствуют.\nРазместите несколько постов и откройте снова.",
                                   fill=self.colors["muted"])
                return

            now = datetime.now()
            days = 21
            start = now - timedelta(days=days)
            counts = {}
            for e in evts:
                dt = e.get("dt")
                if not dt or dt < start:
                    continue
                key = dt.date().isoformat()
                counts[key] = counts.get(key, 0) + 1

            dates = [(start + timedelta(days=i)).date().isoformat() for i in range(days + 1)]
            ys = [counts.get(d, 0) for d in dates]
            maxy = max(ys) or 1

            ax0x = right_x0
            ax1x = right_x1
            ax0y = pad + 55
            ax1y = ch - pad

            canvas.create_rectangle(ax0x, ax0y, ax1x, ax1y, outline=self.colors["border"], width=1)

            pts = []
            n = len(dates)
            for i, yv in enumerate(ys):
                x = ax0x + (ax1x - ax0x) * i / max(1, (n - 1))
                y = ax1y - (ax1y - ax0y) * (yv / maxy)
                pts.extend([x, y])
            if len(pts) >= 4:
                canvas.create_line(*pts, fill=self.colors["accent"], width=2)

            canvas.create_text(ax0x + 6, ax0y + 6, anchor="nw",
                               text=f"Max/day: {maxy}", fill=self.colors["muted"], font=("Segoe UI", 9))

        except Exception as e:
            log(f"draw charts error: {e}")

    # ---------- Reset / cleanup helpers ----------
    def _on_reset_arm_changed(self):
        try:
            armed = bool(self.reset_armed_var.get()) if hasattr(self, "reset_armed_var") else False

            # cancel any existing auto-disarm timer
            if getattr(self, "_arm_after_id", None):
                try:
                    self.after_cancel(self._arm_after_id)
                except Exception:
                    pass
                self._arm_after_id = None

            if armed:
                self.reset_status_var.set("⚠ ВЗВЕДЁН: держи кнопки (авто-сброс через 10с)")
                for b in [getattr(self, "btn_clear_log_file", None), getattr(self, "btn_reset_stats", None)]:
                    if b:
                        b.configure(state="normal")
                # auto-disarm after 10s
                self._arm_after_id = self.after(10_000, lambda: self.reset_armed_var.set(False) or self._on_reset_arm_changed())
            else:
                self.reset_status_var.set("Сброс: ЗАБЛОКИРОВАН")
                for b in [getattr(self, "btn_clear_log_file", None), getattr(self, "btn_reset_stats", None)]:
                    if b:
                        b.configure(state="disabled")
        except Exception:
            pass

    def _make_hold_button(self, parent, text: str, hold_ms: int, command):
        btn = ttk.Button(parent, text=text)
        state = {"pressed": False, "after_id": None}

        def on_press(_e=None):
            if not hasattr(self, "reset_armed_var") or not bool(self.reset_armed_var.get()):
                self.reset_status_var.set("Сброс: ЗАБЛОКИРОВАН")
                return
            state["pressed"] = True
            self.reset_status_var.set(f"Удерживай… ({hold_ms/1000:.0f}с)")
            # schedule execution
            def fire():
                state["after_id"] = None
                if state["pressed"] and bool(self.reset_armed_var.get()):
                    self.reset_status_var.set("✅ Выполнено")
                    try:
                        command()
                    except Exception as ex:
                        self.reset_status_var.set("❌ Ошибка (см. лог)")
                        log(f"reset action error: {ex}")
                else:
                    self.reset_status_var.set("Отменено")
            state["after_id"] = self.after(hold_ms, fire)

        def on_release(_e=None):
            if not state["pressed"]:
                return
            state["pressed"] = False
            if state["after_id"] is not None:
                try:
                    self.after_cancel(state["after_id"])
                except Exception:
                    pass
                state["after_id"] = None
                # released too soon
                if hasattr(self, "reset_armed_var") and bool(self.reset_armed_var.get()):
                    self.reset_status_var.set("Отпущено слишком рано — отменено")

        btn.bind("<ButtonPress-1>", on_press)
        btn.bind("<ButtonRelease-1>", on_release)
        btn.configure(state="disabled")
        return btn

    def _trash_user_files(self, paths):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        trash_dir = USER_DIR / f"_TRASH_{stamp}"
        try:
            trash_dir.mkdir(parents=True, exist_ok=False)
        except Exception:
            trash_dir.mkdir(parents=True, exist_ok=True)

        for p in paths:
            try:
                if not p.exists():
                    continue
                dst = trash_dir / p.name
                if dst.exists():
                    dst = trash_dir / f"{p.stem}__DUP__{p.suffix}"
                shutil.move(str(p), str(dst))
            except Exception as e:
                log(f"trash move error: {e}")
        return trash_dir

    def _clear_log_file_action(self):
        try:
            with LOG_LOCK:
                if LOG_PATH.exists():
                    self._trash_user_files([LOG_PATH])
                # recreate empty log file
                LOG_PATH.write_text("", encoding="utf-8")
            self.clear_logs()
            self.log_view.insert("end", "[LOG CLEARED]\n")
            self.log_view.see("end")
            log("LOG FILE CLEARED (moved to _TRASH_)")
        except Exception as e:
            log(f"clear log file error: {e}")

    def _reset_stats_action(self):
        try:
            paths = []
            if STATS_PATH.exists():
                paths.append(STATS_PATH)
            if ARCHIVE_CSV_PATH.exists():
                paths.append(ARCHIVE_CSV_PATH)
            if paths:
                self._trash_user_files(paths)
            # reset UI
            self._archive_cache_mtime = None
            self._archive_cache_events = []
            self.total_posts_var.set("0")
            self.total_rev_var.set("0.00")
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            self._set_recent_text("Recent posts will appear here.\n")
            log("STATS RESET (stats.json + archive.csv moved to _TRASH_)")
        except Exception as e:
            log(f"reset stats error: {e}")


    def clear_logs(self):
        self.log_view.delete("1.0", "end")

    def _pump_logs(self):
        try:
            while True:
                line = LOG_QUEUE.get_nowait()
                self.log_view.insert("end", line + "\n")
                self.log_view.see("end")
        except Exception:
            pass
        if not getattr(self, "_closing", False):
            self.after(150, self._pump_logs)

    def _refresh_stats(self, force: bool = False):
        """
        Refresh Stats tab from archive.csv (event-level truth).
        Popularity (Pop Δ) is computed as change between current window and previous same window.
        """
        try:
            # -------- window / settings --------
            win = (self.stats_window_var.get() if hasattr(self, "stats_window_var") else "all").strip().lower()
            sort_key = (self.sort_key_var.get() if hasattr(self, "sort_key_var") else "posts").strip()
            desc = bool(self.sort_desc_var.get()) if hasattr(self, "sort_desc_var") else True
            pop_metric = (self.pop_metric_var.get() if hasattr(self, "pop_metric_var") else "posts").strip().lower()
            q = (self.stats_search_var.get() if hasattr(self, "stats_search_var") else "").strip().lower()

            window_td = None
            if win == "1h":
                window_td = timedelta(hours=1)
            elif win == "24h":
                window_td = timedelta(hours=24)
            elif win == "7d":
                window_td = timedelta(days=7)
            elif win == "30d":
                window_td = timedelta(days=30)
            elif win == "90d":
                window_td = timedelta(days=90)
            elif win == "all":
                window_td = None
            else:
                window_td = None

            # If user selected "all", keep metrics all-time but keep popularity based on last 7d (stable default)
            pop_td = window_td if window_td is not None else timedelta(days=7)

            # -------- load archive events --------
            events = self._load_archive_events(force=force)
            now = datetime.now()

            # Split events into metric window
            if window_td is None:
                metric_events = events
            else:
                start = now - window_td
                metric_events = [e for e in events if e["dt"] >= start]

            # Popularity: compare current window to previous same window
            pop_start = now - pop_td
            pop_prev_start = now - pop_td - pop_td

            cur_pop_events = [e for e in events if e["dt"] >= pop_start]
            prev_pop_events = [e for e in events if (pop_prev_start <= e["dt"] < pop_start)]

            # -------- aggregate helpers --------
            def agg(evts):
                by = {}
                for e in evts:
                    stem = e["stem"]
                    rec = by.get(stem)
                    if not rec:
                        rec = {"posts": 0, "revenue": 0.0, "last_dt": None, "last_price": 0.0}
                        by[stem] = rec
                    rec["posts"] += 1
                    rec["revenue"] += float(e["price_value"])
                    if (rec["last_dt"] is None) or (e["dt"] > rec["last_dt"]):
                        rec["last_dt"] = e["dt"]
                        rec["last_price"] = float(e["price_value"])
                return by

            metric_by = agg(metric_events)
            cur_by = agg(cur_pop_events)
            prev_by = agg(prev_pop_events)

            # Ensure Stats shows ALL vehicles from /car (even if 0 posts in window)
            try:
                cfg_live = self.cfg_provider() if hasattr(self, "cfg_provider") else {}
                car_stems = list_vehicle_stems_from_car(cfg_live)
            except Exception:
                car_stems = []
            try:
                for _s in car_stems:
                    metric_by.setdefault(_s, {"posts": 0, "revenue": 0.0, "last_dt": None, "last_price": 0.0})
                    cur_by.setdefault(_s, {"posts": 0, "revenue": 0.0, "last_dt": None, "last_price": 0.0})
                    prev_by.setdefault(_s, {"posts": 0, "revenue": 0.0, "last_dt": None, "last_price": 0.0})
            except Exception:
                pass

            # pick attempts (how many candidate clicks until a valid selection)
            pick_evts = []
            try:
                pick_evts = self._load_pick_attempt_events()
            except Exception:
                pick_evts = []
            # filter by same metric window (best-effort)
            metric_start_dt = (now - window_td) if window_td is not None else datetime.min
            pick_by = {}
            try:
                for e in pick_evts:
                    if e["dt"] < metric_start_dt:
                        continue
                    if not e.get("success"):
                        continue
                    stem = e["stem"]
                    pick_by.setdefault(stem, []).append(int(e.get("attempts", 0)))
            except Exception:
                pass
            self._pick_stats_by_vehicle = {k: {"avg": (sum(v)/len(v) if v else 0.0), "last": (v[0] if v else 0)} for k, v in pick_by.items()}

            # Totals + dashboard cards
            total_posts = sum(v["posts"] for v in metric_by.values())
            total_rev = sum(v["revenue"] for v in metric_by.values())
            self.total_posts_var.set(str(int(total_posts)))
            self.total_rev_var.set(f"{float(total_rev):.2f}")
            # Avg price across all posted vehicles
            total_avg = (total_rev / total_posts) if total_posts else 0.0
            try:
                self._stats_avg_var.set(f"{total_avg:.2f}")
            except Exception:
                pass
            # Best vehicle by posts
            try:
                if metric_by:
                    best = max(metric_by.items(), key=lambda kv: kv[1]["posts"])
                    self._stats_best_var.set(best[0][:18])
                else:
                    self._stats_best_var.set("—")
            except Exception:
                pass

            # Build per-vehicle events for details (recent)
            self._events_by_vehicle = {}
            for e in metric_events:
                self._events_by_vehicle.setdefault(e["stem"], []).append(e)
            for stem in self._events_by_vehicle:
                self._events_by_vehicle[stem].sort(key=lambda x: x["dt"], reverse=True)

            # Rows for tree
            rows = []
            for vehicle, rec in metric_by.items():
                if q and q not in vehicle.lower():
                    continue

                posts = int(rec["posts"])
                revenue = float(rec["revenue"])
                avg_price = (revenue / posts) if posts else 0.0
                # rates inside the selected window
                if window_td is not None:
                    win_hours = max(1.0, window_td.total_seconds() / 3600.0)
                else:
                    win_hours = 7.0 * 24.0  # keep all-time rates comparable
                posts_hr = float(posts) / win_hours
                rev_hr = float(revenue) / win_hours
                last_price = float(rec["last_price"])
                last_posted_at = rec["last_dt"].strftime("%Y-%m-%d %H:%M:%S") if rec["last_dt"] else ""

                # popularity delta
                cur_val = cur_by.get(vehicle, {}).get(pop_metric, 0.0)
                prev_val = prev_by.get(vehicle, {}).get(pop_metric, 0.0)
                pop_str, pop_num = format_pop_delta(cur_val, prev_val)

                rows.append({
                    "vehicle": vehicle,
                    "posts": posts,
                    "revenue": revenue,
                    "posts_hr": posts_hr,
                    "rev_hr": rev_hr,
                    "avg_price": avg_price,
                    "last_price": last_price,
                    "pop_delta_str": pop_str,
                    "pop_delta_num": pop_num,
                    "last_posted_at": last_posted_at,
                })

            # Compute Score across rows (weight slider). Score = normalized mix of Posts/hr and $/hr.
            w_posts = float(self.score_weight_var.get()) / 100.0 if hasattr(self, "score_weight_var") else 0.5
            w_rev = 1.0 - w_posts

            p_vals = [r.get("posts_hr", 0.0) for r in rows]
            r_vals = [r.get("rev_hr", 0.0) for r in rows]
            pmin, pmax = (min(p_vals), max(p_vals)) if p_vals else (0.0, 0.0)
            rmin, rmax = (min(r_vals), max(r_vals)) if r_vals else (0.0, 0.0)

            for rr in rows:
                pn = 0.0 if pmax == pmin else (rr.get("posts_hr", 0.0) - pmin) / (pmax - pmin)
                rn = 0.0 if rmax == rmin else (rr.get("rev_hr", 0.0) - rmin) / (rmax - rmin)
                score_num = (w_posts * pn + w_rev * rn) * 100.0
                rr["score_num"] = float(score_num)
                rr["score"] = f"{score_num:.0f}"
            # Sort
            def sort_key_fn(r):
                k = sort_key
                if k == "vehicle":
                    return r["vehicle"].lower()
                if k == "posts":
                    return r["posts"]
                if k == "revenue":
                    return r["revenue"]
                if k == "posts_hr":
                    return r.get("posts_hr", 0.0)
                if k == "rev_hr":
                    return r.get("rev_hr", 0.0)
                if k == "score":
                    return r.get("score_num", 0.0)
                if k == "avg_price":
                    return r["avg_price"]
                if k == "last_price":
                    return r["last_price"]
                if k == "pop_delta":
                    return r["pop_delta_num"]
                if k == "last_posted_at":
                    return r["last_posted_at"]
                return r["posts"]

            rows.sort(key=sort_key_fn, reverse=desc)

            # Paint tree (preserve selection so user can edit without it "unselecting" every refresh)
            selected = getattr(self, "_selected_vehicle_cache", "")
            try:
                sel = self.tree.selection()
                if sel:
                    vals = self.tree.item(sel[0], "values")
                    if vals:
                        selected = str(vals[0])
            except Exception:
                pass
            if selected:
                self._selected_vehicle_cache = selected

            self._suppress_select_event = True
            try:
                for iid in self.tree.get_children():
                    self.tree.delete(iid)

                _v2iid = {}
                for _row_idx, r in enumerate(rows):
                    _row_tag = "evenrow" if _row_idx % 2 == 0 else "oddrow"
                    iid = self.tree.insert(
                        "",
                        "end",
                        values=(
                            r["vehicle"],
                            r["posts"],
                            f"{r['revenue']:.2f}",
                            f"{r['posts_hr']:.3f}",
                            f"{r['rev_hr']:.3f}",
                            r["score"],
                            f"{r['avg_price']:.2f}",
                            f"{r['last_price']:.2f}",
                            r["pop_delta_str"],
                            r["last_posted_at"],
                        ),
                        tags=(_row_tag,),
                    )
                    _v2iid[str(r["vehicle"])] = iid

                if selected and selected in _v2iid:
                    iid = _v2iid[selected]
                    self.tree.selection_set(iid)
                    self.tree.focus(iid)
                    try:
                        self.tree.see(iid)
                    except Exception:
                        pass
            finally:
                self._suppress_select_event = False

            # Keep last computed rows for charts
            self._stats_rows = rows
            try:
                self._redraw_inline_charts()
            except Exception:
                pass

            self._last_pop_window_label = f"{int(pop_td.total_seconds()//3600)}h" if pop_td < timedelta(days=2) else f"{int(pop_td.days)}d"

            # Update selection details if any
            self._refresh_selected_details()

        except Exception as e:
            log(f"stats refresh error: {e}")
        if not getattr(self, "_closing", False):
            self.after(1200, self._refresh_stats)

    def on_close(self):
        self._closing = True
        self.run_event.clear()
        self.stop_event.set()
        try:
            if hasattr(self, 'tg_tracker'):
                self.tg_tracker.stop()
        except Exception:
            pass
        time.sleep(0.15)
        self.destroy()


def _fatal_messagebox(tb: str) -> None:
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Wiwang Poster crashed", tb)
        r.destroy()
    except Exception:
        pass



# ---------- Stats Editor UI ----------
def _build_editor_ui(self):
    parent = getattr(self, "tab_editor_inner", self.tab_editor)
    top = tk.Frame(parent, bg=self.colors["panel"])
    top.pack(fill="x", padx=10, pady=10)

    self.editor_vehicle_var = tk.StringVar(value="")
    tk.Label(top, text="Выбрано:", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left")
    tk.Label(top, textvariable=self.editor_vehicle_var, fg=self.colors["fg"], bg=self.colors["panel"]).pack(side="left", padx=8)

    self.editor_mode_var = tk.StringVar(value="manual")
    tk.Label(top, text="Режим:", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left", padx=(18, 4))
    self.cmb_mode = ttk.Combobox(top, textvariable=self.editor_mode_var, values=["manual", "schedule", "auto_suggest"], width=14, state="readonly")
    self.cmb_mode.pack(side="left", padx=(0, 10))
    # Auto-save schedule mode on change (so dynamic pricing actually applies without pressing Save).
    self.cmb_mode.bind("<<ComboboxSelected>>", lambda e: self._save_schedule_only(silent=True))
    Tooltip(self.cmb_mode, "Pricing mode\nmanual = use price.txt\nschedule = base×mult by MSK slots\nauto_suggest = same as schedule + shows suggestions")

    btn_smart = ttk.Button(top, text="🤖 Авто-расписание", command=self._smart_build_schedule_for_selected)
    btn_smart.pack(side="right")
    Tooltip(btn_smart, "Generate a reasonable starting schedule\nfor weekday/weekend multipliers\nusing your demand pattern (MSK slots).")

    row = tk.Frame(parent, bg=self.colors["panel"])
    row.pack(fill="x", padx=10, pady=(0, 10))
    tk.Label(row, text="Базовая цена (price.txt):", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left")
    self.entry_base_price = ttk.Entry(row, width=14)
    self.entry_base_price.pack(side="left", padx=8)
    self.entry_base_price.bind("<KeyRelease>", lambda _e=None: self._update_multiplier_previews(), add="+")
    self.entry_base_price.bind("<FocusOut>", lambda _e=None: self._update_multiplier_previews(), add="+")
    Tooltip(self.entry_base_price, "Base price stored in car/<vehicle>/price.txt\nSchedule multipliers apply on top of this value.")

    
    # Plate number (optional) - used for OCR validation if plate_ref.png is not provided
    row_plate = tk.Frame(parent, bg=self.colors["panel"])
    row_plate.pack(fill="x", padx=10, pady=(0, 10))
    tk.Label(row_plate, text="Гос. номер (опц.):", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left")
    self.entry_plate_text = ttk.Entry(row_plate, width=18)
    self.entry_plate_text.pack(side="left", padx=8)
    # prevent auto-sync from wiping what you type
    try:
        self.entry_plate_text.bind("<FocusIn>", lambda _e=None: self._set_editor_typing(True), add="+")
        self.entry_plate_text.bind("<KeyPress>", lambda _e=None: self._bump_editor_typing(), add="+")
        self.entry_plate_text.bind("<FocusOut>", lambda _e=None: self._set_editor_typing(False), add="+")
        self.entry_plate_text.bind("<KeyRelease>", lambda _e=None: self._mark_editor_dirty(), add="+")
    except Exception:
        pass
    ttk.Button(row_plate, text="📌 Захватить", command=self._capture_plate_ref_for_vehicle).pack(side="left", padx=(10,0))
    # Per-vehicle hard mode: do not post unless plate_validate passes
    self.var_plate_require = tk.BooleanVar(value=False)
    ttk.Checkbutton(row_plate, text="Требовать проверку", variable=self.var_plate_require,
                    command=lambda: self._save_schedule_only(silent=True)).pack(side="left", padx=(10,0))
    ttk.Button(row_plate, text="👁 Превью", command=self._update_plate_preview).pack(side="left", padx=(8,0))

    self.pick_attempts_var = tk.StringVar(value="pick: —")
    tk.Label(row_plate, textvariable=self.pick_attempts_var, fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left", padx=(12,0))
    Tooltip(self.entry_plate_text, "Optional: stored in schedule.json as plate_text.\nUsed for OCR validation when plate_ref.png is not available.\nIf Require validate is ON, posting will be blocked on mismatch / missing validation.")

    btn_save = ttk.Button(row, text="💾 Сохранить цену/описание/расписание", command=self._save_editor_changes)
    btn_save.pack(side="left", padx=(12, 0))


    # Plate sanity preview (live crop vs saved plate_ref.png)
    prev = tk.Frame(parent, bg=self.colors["panel"])
    prev.pack(fill="x", padx=10, pady=(0, 10))
    tk.Label(prev, text="Номер (экран):", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left")
    self.lbl_plate_live = tk.Label(prev, text="(no live)", fg=self.colors["muted"], bg=self.colors["panel"], width=24, anchor="w")
    self.lbl_plate_live.pack(side="left", padx=(8, 12))
    tk.Label(prev, text="Сохр. шаблон:", fg=self.colors["muted"], bg=self.colors["panel"]).pack(side="left")
    self.lbl_plate_ref = tk.Label(prev, text="(no ref)", fg=self.colors["muted"], bg=self.colors["panel"], width=24, anchor="w")
    self.lbl_plate_ref.pack(side="left", padx=(8, 12))
    self.plate_preview_status_var = tk.StringVar(value="Plate preview: —")
    tk.Label(parent, textvariable=self.plate_preview_status_var, fg=self.colors["muted"], bg=self.colors["panel"]).pack(anchor="w", padx=14, pady=(0, 8))

    grid = tk.Frame(parent, bg=self.colors["panel"])
    grid.pack(fill="x", padx=10, pady=(0, 10))

    tk.Label(grid, text="Множители буднего дня", fg=self.colors["fg"], bg=self.colors["panel"]).grid(row=0, column=0, columnspan=4, sticky="w")
    tk.Label(grid, text="Множители выходного дня", fg=self.colors["fg"], bg=self.colors["panel"]).grid(row=0, column=4, columnspan=4, sticky="w", padx=(24, 0))

    slots = [("low", "02–12"), ("mid", "12–18"), ("high", "18–22"), ("gold", "22–02")]
    self.mult_entries = {"weekday": {}, "weekend": {}}
    self.mult_preview_labels = {"weekday": {}, "weekend": {}}
    # Preview columns: show resulting price next to multiplier for convenience
    tk.Label(grid, text="→ price", fg=self.colors["muted"], bg=self.colors["panel"]).grid(row=0, column=2, sticky="w", padx=(6, 0))
    tk.Label(grid, text="→ price", fg=self.colors["muted"], bg=self.colors["panel"]).grid(row=0, column=6, sticky="w", padx=(6, 0))
    for i, (slot, label) in enumerate(slots, start=1):
        tk.Label(grid, text=f"{slot} ({label})", fg=self.colors["muted"], bg=self.colors["panel"]).grid(row=i, column=0, sticky="w", pady=4)
        e1 = ttk.Entry(grid, width=8)
        e1.grid(row=i, column=1, sticky="w")
        self.mult_entries["weekday"][slot] = e1
        # live preview + live update
        p1 = tk.Label(grid, text="—", fg=self.colors["muted"], bg=self.colors["panel"])
        p1.grid(row=i, column=2, sticky="w", padx=(6, 0))
        self.mult_preview_labels["weekday"][slot] = p1
        e1.bind("<KeyRelease>", lambda _e=None: self._update_multiplier_previews(), add="+")
        e1.bind("<FocusOut>", lambda _e=None: self._update_multiplier_previews(), add="+")
        e1.bind("<Return>", lambda _e=None: self._save_schedule_only(silent=True), add="+")
        e1.bind("<FocusOut>", lambda _e=None: self._save_schedule_only(silent=True), add="+")

        tk.Label(grid, text=f"{slot} ({label})", fg=self.colors["muted"], bg=self.colors["panel"]).grid(row=i, column=4, sticky="w", padx=(24, 0), pady=4)
        e2 = ttk.Entry(grid, width=8)
        e2.grid(row=i, column=5, sticky="w")
        self.mult_entries["weekend"][slot] = e2
        p2 = tk.Label(grid, text="—", fg=self.colors["muted"], bg=self.colors["panel"])
        p2.grid(row=i, column=6, sticky="w", padx=(6, 0))
        self.mult_preview_labels["weekend"][slot] = p2
        e2.bind("<KeyRelease>", lambda _e=None: self._update_multiplier_previews(), add="+")
        e2.bind("<FocusOut>", lambda _e=None: self._update_multiplier_previews(), add="+")
        e2.bind("<Return>", lambda _e=None: self._save_schedule_only(silent=True), add="+")
        e2.bind("<FocusOut>", lambda _e=None: self._save_schedule_only(silent=True), add="+")

    self.editor_desc = scrolledtext.ScrolledText(
        parent,
        height=9,
        bg=self.colors["panel2"],
        fg=self.colors["fg"],
        insertbackground=self.colors["fg"],
        relief="flat",
    )
    self.editor_desc.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    Tooltip(self.editor_desc, "Description stored in car/<vehicle>/description.txt\nThis is what the bot types into the form.")

    tk.Label(
        parent,
        text="Подсказка: при сохранении создаётся резервная копия в папке _TRASH_... (ничто не удаляется).",
        fg=self.colors["muted"],
        bg=self.colors["panel"],
    ).pack(anchor="w", padx=10, pady=(0, 10))

# ---------- Pricing AI helpers ----------
def _normalize_price_input(raw: str) -> str:
    raw = str(raw or "").strip().replace(" ", "")
    if not raw:
        raise ValueError("Пустая цена")
    cleaned = raw.replace(",", ".")
    value = float(cleaned)
    if value <= 0:
        raise ValueError("Цена должна быть > 0")
    return f"{value:.0f}"


def _safe_trash_dir(base_folder: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash = base_folder.parent / f"_TRASH_{stamp}"
    trash.mkdir(exist_ok=True)
    return trash


def _append_edit_log(stem: str, action: str, detail: str):
    try:
        headers = ["timestamp", "stem", "action", "detail"]
        exists = EDIT_LOG_CSV_PATH.exists()
        with open(EDIT_LOG_CSV_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            w.writerow({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stem": stem,
                "action": action,
                "detail": detail,
            })
    except Exception:
        pass


# ---------- Pricing AI (A) UI ----------
def _build_pricing_ai_ui(self):
    colors = self.colors
    panel = colors["panel"]
    panel2 = colors.get("panel2", "#0f3460")
    fg = colors["fg"]
    muted = colors.get("muted", "#7a7a8e")
    accent = colors["accent"]
    warn = colors.get("warning", "#c4903e")
    succ = colors.get("success", "#4caf7c")
    danger = colors.get("danger", "#c0544e")
    border = colors.get("border", "#2a2a4a")

    self._ai_nb = ttk.Notebook(self.tab_ai)
    self._ai_nb.pack(fill="both", expand=True, padx=0, pady=0)
    tab_manual = tk.Frame(self._ai_nb, bg=panel)
    tab_sug = tk.Frame(self._ai_nb, bg=panel)
    self._ai_nb.add(tab_manual, text="💰 Ручные цены")
    self._ai_nb.add(tab_sug, text="🤖 AI предложения")

    boost_bar = tk.Frame(tab_manual, bg=panel, highlightthickness=1, highlightbackground=border)
    boost_bar.pack(fill="x", padx=10, pady=(10, 4))
    tk.Frame(boost_bar, bg=warn, width=4).pack(side="left", fill="y")
    tk.Label(boost_bar, text="⚡ Event Boost (глобальный × для всех машин):", fg=warn, bg=panel, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 10))
    tk.Label(boost_bar, text="Множитель:", fg=muted, bg=panel).pack(side="left")
    self._boost_var = tk.DoubleVar(value=1.0)
    self._boost_spin = tk.Spinbox(boost_bar, from_=0.5, to=5.0, increment=0.05, textvariable=self._boost_var, width=7, bg=panel2, fg=fg, relief="flat", buttonbackground=panel2, command=self._on_boost_change)
    self._boost_spin.pack(side="left", padx=(4, 10))
    self._boost_spin.bind("<KeyRelease>", lambda _e=None: self._on_boost_change())
    self._boost_preview_var = tk.StringVar(value="—")
    tk.Label(boost_bar, text="Preview:", fg=muted, bg=panel).pack(side="left", padx=(4, 4))
    tk.Label(boost_bar, textvariable=self._boost_preview_var, fg=warn, bg=panel, font=("Segoe UI", 9, "bold"), width=22, anchor="w").pack(side="left")
    tk.Button(boost_bar, text="↺ Сброс", bg=panel2, fg=muted, activebackground=border, activeforeground=fg, relief="flat", bd=0, padx=8, pady=3, font=("Segoe UI", 9), command=self._reset_event_boost).pack(side="right", padx=(0, 4))
    tk.Button(boost_bar, text="✅ Применить Event Boost", bg=warn, fg=colors["bg"], activebackground="#a07030", activeforeground=colors["bg"], relief="flat", bd=0, padx=10, pady=3, font=("Segoe UI", 9, "bold"), command=self._apply_event_boost).pack(side="right", padx=(0, 4))

    toolbar = tk.Frame(tab_manual, bg=panel)
    toolbar.pack(fill="x", padx=10, pady=(4, 4))
    ttk.Button(toolbar, text="🔄 Обновить", command=self._refresh_manual_prices).pack(side="left")
    tk.Label(toolbar, text="Поиск:", fg=muted, bg=panel).pack(side="left", padx=(12, 4))
    self._manual_search_var = tk.StringVar()
    ent = ttk.Entry(toolbar, textvariable=self._manual_search_var, width=20)
    ent.pack(side="left")
    ent.bind("<KeyRelease>", lambda _e=None: self._refresh_manual_prices())

    tbl_frame = tk.Frame(tab_manual, bg=panel)
    tbl_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
    mcols = ("vehicle","base_price","boost_preview","mode","weekday_low","weekday_gold","weekend_low","weekend_gold")
    self._manual_tree = ttk.Treeview(tbl_frame, columns=mcols, show="headings", height=12, selectmode="browse")
    mvsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self._manual_tree.yview)
    mhsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self._manual_tree.xview)
    self._manual_tree.configure(yscrollcommand=mvsb.set, xscrollcommand=mhsb.set)
    self._manual_tree.grid(row=0, column=0, sticky="nsew")
    mvsb.grid(row=0, column=1, sticky="ns")
    mhsb.grid(row=1, column=0, sticky="ew")
    tbl_frame.rowconfigure(0, weight=1)
    tbl_frame.columnconfigure(0, weight=1)
    for col, title, width, anchor in [("vehicle","Машина",200,"w"),("base_price","Базовая цена",110,"e"),("boost_preview","⚡ Preview цена",120,"e"),("mode","Режим",80,"center"),("weekday_low","Будни low×",80,"center"),("weekday_gold","Будни gold×",80,"center"),("weekend_low","Выход. low×",80,"center"),("weekend_gold","Выход. gold×",80,"center")]:
        self._manual_tree.heading(col, text=title, command=lambda c=col: self._sort_manual_tree(c))
        self._manual_tree.column(col, width=width, anchor=anchor)
    self._manual_tree.tag_configure("boosted", background="#2a2208", foreground=warn)
    self._manual_tree.tag_configure("oddrow", background=panel2)
    self._manual_tree.tag_configure("evenrow", background=panel)
    self._manual_tree.bind("<<TreeviewSelect>>", self._on_manual_price_select)
    self._manual_tree.bind("<Double-1>", lambda _e=None: self._edit_manual_price_inline())

    edit_row = tk.Frame(tab_manual, bg=panel, highlightthickness=1, highlightbackground=border)
    edit_row.pack(fill="x", padx=10, pady=(0, 4))
    tk.Label(edit_row, text="✏ Быстрое изменение:", fg=muted, bg=panel, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 8))
    tk.Label(edit_row, text="Машина:", fg=muted, bg=panel).pack(side="left")
    self._manual_sel_name_var = tk.StringVar(value="—")
    tk.Label(edit_row, textvariable=self._manual_sel_name_var, fg=fg, bg=panel, width=20, anchor="w", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 10))
    tk.Label(edit_row, text="Новая цена:", fg=muted, bg=panel).pack(side="left")
    self._manual_new_price_var = tk.StringVar()
    self._manual_price_entry = ttk.Entry(edit_row, textvariable=self._manual_new_price_var, width=12)
    self._manual_price_entry.pack(side="left", padx=(4, 6))
    self._manual_price_entry.bind("<Return>", lambda _e=None: self._save_manual_price_quick())
    tk.Button(edit_row, text="💾 Сохранить", bg=accent, fg=colors["bg"], activebackground=colors.get("accent2", "#5b8a72"), relief="flat", bd=0, padx=10, pady=3, font=("Segoe UI", 9, "bold"), command=self._save_manual_price_quick).pack(side="left", padx=(0, 8))
    self._manual_save_status_var = tk.StringVar(value="")
    tk.Label(edit_row, textvariable=self._manual_save_status_var, fg=succ, bg=panel, font=("Segoe UI", 9)).pack(side="left")
    self._manual_status_var = tk.StringVar(value="Загрузка...")
    tk.Label(tab_manual, textvariable=self._manual_status_var, fg=muted, bg=panel, font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=(0, 4))

    ai_top = tk.Frame(tab_sug, bg=panel)
    ai_top.pack(fill="x", padx=10, pady=10)
    tk.Label(ai_top, text="Режим A: предложения по archive.csv. Работает только для машин в режиме schedule/auto_suggest.", fg=muted, bg=panel).pack(side="left")
    ttk.Button(ai_top, text="🔄 Обновить предложения", command=self._refresh_suggestions).pack(side="right", padx=(8, 0))
    ttk.Button(ai_top, text="✅ Применить все", command=self._apply_all_suggestions).pack(side="right", padx=(8, 0))
    sug_frame = tk.Frame(tab_sug, bg=panel)
    sug_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cols = ("vehicle","daytype","slot","cur","suggest","delta","why")
    self.sug_tree = ttk.Treeview(sug_frame, columns=cols, show="headings", height=10)
    sug_vsb = ttk.Scrollbar(sug_frame, orient="vertical", command=self.sug_tree.yview)
    self.sug_tree.configure(yscrollcommand=sug_vsb.set)
    self.sug_tree.pack(side="left", fill="both", expand=True)
    sug_vsb.pack(side="left", fill="y")
    for c, title, w in [("vehicle","Машина",200),("daytype","День",80),("slot","Слот",70),("cur","Тек.×",70),("suggest","Пред.×",70),("delta","Δ%",60),("why","Причина",380)]:
        self.sug_tree.heading(c, text=title)
        self.sug_tree.column(c, width=w, anchor="w")
    self.sug_tree.tag_configure("up", foreground=succ)
    self.sug_tree.tag_configure("down", foreground=danger)
    self.sug_tree.bind("<Double-1>", lambda _e=None: self._apply_selected_suggestion())
    ai_bottom = tk.Frame(tab_sug, bg=panel)
    ai_bottom.pack(fill="x", padx=10, pady=(0, 8))
    ttk.Button(ai_bottom, text="✅ Применить выбранные", command=self._apply_selected_suggestion).pack(side="left")
    ttk.Button(ai_bottom, text="📤 Экспорт CSV", command=self._export_suggestions_csv).pack(side="left", padx=(10, 0))
    self._sug_count_var = tk.StringVar(value="")
    tk.Label(ai_bottom, textvariable=self._sug_count_var, fg=muted, bg=panel, font=("Segoe UI", 9)).pack(side="left", padx=(16, 0))
    try:
        self.tab_ai.after(200, self._refresh_manual_prices)
    except Exception:
        pass


def _get_all_vehicles(self) -> list:
    veh = []
    try:
        for iid in self.tree.get_children():
            try:
                veh.append(self.tree.item(iid, "values")[0])
            except Exception:
                pass
    except Exception:
        pass
    return veh


def _refresh_manual_prices(self):
    if not hasattr(self, "_manual_tree"):
        return
    query = ""
    try:
        query = self._manual_search_var.get().strip().lower()
    except Exception:
        pass
    boost = 1.0
    try:
        boost = float(self._boost_var.get())
        if boost <= 0:
            boost = 1.0
    except Exception:
        boost = 1.0
    vehicles = self._get_all_vehicles()
    rows = []
    for stem in vehicles:
        if query and query not in stem.lower():
            continue
        try:
            folder = item_folder(self.cfg_provider(), stem)
            if not folder.exists():
                continue
            base_raw = (read_text(folder, "price") or "").strip()
            try:
                base_f = float(base_raw)
            except Exception:
                base_f = None
            sched = load_schedule(folder)
            mode = str(sched.get("mode", "manual"))
            wd = sched.get("weekday", {})
            we = sched.get("weekend", {})
            rows.append({"stem": stem, "base_raw": base_raw, "base_f": base_f, "mode": mode, "wd_low": wd.get("low", 1.0), "wd_gold": wd.get("gold", 1.0), "we_low": we.get("low", 1.0), "we_gold": we.get("gold", 1.0)})
        except Exception:
            pass
    self._manual_tree.delete(*self._manual_tree.get_children())
    boosted_count = 0
    for idx, r in enumerate(rows):
        base_f = r["base_f"]
        if base_f is not None and boost != 1.0:
            preview = f"{base_f * boost:,.0f}"
            boosted_count += 1
            tag = "boosted"
        elif base_f is not None:
            preview = f"{base_f:,.0f}"
            tag = "oddrow" if idx % 2 else "evenrow"
        else:
            preview = "—"
            tag = "oddrow" if idx % 2 else "evenrow"
        self._manual_tree.insert("", "end", iid=r["stem"], values=(r["stem"], r["base_raw"] or "—", preview, r["mode"], f"{float(r['wd_low']):.2f}", f"{float(r['wd_gold']):.2f}", f"{float(r['we_low']):.2f}", f"{float(r['we_gold']):.2f}"), tags=(tag,))
    try:
        self._boost_preview_var.set(f"×{boost:.2f} применится к {boosted_count} машинам" if boost != 1.0 else "×1.00 — изменений нет")
        status = f"Машин: {len(rows)}"
        if query:
            status += f"  (фильтр: «{query}»)"
        if boost != 1.0:
            status += f"  |  ⚡ Boost ×{boost:.2f} активен"
        self._manual_status_var.set(status)
    except Exception:
        pass


def _sort_manual_tree(self, col: str):
    if not hasattr(self, "_manual_tree"):
        return
    if not hasattr(self, "_manual_sort_state"):
        self._manual_sort_state = {}
    asc = not self._manual_sort_state.get(col, False)
    self._manual_sort_state[col] = asc
    items = [(self._manual_tree.set(iid, col), iid) for iid in self._manual_tree.get_children()]
    try:
        items.sort(key=lambda x: float(x[0].replace(",", "").replace("—", "0")), reverse=not asc)
    except Exception:
        items.sort(key=lambda x: x[0].lower(), reverse=not asc)
    for idx, (_, iid) in enumerate(items):
        tag = "boosted" if "boosted" in self._manual_tree.item(iid, "tags") else ("oddrow" if idx % 2 else "evenrow")
        self._manual_tree.move(iid, "", idx)
        self._manual_tree.item(iid, tags=(tag,))


def _on_manual_price_select(self, _event=None):
    if not hasattr(self, "_manual_tree"):
        return
    sel = self._manual_tree.selection()
    if not sel:
        return
    vals = self._manual_tree.item(sel[0], "values")
    self._manual_sel_name_var.set(vals[0])
    self._manual_new_price_var.set(vals[1] if vals[1] != "—" else "")
    try:
        self._manual_save_status_var.set("")
    except Exception:
        pass


def _edit_manual_price_inline(self):
    if not hasattr(self, "_manual_tree"):
        return
    self._on_manual_price_select()
    try:
        self._manual_price_entry.focus_set()
        self._manual_price_entry.select_range(0, "end")
    except Exception:
        pass


def _save_manual_price_quick(self):
    if not hasattr(self, "_manual_tree"):
        return
    stem = ""
    try:
        stem = self._manual_sel_name_var.get().strip()
    except Exception:
        pass
    if not stem or stem == "—":
        messagebox.showinfo("Выбор машины", "Выбери машину в таблице.")
        return
    raw = ""
    try:
        raw = self._manual_new_price_var.get().strip()
    except Exception:
        pass
    if not raw:
        messagebox.showwarning("Ошибка", "Введи новую цену.")
        return
    try:
        normalized = _normalize_price_input(raw)
    except Exception as e:
        messagebox.showerror("Неверная цена", str(e))
        return
    try:
        folder = item_folder(self.cfg_provider(), stem)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / "price.txt"
        try:
            if p.exists():
                trash = _safe_trash_dir(folder)
                shutil.copy2(p, trash / f"{stem}__price.txt")
        except Exception:
            pass
        p.write_text(normalized, encoding="utf-8")
        _append_edit_log(stem, "manual_price_quick", f"price={normalized}")
        log(f"MANUAL PRICE: {stem} -> {normalized}")
        try:
            self._manual_new_price_var.set(normalized)
            self._manual_save_status_var.set(f"✅ {stem} = {normalized}")
            # Auto-clear status after 3 seconds
            try:
                self.tab_ai.after(3000, lambda: self._manual_save_status_var.set(""))
            except Exception:
                pass
        except Exception:
            pass
        self._refresh_manual_prices()
        try:
            if self._selected_vehicle() == stem:
                self._load_editor_from_files()
        except Exception:
            pass
    except Exception as e:
        messagebox.showerror("Ошибка сохранения", str(e))


def _on_boost_change(self, _event=None):
    try:
        self._refresh_manual_prices()
    except Exception:
        pass


def _apply_event_boost(self):
    boost = 1.0
    try:
        boost = float(self._boost_var.get())
    except Exception:
        messagebox.showerror("Ошибка", "Неверный множитель.")
        return
    if boost <= 0:
        messagebox.showerror("Ошибка", "Множитель должен быть больше 0.")
        return
    if boost == 1.0:
        messagebox.showinfo("Event Boost", "Множитель ×1.0 — изменений нет.")
        return
    vehicles = self._get_all_vehicles()
    if not vehicles:
        messagebox.showinfo("Event Boost", "Нет машин в списке.")
        return
    count = 0
    skipped = 0
    errors = []
    for stem in vehicles:
        try:
            folder = item_folder(self.cfg_provider(), stem)
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / "price.txt"
            base_raw = (p.read_text(encoding="utf-8").strip() if p.exists() else "")
            if not base_raw:
                skipped += 1
                continue
            try:
                base_norm = _normalize_price_input(base_raw)
                base_f = float(base_norm)
            except Exception:
                skipped += 1
                continue
            new_raw = f"{base_f * boost:.0f}"
            try:
                if p.exists():
                    trash = _safe_trash_dir(folder)
                    shutil.copy2(p, trash / f"{stem}__price_boost.txt")
            except Exception:
                pass
            p.write_text(new_raw, encoding="utf-8")
            _append_edit_log(stem, "event_boost", f"boost={boost:.2f} old={base_norm} new={new_raw}")
            count += 1
        except Exception as e:
            errors.append(f"{stem}: {e}")
    log(f"EVENT BOOST: ×{boost:.2f} applied to {count} vehicles, skipped={skipped}")
    msg = f"Event Boost ×{boost:.2f} применён к {count} машинам."
    if skipped:
        msg += f"\nПропущено: {skipped} (пустая/битая цена)"
    if errors:
        msg += f"\n\nОшибки ({len(errors)}):\n" + "\n".join(errors[:5])
    messagebox.showinfo("Event Boost применён", msg)
    self._reset_event_boost()
    self._refresh_manual_prices()
    try:
        self._refresh_stats(force=False)
    except Exception:
        pass


def _reset_event_boost(self):
    try:
        self._boost_var.set(1.0)
        self._on_boost_change()
    except Exception:
        pass


# ---------- Editor logic ----------
def _selected_vehicle(self) -> Optional[str]:
    try:
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        return self.tree.item(iid, "values")[0]
    except Exception:
        return None

def _selected_folder(self) -> Optional[Path]:
    v = self._selected_vehicle()
    if not v:
        return None
    try:
        return item_folder(self.cfg_provider(), v)
    except Exception:
        return None

def _smart_build_schedule_for_selected(self):
    folder = self._selected_folder()
    if not folder:
        messagebox.showinfo("Select", "Select a vehicle first.")
        return
    sched = load_schedule(folder)
    sched["weekday"] = {"low": 0.95, "mid": 1.00, "high": 1.08, "gold": 1.12}
    sched["weekend"] = {"low": 1.02, "mid": 1.08, "high": 1.13, "gold": 1.20}
    save_schedule(folder, sched)
    self._load_editor_from_files()
    log(f"EDITOR: smart schedule built for {folder.name}")

def _load_editor_from_files(self):
    folder = self._selected_folder()
    if not folder:
        return
    self.editor_vehicle_var.set(folder.name)

    base_raw = (read_text(folder, "price") or "").strip()
    self.entry_base_price.delete(0, "end")
    self.entry_base_price.insert(0, base_raw)

    desc = read_text(folder, "description") or ""
    self.editor_desc.delete("1.0", "end")
    self.editor_desc.insert("1.0", desc)

    sched = load_schedule(folder)
    try:
        # don't wipe what user is typing right now
        w = None
        try:
            w = self.focus_get()
        except Exception:
            w = None
        focused = (w == self.entry_plate_text) or (str(w) == str(self.entry_plate_text))
        if not getattr(self, '_editor_is_typing', False) and not focused:
            self.entry_plate_text.delete(0, "end")
            self.entry_plate_text.insert(0, str(sched.get("plate_text","")).strip())
    except Exception:
        pass
    try:
        if hasattr(self, "var_plate_require"):
            self.var_plate_require.set(bool(sched.get("plate_require", False)))
    except Exception:
        pass
    try:
        self._update_plate_preview()
    except Exception:
        pass
    self.editor_mode_var.set(str(sched.get("mode", "manual")))
    # show pick attempts for this vehicle (if available)
    try:
        if hasattr(self, "pick_attempts_var"):
            vname = folder.name
            it = getattr(self, "_pick_stats_by_vehicle", {}).get(vname)
            if it:
                self.pick_attempts_var.set(f"pick: avg {float(it.get('avg',0.0)):.2f}  last {int(it.get('last',0))}")
            else:
                self.pick_attempts_var.set("pick: —")
    except Exception:
        pass
    for daytype in ("weekday", "weekend"):
        for slot in ("low", "mid", "high", "gold"):
            val = sched.get(daytype, {}).get(slot, 1.0)
            e = self.mult_entries[daytype][slot]
            e.delete(0, "end")
            e.insert(0, f"{float(val):.2f}")

def _save_editor_changes(self):
    folder = self._selected_folder()
    if not folder:
        messagebox.showinfo("Select", "Select a vehicle first.")
        return

    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash = folder.parent / f"_TRASH_{stamp}"
        trash.mkdir(exist_ok=True)
        for fn in ("price.txt", "description.txt", "schedule.json"):
            p = folder / fn
            if p.exists():
                shutil.copy2(p, trash / f"{folder.name}__{fn}")
    except Exception:
        pass

    base_raw = self.entry_base_price.get().strip()
    try:
        (folder / "price.txt").write_text(base_raw, encoding="utf-8")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to write price.txt: {e}")
        return

    desc = self.editor_desc.get("1.0", "end").rstrip("\n")
    try:
        (folder / "description.txt").write_text(desc, encoding="utf-8")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to write description.txt: {e}")
        return

    sched = load_schedule(folder)
    sched["mode"] = self.editor_mode_var.get().strip()
    for daytype in ("weekday", "weekend"):
        for slot in ("low", "mid", "high", "gold"):
            raw = self.mult_entries[daytype][slot].get().strip()
            try:
                sched[daytype][slot] = float(raw)
            except Exception:
                pass
    # plate text
    try:
        sched["plate_text"] = str(self.entry_plate_text.get()).strip()
    except Exception:
        pass
    # plate require
    try:
        if hasattr(self, "var_plate_require"):
            sched["plate_require"] = bool(self.var_plate_require.get())
    except Exception:
        pass
    save_schedule(folder, sched)

    try:
        headers = ["timestamp", "stem", "action", "detail"]
        exists = EDIT_LOG_CSV_PATH.exists()
        with open(EDIT_LOG_CSV_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            w.writerow({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stem": folder.name,
                "action": "save_editor",
                "detail": f"mode={sched.get('mode')} base={base_raw}",
            })
    except Exception:
        pass

    log(f"EDITOR: saved {folder.name}")
    messagebox.showinfo("Saved", "Saved. Backup created in _TRASH_...")

    self._refresh_stats(force=False)

# ---------- Suggestions logic ----------

def _save_schedule_only(self, silent: bool = False):
    """Save only schedule.json (mode + multipliers) for currently selected vehicle.
    This prevents 'I changed mode but prices didn't change' because user forgot to press full Save.
    """
    folder = self._selected_folder() if hasattr(self, "_selected_folder") else None
    if not folder:
        if not silent:
            try:
                messagebox.showinfo("Select", "Select a vehicle first.")
            except Exception:
                pass
        return

    try:
        sched = load_schedule(folder)
        sched["mode"] = str(getattr(self, "editor_mode_var").get()).strip() if hasattr(self, "editor_mode_var") else sched.get("mode", "manual")
        # multipliers
        if hasattr(self, "mult_entries"):
            for daytype in ("weekday", "weekend"):
                for slot in ("low", "mid", "high", "gold"):
                    try:
                        raw = self.mult_entries[daytype][slot].get().strip()
                        sched.setdefault(daytype, {})[slot] = float(raw)
                    except Exception:
                        pass
        save_schedule(folder, sched)
        log(f"EDITOR: schedule autosaved {folder.name} mode={sched.get('mode')}")
    except Exception as e:
        log(f"EDITOR: schedule autosave failed: {e}")
        if not silent:
            try:
                messagebox.showerror("Error", f"Failed to save schedule.json: {e}")
            except Exception:
                pass

def _compute_vehicle_slot_metrics(self, stem: str, window_td: Optional[timedelta]) -> Dict[str, Any]:
    events = self._load_archive_events(force=False)
    now_local = datetime.now()
    if window_td is not None:
        start = now_local - window_td
        events = [e for e in events if e["dt"] >= start]

    dates = {"weekday": set(), "weekend": set()}
    buckets = {
        "weekday": {s: {"posts": 0, "revenue": 0.0} for s in SLOT_HOURS},
        "weekend": {s: {"posts": 0, "revenue": 0.0} for s in SLOT_HOURS},
    }
    for e in events:
        if e["stem"] != stem:
            continue
        dt_msk = to_msk(e["dt"])
        day = msk_daytype(dt_msk)
        slot = msk_slot(dt_msk)
        dates[day].add(dt_msk.date())
        buckets[day][slot]["posts"] += 1
        buckets[day][slot]["revenue"] += float(e["price_value"])

    out = {"weekday": {}, "weekend": {}}
    for day in ("weekday", "weekend"):
        days = max(1, len(dates[day]))
        for slot, h in SLOT_HOURS.items():
            posts = buckets[day][slot]["posts"]
            rev = buckets[day][slot]["revenue"]
            denom = float(h) * float(days)
            out[day][slot] = {
                "days": days,
                "posts": posts,
                "revenue": rev,
                "posts_hr": posts / denom if denom else 0.0,
                "rev_hr": rev / denom if denom else 0.0,
            }
    return out

def _refresh_suggestions(self):
    win = (self.stats_window_var.get() if hasattr(self, "stats_window_var") else "7d").strip().lower()
    window_td = None
    if win == "24h":
        window_td = timedelta(hours=24)
    elif win == "7d":
        window_td = timedelta(days=7)
    elif win == "30d":
        window_td = timedelta(days=30)
    elif win == "90d":
        window_td = timedelta(days=90)

    weight = float(self.score_weight_var.get()) / 100.0 if hasattr(self, "score_weight_var") else 0.5
    rows = []

    vehicles = []
    for iid in self.tree.get_children():
        try:
            vehicles.append(self.tree.item(iid, "values")[0])
        except Exception:
            pass

    for stem in vehicles:
        folder = item_folder(self.cfg_provider(), stem)
        sched = load_schedule(folder)
        if str(sched.get("mode", "manual")).lower() not in ("schedule", "auto_suggest"):
            continue

        limits = sched.get("limits", {})
        step = float(limits.get("step", 0.03))
        mn = float(limits.get("min", 0.70))
        mx = float(limits.get("max", 1.40))
        min_events = int(limits.get("min_events", 3))
        thr = float(limits.get("threshold", 0.12))

        metrics = self._compute_vehicle_slot_metrics(stem, window_td)

        for daytype in ("weekday", "weekend"):
            slot_scores = {}
            for slot in ("low", "mid", "high", "gold"):
                m = metrics[daytype][slot]
                slot_scores[slot] = weight * m["posts_hr"] + (1 - weight) * (m["rev_hr"] / 100.0)

            avg_score = sum(slot_scores.values()) / 4.0 if slot_scores else 0.0
            if avg_score <= 0:
                continue

            for slot in ("low", "mid", "high", "gold"):
                m = metrics[daytype][slot]
                posts = m["posts"]
                if posts < min_events:
                    continue

                cur_mult = float(sched.get(daytype, {}).get(slot, 1.0))
                s = slot_scores[slot]
                ratio = (s - avg_score) / avg_score

                if ratio > thr:
                    sug = min(mx, cur_mult + step)
                    delta = (sug - cur_mult) / cur_mult * 100.0 if cur_mult else 0.0
                    why = f"{daytype}/{slot}: score выше среднего на {ratio*100:.0f}% (posts={posts})"
                elif ratio < -thr:
                    sug = max(mn, cur_mult - step)
                    delta = (sug - cur_mult) / cur_mult * 100.0 if cur_mult else 0.0
                    why = f"{daytype}/{slot}: score ниже среднего на {abs(ratio)*100:.0f}% (posts={posts})"
                else:
                    continue

                rows.append({
                    "vehicle": stem,
                    "daytype": daytype,
                    "slot": slot,
                    "cur": cur_mult,
                    "suggest": sug,
                    "delta": delta,
                    "why": why,
                })

    for iid in getattr(self, "sug_tree", ttk.Treeview()).get_children():
        try:
            self.sug_tree.delete(iid)
        except Exception:
            pass
    for r in rows:
        tag = "up" if r["delta"] > 0 else "down"
        self.sug_tree.insert("", "end", values=(
            r["vehicle"],
            r["daytype"],
            r["slot"],
            f"{r['cur']:.2f}",
            f"{r['suggest']:.2f}",
            f"{r['delta']:+.0f}%",
            r["why"],
        ), tags=(tag,))
    try:
        if rows:
            self._sug_count_var.set(f"Предложений: {len(rows)}")
        else:
            self._sug_count_var.set("Нет предложений — убедитесь что режим schedule/auto_suggest включён у машин")
    except Exception:
        pass

    try:
        headers = ["timestamp", "vehicle", "daytype", "slot", "cur", "suggest", "delta", "why"]
        exists = PRICE_SUGGESTIONS_CSV_PATH.exists()
        with open(PRICE_SUGGESTIONS_CSV_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), **r})
    except Exception:
        pass

def _apply_selected_suggestion(self):
    sel = self.sug_tree.selection()
    if not sel:
        messagebox.showinfo("AI предложения", "Выбери строку в таблице предложений.")
        return
    try:
        vals = self.sug_tree.item(sel[0], "values")
        stem, daytype, slot, sug = vals[0], vals[1], vals[2], float(vals[4])
        self._apply_one_suggestion(stem, daytype, slot, sug)
        try:
            self._refresh_suggestions()
        except Exception:
            pass
    except Exception as e:
        messagebox.showerror("Ошибка применения", str(e))

def _apply_all_suggestions(self):
    # Tree can refresh while applying; protect against "Item not found"
    iids = list(self.sug_tree.get_children())
    count = 0
    errors = []
    for iid in iids:
        try:
            vals = self.sug_tree.item(iid, "values")
        except Exception:
            continue
        try:
            stem, daytype, slot, sug = vals[0], vals[1], vals[2], float(vals[4])
        except Exception:
            continue
        try:
            self._apply_one_suggestion(stem, daytype, slot, sug)
            count += 1
        except Exception as e:
            errors.append(f"{vals[0] if vals else '?'}: {e}")
    msg = f"Применено предложений: {count}"
    if errors:
        msg += f"\nОшибки ({len(errors)}):\n" + "\n".join(errors[:5])
    messagebox.showinfo("AI предложения применены", msg)
    try:
        self._refresh_suggestions()
    except Exception:
        pass

def _apply_one_suggestion(self, stem: str, daytype: str, slot: str, sug: float):
    folder = item_folder(self.cfg_provider(), stem)
    folder.mkdir(parents=True, exist_ok=True)
    sched = load_schedule(folder)
    try:
        p = folder / "schedule.json"
        if p.exists():
            trash = _safe_trash_dir(folder)
            shutil.copy2(p, trash / f"{stem}__schedule.json")
    except Exception:
        pass

    cur = float(sched.get(daytype, {}).get(slot, 1.0))
    safe_sug = max(0.01, float(sug))
    sched.setdefault(daytype, {})[slot] = safe_sug
    save_schedule(folder, sched)
    _append_edit_log(stem, "apply_suggestion", f"{daytype}/{slot}: {cur:.2f} -> {safe_sug:.2f}")

    log(f"AI(A): applied suggestion {stem} {daytype}/{slot} {cur:.2f}->{safe_sug:.2f}")
    self._load_editor_from_files()
    self._refresh_stats(force=False)


def _export_suggestions_csv(self):
    """Копирует файл предложений в выбранное пользователем место."""
    try:
        from tkinter import filedialog
        src = PRICE_SUGGESTIONS_CSV_PATH
        if not src.exists():
            messagebox.showwarning("Экспорт", f"Файл предложений не найден:\n{src}")
            return
        dst = filedialog.asksaveasfilename(
            title="Экспорт предложений AI",
            defaultextension=".csv",
            filetypes=[("CSV файлы", "*.csv"), ("Все файлы", "*.*")],
            initialfile=f"ai_suggestions_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not dst:
            return
        import shutil as _shutil
        _shutil.copy2(src, dst)
        messagebox.showinfo("Экспорт", f"Предложения сохранены:\n{dst}")
    except Exception as e:
        messagebox.showerror("Ошибка экспорта", str(e))

# ---------- Inline charts ----------
def _redraw_inline_charts(self):
    if not hasattr(self, "_inline_canvas") or self._inline_canvas is None:
        return
    canvas = self._inline_canvas
    canvas.delete("all")
    w = max(10, canvas.winfo_width())
    h = max(10, canvas.winfo_height())
    pad = 18

    rows = getattr(self, "_stats_rows", None)
    if not rows:
        canvas.create_text(pad, pad, anchor="nw", text="No data", fill=self.colors["muted"], font=("Segoe UI", 11))
        return

    def draw_panel(x0, y0, x1, title, items, fmt):
        canvas.create_text(x0, y0, anchor="nw", text=title, fill=self.colors["fg"], font=("Segoe UI", 10, "bold"))
        y = y0 + 22
        bar_h = 16
        if not items:
            canvas.create_text(x0, y, anchor="nw", text="(no data)", fill=self.colors["muted"], font=("Segoe UI", 9))
            return
        maxv = max(v for _, v in items) or 1.0
        for i, (label, val) in enumerate(items[:8]):
            yy = y + i * (bar_h + 6)
            bw = int((x1 - x0 - 160) * (val / maxv))
            canvas.create_rectangle(x0, yy, x0 + bw, yy + bar_h, outline="", fill="#2a2f3a")
            canvas.create_text(x0 + bw + 8, yy + bar_h/2, anchor="w",
                               text=f"{label} — {fmt(val)}",
                               fill=self.colors["fg"], font=("Segoe UI", 9))

    # layout: 3 columns
    gap = 10
    col_w = (w - pad*2 - gap*2) // 3
    x0 = pad
    x1 = x0 + col_w
    x2 = x1 + gap
    x3 = x2 + col_w
    x4 = x3 + gap
    x5 = x4 + col_w

    top_score = sorted(rows, key=lambda r: r.get("score_num", 0.0), reverse=True)
    top_posts_hr = sorted(rows, key=lambda r: r.get("posts_hr", 0.0), reverse=True)
    top_rev_hr = sorted(rows, key=lambda r: r.get("rev_hr", 0.0), reverse=True)

    draw_panel(x0, pad, x1, "Top by Score", [(r["vehicle"], r.get("score_num", 0.0)) for r in top_score], lambda v: f"{v:.0f}")
    draw_panel(x2, pad, x3, "Top by Posts/hr", [(r["vehicle"], r.get("posts_hr", 0.0)) for r in top_posts_hr], lambda v: f"{v:.2f}")
    draw_panel(x4, pad, x5, "Top by $/hr", [(r["vehicle"], r.get("rev_hr", 0.0)) for r in top_rev_hr], lambda v: f"${v:.0f}")



# ================== STABILITY PATCH (auto-bind missing callbacks) ==================
# This block prevents startup crashes caused by missing App methods after merges/indent issues.
# It attaches a safe __getattr__ and minimal _setup_window, plus a few common callbacks.
import tkinter as _tk

def _app___getattr__(self, name):
    """
    Safety net for missing callbacks in mixed/merged builds.
    If UI wires a missing handler (e.g., _pump_logs, _on_*, clear_*), we return a safe no-op callable
    instead of crashing at startup. For everything else, delegate to tkinter.Tk.__getattr__.
    """
    # Treat these as "internal callback" names that should be callable.
    _callback_prefixes = (
        "_pump_", "_on_", "_setup_", "_build_", "_redraw_", "_refresh_", "_apply_",
        "_save_", "_load_", "_update_", "open_", "clear_", "reset_", "do_",
    )
    try:
        if isinstance(name, str) and name.startswith(_callback_prefixes):
            def _missing_cb(*args, **kwargs):
                try:
                    # log once per name
                    seen = getattr(self, "_missing_cb_seen", set())
                    if name not in seen:
                        seen.add(name)
                        setattr(self, "_missing_cb_seen", seen)
                        try:
                            self._log(f"[WARN] Missing callback: {name} (no-op)")
                        except Exception:
                            pass
                except Exception:
                    pass
                return None
            return _missing_cb
    except Exception:
        pass

    # Not our internal callback → let tkinter handle it (widget commands, etc.)
    return _tk.Tk.__getattr__(self, name)
def _app__setup_window(self):
    # Minimal window setup; keep safe.
    try:
        self.title(getattr(self, "APP_TITLE", "Wiwang Poster — LOOP"))
    except Exception:
        pass
    try:
        self.minsize(900, 600)
    except Exception:
        pass
    try:
        self.geometry(getattr(self, "DEFAULT_GEOMETRY", "1200x780"))
    except Exception:
        pass

def _app__on_reset_arm_changed(self, *args, **kwargs):
    # Optional UI toggle; safe no-op.
    return None

def _app_clear_logs(self, *args, **kwargs):
    try:
        for name in ("txt_logs","log_text","logs_text","txt_log","console_text","txt_console"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.delete("1.0","end")
                return
            except Exception:
                pass
        for name in ("logs_tree","tree_logs","lst_logs","list_logs"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.delete(*w.get_children())
                return
            except Exception:
                try:
                    w.delete(0,"end")
                    return
                except Exception:
                    pass
    except Exception:
        pass

def _bind_stability_methods():
    if "App" not in globals():
        return
    # Ensure our __getattr__ overrides Tk.__getattr__
    if not hasattr(App, "__getattr__") or App.__getattr__ is _tk.Tk.__getattr__:
        setattr(App, "__getattr__", _app___getattr__)
    # Ensure setup window exists
    if not hasattr(App, "_setup_window"):
        setattr(App, "_setup_window", _app__setup_window)
    if not hasattr(App, "_on_reset_arm_changed"):
        setattr(App, "_on_reset_arm_changed", _app__on_reset_arm_changed)
    if not hasattr(App, "clear_logs"):
        setattr(App, "clear_logs", _app_clear_logs)

        # Bind editor/AI helpers that may become top-level defs after merges
        try:
            bind_names = [
                '_selected_vehicle', '_selected_folder',
                '_smart_build_schedule_for_selected',
                '_load_editor_from_files', '_save_editor_changes', '_save_schedule_only',
                '_refresh_suggestions', '_apply_selected_suggestion', '_apply_all_suggestions',
                '_apply_one_suggestion', '_export_suggestions_csv', '_redraw_inline_charts',
                '_compute_vehicle_slot_metrics',
            ]
            for _n in bind_names:
                _fn = globals().get(_n)
                if callable(_fn):
                    setattr(App, _n, _fn)
        except Exception:
            pass

_bind_stability_methods()
# ================== END STABILITY PATCH ==================


# ===================== STABILITY BOOTSTRAP v6 (runs BEFORE main) =====================
# This file is a merged build; some App methods can "fall out" of the class due to indentation/merge edits.
# Tkinter callbacks (command=self.<handler>) will crash at startup if handlers are missing.
#
# v6 guarantees:
#  - App.__getattr__ returns safe no-op callables for missing internal callbacks (including on_*)
#  - App.on_close exists (used by WM_DELETE_WINDOW protocol)
#  - critical stubs exist if missing: _setup_window, _pump_logs, clear_logs
#
# NOTE: This block is intentionally placed BEFORE def main() so it executes before App() is instantiated.

import tkinter as _tk  # noqa

def _v6_missing_cb_factory(_name: str):
    def _cb(*args, **kwargs):
        # no-op handler; logs once if possible
        try:
            seen = getattr(_cb, "_seen", set())
            if _name not in seen:
                seen.add(_name)
                _cb._seen = seen
                try:
                    if hasattr(_cb, "_app") and hasattr(_cb._app, "_log"):
                        _cb._app._log(f"[WARN] Missing callback: {_name} (no-op)")
                except Exception:
                    pass
        except Exception:
            pass
        return None
    return _cb

def _v6_safe_getattr(self, name: str):
    prefixes = (
        "on_",  # <-- important for on_close
        "_on_", "_setup_", "_build_", "_pump_", "_refresh_", "_redraw_", "_apply_",
        "_save_", "_load_", "_update_", "open_", "clear_", "reset_", "do_",
    )
    try:
        if isinstance(name, str) and name.startswith(prefixes):
            cb = _v6_missing_cb_factory(name)
            try:
                cb._app = self
            except Exception:
                pass
            # Special-case: WM_DELETE_WINDOW expects on_close to exist
            if name == "on_close":
                def _close_cb(*a, **k):
                    return _v6_on_close_stub(self)
                return _close_cb
            return cb
    except Exception:
        pass
    return _tk.Tk.__getattr__(self, name)

def _v6_on_close_stub(self):
    """Safe window close handler."""
    try:
        setattr(self, "_closing", True)
    except Exception:
        pass
    # attempt to stop worker loop politely
    for attr in ("stop_event", "_stop_event", "stop_flag"):
        ev = getattr(self, attr, None)
        try:
            if ev is not None and hasattr(ev, "set"):
                ev.set()
        except Exception:
            pass
    try:
        setattr(self, "running", False)
    except Exception:
        pass
    # save config if available
    try:
        if hasattr(self, "_save_config"):
            self._save_config()
    except Exception:
        pass
    # close window
    try:
        self.destroy()
    except Exception:
        try:
            self.quit()
        except Exception:
            pass

def _v6_setup_window_stub(self):
    """Minimal window setup stub."""
    try:
        try:
            cur = self.title()
        except Exception:
            cur = ""
        try:
            if not cur:
                self.title("Wiwang Poster — Авторазмещение")
        except Exception:
            pass
    except Exception:
        pass

def _v6_pump_logs_stub(self):
    """Periodic log pump stub. Never crashes; reschedules itself."""
    try:
        q = getattr(self, "log_queue", None) or getattr(self, "_log_queue", None)
        if q is not None:
            while True:
                try:
                    msg = q.get_nowait()
                except Exception:
                    break
                try:
                    if hasattr(self, "_append_log_line"):
                        self._append_log_line(str(msg))
                    else:
                        for nm in ("txt_logs","log_text","logs_text","txt_console","console_text"):
                            w = getattr(self, nm, None)
                            if w is None:
                                continue
                            try:
                                w.insert("end", str(msg) + "\n")
                                w.see("end")
                                break
                            except Exception:
                                pass
                except Exception:
                    pass
        if not getattr(self, "_closing", False):
            try:
                self.after(250, self._pump_logs)
            except Exception:
                pass
    except Exception:
        try:
            self.after(500, self._pump_logs)
        except Exception:
            pass

def _v6_clear_logs_stub(self):
    """Clear logs view safely."""
    try:
        for nm in ("txt_logs","log_text","logs_text","txt_console","console_text"):
            w = getattr(self, nm, None)
            if w is None:
                continue
            try:
                w.delete("1.0", "end")
                return
            except Exception:
                pass
        for nm in ("logs_tree","tree_logs","lst_logs","list_logs"):
            w = getattr(self, nm, None)
            if w is None:
                continue
            try:
                w.delete(*w.get_children())
                return
            except Exception:
                try:
                    w.delete(0, "end")
                    return
                except Exception:
                    pass
    except Exception:
        pass

def _v6_install():
    if "App" not in globals():
        return
    # Force-install safe getattr and key methods
    try:
        setattr(App, "__getattr__", _v6_safe_getattr)
    except Exception:
        pass
    try:
        if not hasattr(App, "on_close"):
            setattr(App, "on_close", _v6_on_close_stub)
    except Exception:
        pass
    try:
        if not hasattr(App, "_setup_window"):
            setattr(App, "_setup_window", _v6_setup_window_stub)
    except Exception:
        pass
    try:
        if not hasattr(App, "_pump_logs"):
            setattr(App, "_pump_logs", _v6_pump_logs_stub)
    except Exception:
        pass
    try:
        if not hasattr(App, "clear_logs"):
            setattr(App, "clear_logs", _v6_clear_logs_stub)
    except Exception:
        pass

_v6_install()
print("[BOOT] STABLE v6 installed (safe callbacks + on_close).")
# ===================== /STABILITY BOOTSTRAP v6 =====================


def main():
    _enforce_user_file_limits()
    init_pyautogui()
    if os.getenv("ADMIN_MODE") == "1":
        # Initialize TG tracker module for Admin process too
        # (GUI reads tg_rent_tracker.get_status() for live metrics)
        try:
            _admin_cfg = ConfigManager.load()
            _admin_tg = _admin_cfg.get("telegram", {})
            if isinstance(_admin_tg, dict):
                _aid = str(_admin_tg.get("api_id") or "").strip()
                _ahash = str(_admin_tg.get("api_hash") or "").strip()
                _sname = str(_admin_tg.get("session_name") or "").strip()
                if _aid and not os.getenv("TG_API_ID"):
                    os.environ["TG_API_ID"] = _aid
                if _ahash and not os.getenv("TG_API_HASH"):
                    os.environ["TG_API_HASH"] = _ahash
                if _sname and not os.getenv("TG_SESSION_PATH"):
                    _spath = USER_DIR / f"{_sname}.session"
                    if _spath.exists():
                        os.environ["TG_SESSION_PATH"] = str(_spath)
            if not os.getenv("TG_SUMMARY_PATH"):
                os.environ["TG_SUMMARY_PATH"] = str(USER_DIR / "rentals_summary.json")
            if not os.getenv("PLATE_MAP_PATH"):
                os.environ["PLATE_MAP_PATH"] = str(USER_DIR / "plate_map.json")
            _admin_tg_enabled = bool(_admin_cfg.get("tg_tracker_cfg", {}).get("enabled", False))
            if bool(_admin_tg.get("enabled", False)):
                _admin_tg_enabled = True
            tg_rent_tracker.start(enabled=_admin_tg_enabled)
        except Exception as e:
            log(f"ADMIN tg_rent_tracker init: {e}")
        app = App()
        app.protocol("WM_DELETE_WINDOW", app.on_close)
        app.mainloop()
        return
    cfg_holder = {"cfg": ConfigManager.load()}
    tg_cfg = cfg_holder["cfg"].get("telegram", {})
    if isinstance(tg_cfg, dict):
        api_id = str(tg_cfg.get("api_id") or "").strip()
        api_hash = str(tg_cfg.get("api_hash") or "").strip()
        session_name = str(tg_cfg.get("session_name") or "").strip()
        if api_id and not os.getenv("TG_API_ID"):
            os.environ["TG_API_ID"] = api_id
        if api_hash and not os.getenv("TG_API_HASH"):
            os.environ["TG_API_HASH"] = api_hash
        if session_name and not os.getenv("TG_SESSION_PATH"):
            session_path = USER_DIR / f"{session_name}.session"
            if session_path.exists():
                os.environ["TG_SESSION_PATH"] = str(session_path)
    tg_enabled = bool(cfg_holder["cfg"].get("tg_tracker_cfg", {}).get("enabled", False))
    if bool(cfg_holder["cfg"].get("telegram", {}).get("enabled", False)):
        tg_enabled = True
    if os.getenv("TG_TRACKER_ENABLED") == "1":
        tg_enabled = True
    if not os.getenv("TG_SUMMARY_PATH"):
        os.environ["TG_SUMMARY_PATH"] = str(USER_DIR / "rentals_summary.json")
    if not os.getenv("PLATE_MAP_PATH"):
        os.environ["PLATE_MAP_PATH"] = str(USER_DIR / "plate_map.json")
    try:
        tg_rent_tracker.start(enabled=tg_enabled)
        log("tg_rent_tracker: started" if tg_enabled else "tg_rent_tracker: disabled")
    except Exception as e:
        log(f"tg_rent_tracker: start failed: {e}")
    if plate_registry.is_enabled():
        log("Plate registry: ON")
    else:
        log("Plate registry: OFF")
    root = tk.Tk()

    def _cfg_provider():
        return dict(cfg_holder["cfg"])

    # Wire up debug mode so click_xy can read config at runtime
    global _DEBUG_CFG_PROVIDER
    _DEBUG_CFG_PROVIDER = _cfg_provider

    def _cfg_save(new_cfg):
        cfg_holder["cfg"] = dict(new_cfg)
        ConfigManager.save(cfg_holder["cfg"])

    app_gui = AppGUI(
        root,
        cfg_provider=_cfg_provider,
        cfg_save=_cfg_save,
        log_fn=log,
        colors={
            "bg":      "#1a1a2e",
            "panel":   "#16213e",
            "panel2":  "#0f3460",
            "fg":      "#d4d4dc",
            "muted":   "#7a7a8e",
            "accent":  "#4a6fa5",
            "accent2": "#5b8a72",
            "success": "#4caf7c",
            "danger":  "#c0544e",
            "warning": "#c4903e",
            "border":  "#2a2a4a",
            "btn":     "#253554",
        },
    )

    admin_window = {"app": None}

    def _open_admin(_event=None):
        if admin_window.get("pid"):
            return
        try:
            import subprocess
            env = dict(os.environ)
            env["ADMIN_MODE"] = "1"
            p = subprocess.Popen([sys.executable, __file__], env=env)
            admin_window["pid"] = p.pid
        except Exception as e:
            log(f"Admin UI start failed: {e}")

    root.bind("<<AdminUnlocked>>", _open_admin, add="+")
    root.mainloop()





def _update_multiplier_previews(self):
    """Update price preview labels next to multiplier fields."""
    try:
        base_raw = (self.entry_base_price.get() or "").strip()
        base = float(base_raw) if base_raw else 0.0
    except Exception:
        base = 0.0

    def _fmt(v: float) -> str:
        try:
            if v <= 0:
                return "—"
            return f"${int(round(v)):,}"
        except Exception:
            return "—"

    try:
        labels = getattr(self, "mult_preview_labels", None)
        entries = getattr(self, "mult_entries", None)
        if not labels or not entries:
            return
        for daytype in ("weekday", "weekend"):
            for slot in ("low", "mid", "high", "gold"):
                try:
                    e = entries[daytype][slot]
                    raw = (e.get() or "").strip()
                    mult = float(raw) if raw else 0.0
                except Exception:
                    mult = 0.0
                try:
                    lbl = labels[daytype][slot]
                    lbl.configure(text=_fmt(base * mult))
                except Exception:
                    pass
    except Exception:
        pass


def _pulse_tick(self):
    """Tiny UI animation. Safe: never throws."""
    try:
        c = getattr(self, "_pulse_canvas", None)
        dot = getattr(self, "_pulse_dot", None)
        if not c or dot is None:
            return
        st = ""
        try:
            st = (self.status_var.get() or "").upper()
        except Exception:
            st = ""

        if "RUN" in st:
            self._pulse_state = not bool(getattr(self, "_pulse_state", False))
            fill = self.colors.get("accent") if self._pulse_state else self.colors.get("panel2")
        elif "PAUSE" in st:
            fill = self.colors.get("muted")
        else:
            fill = self.colors.get("panel2")
        try:
            c.itemconfig(dot, fill=fill)
        except Exception:
            pass
    finally:
        try:
            self.after(350, self._pulse_tick)
        except Exception:
            pass

# --- Bind module-level UI helper functions into App (fix indentation/merge issues) ---


try:
    for _name in [
        '_build_editor_ui',
        '_build_pricing_ai_ui',
        '_selected_vehicle',
        '_selected_folder',
        '_smart_build_schedule_for_selected',
        '_load_editor_from_files',
        '_save_editor_changes',
        '_compute_vehicle_slot_metrics',
        '_refresh_suggestions',
        '_apply_selected_suggestion',
        '_apply_all_suggestions',
        '_apply_one_suggestion',
        '_export_suggestions_csv',
        '_update_multiplier_previews',
        '_pulse_tick',
        '_redraw_inline_charts']:
        if _name in globals() and not hasattr(App, _name):
            setattr(App, _name, globals()[_name])
except Exception:
    pass


# --- Auto-bind any orphaned App methods (self, ...) that were left at module level
def _bind_orphan_app_methods():
    import inspect as _inspect
    g = globals()
    for _name, _obj in list(g.items()):
        if _name in {'main', '_bind_orphan_app_methods'}:
            continue
        if not callable(_obj):
            continue
        if getattr(_obj, '__name__', None) != _name:
            continue
        try:
            _sig = _inspect.signature(_obj)
        except Exception:
            continue
        _params = list(_sig.parameters.values())
        if not _params:
            continue
        if _params[0].name != 'self':
            continue
        if not hasattr(App, _name):
            setattr(App, _name, _obj)

_bind_orphan_app_methods()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            log(f"FATAL:\n{tb}")
        except Exception:
            pass
        _fatal_messagebox(tb)
        try:
            input("FATAL error. Press Enter to exit...")
        except Exception:
            pass
        raise

# --- FIX: ensure App has clear_logs (used by Logs tab buttons) ---
def _app_clear_logs(self):
    """Clear logs view (UI), safe no-op if widgets not present."""
    try:
        candidates = [
            "txt_logs", "log_text", "logs_text", "txt_log", "log_view", "txt_log_view",
            "txt_logs_view", "txt_console", "console_text"
        ]
        for name in candidates:
            w = getattr(self, name, None)
            if w is None:
                continue
            # Tk Text widget
            try:
                w.delete("1.0", "end")
                return
            except Exception:
                pass
        # Tree or Listbox case
        for name in ["logs_tree", "tree_logs", "lst_logs", "list_logs"]:
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.delete(*w.get_children())
                return
            except Exception:
                try:
                    w.delete(0, "end")
                    return
                except Exception:
                    pass
    except Exception:
        pass

# attach if missing
try:
    if "App" in globals() and not hasattr(App, "clear_logs"):
        setattr(App, "clear_logs", _app_clear_logs)
except Exception:
    pass



# --- Fallback implementations for critical callbacks (only if missing) ---
def _fallback_on_reset_arm_changed(self):
    """Enable/disable hold buttons when ARM RESET is toggled. Auto-disarm after 10s."""
    try:
        armed = bool(getattr(self, "reset_armed_var", None).get()) if hasattr(self, "reset_armed_var") else False

        # buttons that should be protected by ARM RESET
        btns = []
        for n in ("btn_clear_log_file", "btn_reset_stats", "btn_reset_processed", "btn_reset_photo_hashes"):
            b = getattr(self, n, None)
            if b is not None:
                btns.append(b)

        # cancel old timer
        if getattr(self, "_arm_after_id", None):
            try:
                self.after_cancel(self._arm_after_id)
            except Exception:
                pass
            self._arm_after_id = None

        if armed:
            for b in btns:
                try: b.configure(state="normal")
                except Exception: pass
            if hasattr(self, "reset_status_var"):
                try: self.reset_status_var.set("⚠ ВЗВЕДЁН: кнопки активны (авто-сброс 10с)")
                except Exception: pass

            def disarm():
                try:
                    if hasattr(self, "reset_armed_var"):
                        self.reset_armed_var.set(False)
                    for b in btns:
                        try: b.configure(state="disabled")
                        except Exception: pass
                    if hasattr(self, "reset_status_var"):
                        try: self.reset_status_var.set("Авто-сброс")
                        except Exception: pass
                finally:
                    self._arm_after_id = None

            self._arm_after_id = self.after(10_000, disarm)
        else:
            for b in btns:
                try: b.configure(state="disabled")
                except Exception: pass
            if hasattr(self, "reset_status_var"):
                try: self.reset_status_var.set("Сброс: ЗАБЛОКИРОВАН")
                except Exception: pass
    except Exception as ex:
        try: log(f"[WARN] reset arm handler error: {ex}")
        except Exception: pass

def _fallback_clear_logs(self):
    """Clear logs text widget view (UI only)."""
    try:
        w = getattr(self, "log_view", None)
        if w is not None:
            try:
                w.delete("1.0", "end")
                return
            except Exception:
                pass
        for n in ("txt_logs", "log_text", "logs_text", "txt_log"):
            w = getattr(self, n, None)
            if w is None: 
                continue
            try:
                w.delete("1.0", "end")
                return
            except Exception:
                pass
    except Exception:
        pass

try:
    if "App" in globals():
        if not hasattr(App, "_on_reset_arm_changed"):
            setattr(App, "_on_reset_arm_changed", _fallback_on_reset_arm_changed)
        if not hasattr(App, "clear_logs"):
            setattr(App, "clear_logs", _fallback_clear_logs)
except Exception:
    pass


# --- FIX: ensure App has _pump_logs (scheduled via after in __init__) ---
def _app_pump_logs(self):
    """
    Periodic log pump. Safe even if widgets/queues are missing.
    """
    try:
        # If there is a queue-like object, drain it
        q = getattr(self, "log_queue", None) or getattr(self, "_log_queue", None)
        if q is not None:
            while True:
                try:
                    msg = q.get_nowait()
                except Exception:
                    break
                try:
                    self._append_log_line(str(msg))
                except Exception:
                    # try common widgets
                    for name in ("txt_logs","log_text","logs_text","txt_console","console_text"):
                        w = getattr(self, name, None)
                        if w is None:
                            continue
                        try:
                            w.insert("end", str(msg) + "\n")
                            w.see("end")
                            break
                        except Exception:
                            pass
        # Reschedule itself if app is still alive
        try:
            if getattr(self, "_closing", False):
                return
            self.after(250, self._pump_logs)
        except Exception:
            pass
    except Exception:
        # never crash the UI loop
        try:
            self.after(500, self._pump_logs)
        except Exception:
            pass

try:
    if "App" in globals() and not hasattr(App, "_pump_logs"):
        setattr(App, "_pump_logs", _app_pump_logs)
except Exception:
    pass




# ===================== STABILITY BOOTSTRAP (force-install callbacks) =====================
# Your build is a merged file where some methods/callbacks can "fall out" of class App due to indentation/merge.
# Tkinter wires many buttons using command=self.<callback>. If the callback is missing, Tk tries to resolve it
# via tkapp and the app crashes at startup.
#
# This bootstrap force-installs:
#   - App.__getattr__ that returns safe no-op callables for missing internal callbacks
#   - critical stubs (e.g. _setup_window, _pump_logs) if they are missing
#
# It does NOT delete any existing functionality; it only prevents startup crashes.

import tkinter as _tk  # noqa

def _missing_cb_factory(_name: str):
    def _cb(*args, **kwargs):
        try:
            seen = getattr(_cb, "_seen", set())
            if _name not in seen:
                seen.add(_name)
                _cb._seen = seen
                try:
                    # Prefer App._log if exists
                    if hasattr(_cb, "_app") and hasattr(_cb._app, "_log"):
                        _cb._app._log(f"[WARN] Missing callback: {_name} (no-op)")
                except Exception:
                    pass
        except Exception:
            pass
        return None
    return _cb

def _app_safe_getattr(self, name: str):
    prefixes = (
        "_on_", "_setup_", "_build_", "_pump_", "_refresh_", "_redraw_", "_apply_",
        "_save_", "_load_", "_update_", "open_", "clear_", "reset_", "do_",
    )
    try:
        if isinstance(name, str) and name.startswith(prefixes):
            cb = _missing_cb_factory(name)
            try:
                cb._app = self
            except Exception:
                pass
            return cb
    except Exception:
        pass
    # Delegate to Tk for normal behavior (e.g. self.tk callables)
    return _tk.Tk.__getattr__(self, name)

def _app_setup_window_stub(self):
    """Minimal window setup stub (safe)."""
    try:
        # Do nothing heavy: Tk window already exists. Keep it safe.
        if not getattr(self, "title", None):
            return
        # Keep whatever title is already set in code; only set if empty.
        try:
            cur = self.title()
            if not cur:
                self.title("Wiwang Poster — Авторазмещение")
        except Exception:
            pass
    except Exception:
        pass

def _app_pump_logs_stub(self):
    """Periodic log pump stub. Never crashes; reschedules itself."""
    try:
        q = getattr(self, "log_queue", None) or getattr(self, "_log_queue", None)
        if q is not None:
            while True:
                try:
                    msg = q.get_nowait()
                except Exception:
                    break
                try:
                    if hasattr(self, "_append_log_line"):
                        self._append_log_line(str(msg))
                    else:
                        # fallback to common text widgets
                        for nm in ("txt_logs","log_text","logs_text","txt_console","console_text"):
                            w = getattr(self, nm, None)
                            if w is None:
                                continue
                            try:
                                w.insert("end", str(msg) + "\n")
                                w.see("end")
                                break
                            except Exception:
                                pass
                except Exception:
                    pass
        # reschedule
        if not getattr(self, "_closing", False):
            try:
                self.after(250, self._pump_logs)
            except Exception:
                pass
    except Exception:
        try:
            self.after(500, self._pump_logs)
        except Exception:
            pass

def _app_clear_logs_stub(self):
    """Clear logs view safely."""
    try:
        for nm in ("txt_logs","log_text","logs_text","txt_console","console_text"):
            w = getattr(self, nm, None)
            if w is None:
                continue
            try:
                w.delete("1.0", "end")
                return
            except Exception:
                pass
        for nm in ("logs_tree","tree_logs","lst_logs","list_logs"):
            w = getattr(self, nm, None)
            if w is None:
                continue
            try:
                w.delete(*w.get_children())
                return
            except Exception:
                try:
                    w.delete(0, "end")
                    return
                except Exception:
                    pass
    except Exception:
        pass

def _install_stability_bootstrap():
    try:
        if "App" not in globals():
            return
        # Force-install __getattr__ (overrides tk.Tk.__getattr__ for App)
        try:
            setattr(App, "__getattr__", _app_safe_getattr)
        except Exception:
            pass
        # Install critical stubs if missing
        try:
            if not hasattr(App, "_setup_window"):
                setattr(App, "_setup_window", _app_setup_window_stub)
        except Exception:
            pass
        try:
            if not hasattr(App, "_pump_logs"):
                setattr(App, "_pump_logs", _app_pump_logs_stub)
        except Exception:
            pass
        try:
            if not hasattr(App, "clear_logs"):
                setattr(App, "clear_logs", _app_clear_logs_stub)
        except Exception:
            pass
    except Exception:
        pass

_install_stability_bootstrap()
# ===================== /STABILITY BOOTSTRAP =====================
