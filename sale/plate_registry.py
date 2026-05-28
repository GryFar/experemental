import importlib
import importlib.util
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("plate_registry")
logger.addHandler(logging.NullHandler())

_ENABLED_ENV = "PLATE_REGISTRY_ENABLED"
_HEADLESS_ENV = "PLATE_REGISTRY_HEADLESS"
_MAP_PATH = Path(os.getenv("PLATE_MAP_PATH", "plate_map.json"))

_PLATE_CYR_TO_LAT = {
    "А": "A",
    "В": "B",
    "Е": "E",
    "К": "K",
    "М": "M",
    "Н": "H",
    "О": "O",
    "Р": "P",
    "С": "C",
    "Т": "T",
    "У": "Y",
    "Х": "X",
}


def _normalize_plate_key(text: str) -> str:
    if not text:
        return ""
    text = text.upper()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9A-ZА-Я]", "", text)
    return "".join(_PLATE_CYR_TO_LAT.get(ch, ch) for ch in text)


def _find_entry_by_normalized(data: Dict[str, Dict[str, Any]], plate_norm: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not plate_norm:
        return None, None
    for key, entry in data.items():
        if _normalize_plate_key(key) == plate_norm:
            return key, entry
    return None, None


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


def _is_headless() -> bool:
    return os.getenv(_HEADLESS_ENV) == "1"


def is_enabled() -> bool:
    return _is_enabled()


def count_entries() -> int:
    try:
        data = _load_map()
        return len(data.keys())
    except Exception:
        return 0


def list_mappings() -> Dict[str, Dict[str, Any]]:
    try:
        return dict(_load_map())
    except Exception:
        return {}


def upsert_mapping(plate: str, vehicle_key: str) -> bool:
    if not _is_enabled():
        return False
    try:
        plate = _normalize_plate_key(str(plate or "").strip())
        vehicle_key = str(vehicle_key or "").strip()
        if not plate or not vehicle_key:
            return False
        data = _load_map()
        entry = data.get(plate)
        never_rent = False
        if isinstance(entry, dict):
            never_rent = bool(entry.get("never_rent"))
        data[plate] = {"vehicle_key": vehicle_key, "never_rent": never_rent}
        _save_map(data)
        return True
    except Exception as exc:
        _log_error(f"plate_registry upsert_mapping error: {exc}")
        return False


def remove_mapping(plate: str) -> bool:
    if not _is_enabled():
        return False
    try:
        plate = str(plate or "").strip()
        if not plate:
            return False
        plate_norm = _normalize_plate_key(plate)
        data = _load_map()
        key, _entry = _find_entry_by_normalized(data, plate_norm)
        if key or plate in data:
            data.pop(key or plate, None)
            if plate_norm and plate_norm in data:
                data.pop(plate_norm, None)
            _save_map(data)
        return True
    except Exception as exc:
        _log_error(f"plate_registry remove_mapping error: {exc}")
        return False


def _load_map() -> Dict[str, Dict[str, Any]]:
    try:
        if _MAP_PATH.exists():
            data = json.loads(_MAP_PATH.read_text(encoding="utf-8", errors="ignore") or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:
        _log_error(f"plate_registry load failed: {exc}")
    return {}


def _save_map(data: Dict[str, Dict[str, Any]]) -> None:
    try:
        _MAP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _log_error(f"plate_registry save failed: {exc}")


def _get_tk_modules() -> Optional[Tuple[Any, Any]]:
    if importlib.util.find_spec("tkinter") is None:
        _log_error("plate_registry tkinter unavailable")
        return None
    tk = importlib.import_module("tkinter")
    simpledialog = importlib.import_module("tkinter.simpledialog")
    return tk, simpledialog


def _prompt_plate(default_plate: str) -> Optional[str]:
    root = None
    try:
        if _is_headless():
            _log_info("plate_registry headless: skip plate prompt")
            return None
        modules = _get_tk_modules()
        if modules is None:
            return None
        tk, simpledialog = modules
        root = tk.Tk()
        root.withdraw()
        plate = simpledialog.askstring("Введите госномер", "Введите госномер", initialvalue=default_plate)
        if plate is None:
            return None
        plate = str(plate).strip()
        return plate or None
    except Exception as exc:
        _log_error(f"plate_registry prompt failed: {exc}")
        return None
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def _prompt_vehicle_key(plate: str) -> Optional[str]:
    root = None
    try:
        if _is_headless():
            _log_info(f"plate_registry headless: skip vehicle_key prompt for {plate}")
            return None
        modules = _get_tk_modules()
        if modules is None:
            return None
        tk, simpledialog = modules
        root = tk.Tk()
        root.withdraw()
        prompt = f"Введите vehicle_key для {plate}"
        vehicle_key = simpledialog.askstring("Введите vehicle_key", prompt)
        if vehicle_key is None:
            return None
        vehicle_key = str(vehicle_key).strip()
        return vehicle_key or None
    except Exception as exc:
        _log_error(f"plate_registry vehicle_key prompt failed: {exc}")
        return None
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def get_vehicle_by_plate(plate: str) -> Optional[str]:
    """
    Return vehicle_key for a known plate. If unknown, prompt and store mapping.

    Enabled via PLATE_REGISTRY_ENABLED=1.
    """
    if not _is_enabled():
        return None

    try:
        plate_raw = str(plate or "").strip()
        plate = _normalize_plate_key(plate_raw)
        if not plate:
            return None

        data = _load_map()
        entry = data.get(plate)
        key = plate if entry is not None else None
        if entry is None:
            entry = data.get(plate_raw)
            key = plate_raw if entry is not None else None
        if entry is None:
            key, entry = _find_entry_by_normalized(data, plate)
        if isinstance(entry, dict) and entry.get("vehicle_key"):
            return str(entry.get("vehicle_key"))

        entered_plate = _prompt_plate(plate_raw)
        if not entered_plate:
            return None

        entered_norm = _normalize_plate_key(entered_plate)
        if entered_norm != plate:
            plate = entered_norm
            entry = data.get(plate) or data.get(entered_plate)
            if entry is None:
                key, entry = _find_entry_by_normalized(data, plate)
            if isinstance(entry, dict) and entry.get("vehicle_key"):
                return str(entry.get("vehicle_key"))

        vehicle_key = _prompt_vehicle_key(plate)
        if not vehicle_key:
            return None

        data[plate] = {
            "vehicle_key": vehicle_key,
            "never_rent": False,
        }
        if key and key != plate:
            data.pop(key, None)
        _save_map(data)
        return vehicle_key
    except Exception as exc:
        _log_error(f"plate_registry get_vehicle_by_plate error: {exc}")
        return None


def resolve_or_prompt(plate: str) -> Optional[str]:
    """
    Resolve plate to vehicle_key. If unknown, prompt and persist mapping.
    """
    try:
        return get_vehicle_by_plate(plate)
    except Exception as exc:
        _log_error(f"plate_registry resolve_or_prompt error: {exc}")
        return None

def is_never_rent(plate: str) -> bool:
    if not _is_enabled():
        return False

    try:
        plate_raw = str(plate or "").strip()
        plate = _normalize_plate_key(plate_raw)
        if not plate:
            return False

        data = _load_map()
        entry = data.get(plate) or data.get(plate_raw)
        if entry is None:
            _key, entry = _find_entry_by_normalized(data, plate)
        if isinstance(entry, dict):
            return bool(entry.get("never_rent"))
        return False
    except Exception as exc:
        _log_error(f"plate_registry is_never_rent error: {exc}")
        return False


def register_plate(plate: str, vehicle_key: str) -> None:
    if not _is_enabled():
        return

    try:
        plate = _normalize_plate_key(str(plate or "").strip())
        vehicle_key = str(vehicle_key or "").strip()
        if not plate or not vehicle_key:
            return

        data = _load_map()
        entry = data.get(plate)
        never_rent = False
        if isinstance(entry, dict):
            never_rent = bool(entry.get("never_rent"))

        data[plate] = {
            "vehicle_key": vehicle_key,
            "never_rent": never_rent,
        }
        _save_map(data)
    except Exception as exc:
        _log_error(f"plate_registry register_plate error: {exc}")
