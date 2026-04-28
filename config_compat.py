import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple


def load_config(path: Path, log_fn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    def _log(message: str) -> None:
        if log_fn:
            try:
                log_fn(message)
            except Exception:
                pass

    if not path.exists():
        _log(f"CONFIG: path missing -> {path}")
        return {}

    raw = ""
    try:
        _log(f"CONFIG: reading {path}")
        raw = path.read_text(encoding="utf-8", errors="ignore")
        _log(f"CONFIG: read bytes={len(raw)}")
        data = json.loads(raw)
        if isinstance(data, dict):
            _log(f"CONFIG: loaded ok keys={len(data.keys())}")
            return data
    except Exception as e:
        _log(f"CONFIG load error -> attempting repair: {e}")

    repaired = raw
    try:
        repaired = re.sub(r"//.*?$", "", repaired, flags=re.M)
        repaired = re.sub(r"#.*?$", "", repaired, flags=re.M)
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        repaired = repaired.strip()
        if repaired:
            data = json.loads(repaired)
            if isinstance(data, dict):
                _backup_bad(path, raw)
                _atomic_write(path, data)
                _log("CONFIG repaired and re-saved")
                _log(f"CONFIG repaired keys={len(data.keys())}")
                return data
    except Exception as e2:
        _backup_bad(path, raw)
        _log(f"CONFIG repair failed: {e2}")

    return {}


def apply_defaults(cfg: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    def _merge(base: Any, extra: Any) -> Any:
        if isinstance(base, dict) and isinstance(extra, dict):
            merged = dict(extra)
            for k, v in base.items():
                if k in merged:
                    merged[k] = _merge(v, merged[k])
                else:
                    merged[k] = v
            return merged
        return extra if extra is not None else base

    if not isinstance(cfg, dict):
        cfg = {}
    if not isinstance(defaults, dict):
        return cfg
    return _merge(defaults, cfg)


def deep_merge(
    old: Any,
    new: Any,
    *,
    changed_keys: Optional[Set[str]] = None,
    key: str = "",
    allow_empty_lists: bool = False,
) -> Any:
    if isinstance(old, dict) and isinstance(new, dict):
        merged = dict(old)
        for k, v in new.items():
            merged[k] = deep_merge(
                old.get(k),
                v,
                changed_keys=changed_keys,
                key=str(k),
                allow_empty_lists=allow_empty_lists,
            )
        return merged

    if isinstance(new, list):
        if new or allow_empty_lists or _is_explicit_change(key, changed_keys):
            return list(new)
        return old if old is not None else list(new)

    if _is_placeholder_scalar(new) and not _is_explicit_change(key, changed_keys):
        return old

    return new


def save_config_preserve(
    path: Path,
    cfg_updates: Dict[str, Any],
    log_fn: Optional[Callable[[str], None]] = None,
    changed_keys: Optional[Set[str]] = None,
    allow_empty_lists: bool = False,
) -> Tuple[bool, int, int]:
    def _log(message: str) -> None:
        if log_fn:
            try:
                log_fn(message)
            except Exception:
                pass

    _log(f"CONFIG: save request -> {path}")
    old_cfg = load_config(path, log_fn=log_fn)
    before = len(old_cfg.keys()) if isinstance(old_cfg, dict) else 0
    if changed_keys:
        _log(f"CONFIG: changed_keys={sorted(changed_keys)}")
    merged = deep_merge(
        old_cfg,
        cfg_updates,
        changed_keys=changed_keys,
        allow_empty_lists=allow_empty_lists,
    )
    after = len(merged.keys()) if isinstance(merged, dict) else 0

    if after < before:
        _log(f"CONFIG ERROR: key loss detected, abort saving (before={before}, after={after})")
        return False, before, after

    _atomic_write(path, merged)
    _log(f"CONFIG: saved ok keys={after} (before={before})")
    return True, before, after


def detect_keys(cfg: Dict[str, Any]) -> Dict[str, Any]:
    fastscan_keys = []
    tg_keys = []
    if isinstance(cfg, dict):
        for k in cfg.keys():
            if str(k).startswith("fast_scan"):
                fastscan_keys.append(str(k))
        tg = cfg.get("telegram")
        if isinstance(tg, dict):
            tg_keys = list(tg.keys())
    return {"fastscan": fastscan_keys, "tg": tg_keys}


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _backup_bad(path: Path, raw: str) -> None:
    try:
        ts = __import__("time").strftime("%Y%m%d_%H%M%S")
        bad = path.with_suffix(f".bad_{ts}.json")
        if not bad.exists():
            bad.write_text(raw or "", encoding="utf-8")
    except Exception:
        pass


def _is_placeholder_scalar(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _is_explicit_change(key: str, changed_keys: Optional[Set[str]]) -> bool:
    if not key or not changed_keys:
        return False
    return key in changed_keys
