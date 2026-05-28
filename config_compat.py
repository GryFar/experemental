import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

_MAX_BACKUPS = 20
_LOCK_TIMEOUT_S = 5.0  # макс время ожидания файлового лока
_LOCK_POLL_S = 0.05


@contextmanager
def _file_lock(path: Path, log_fn: Optional[Callable[[str], None]] = None):
    """Межпроцессный лок на базе .lock-файла рядом с config.json.

    Необходим из-за двухпроцессной архитектуры (GUI + Admin), когда оба пишут в один файл.
    Использует msvcrt.locking на Windows / fcntl.flock на POSIX. Без внешних зависимостей.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    fh = None
    locked = False
    is_win = sys.platform.startswith("win")
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        fh = open(str(lock_path), "a+b")
        if is_win:
            try:
                import msvcrt  # type: ignore
                deadline = time.time() + _LOCK_TIMEOUT_S
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.time() > deadline:
                            break
                        time.sleep(_LOCK_POLL_S)
            except Exception:
                locked = False
        else:
            try:
                import fcntl  # type: ignore
                deadline = time.time() + _LOCK_TIMEOUT_S
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        if time.time() > deadline:
                            break
                        time.sleep(_LOCK_POLL_S)
            except Exception:
                locked = False
        if not locked and log_fn:
            try:
                log_fn(f"CONFIG: lock not acquired ({_LOCK_TIMEOUT_S}s), proceeding without lock")
            except Exception:
                pass
        yield locked
    finally:
        if fh is not None:
            try:
                if locked:
                    if is_win:
                        try:
                            import msvcrt  # type: ignore
                            try:
                                fh.seek(0)
                            except Exception:
                                pass
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                    else:
                        try:
                            import fcntl  # type: ignore
                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
            finally:
                try:
                    fh.close()
                except Exception:
                    pass


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
    with _file_lock(path, log_fn=_log):
        # Под локом: перечитать свежий конфиг (вдруг второй процесс писал),
        # смерджить и записать — одной атомарной операцией.
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

        # No-op защита: если ничего не поменялось — не пишем и не делаем backup.
        # Это резко снижает спам в логах и ротацию бэкапов.
        if merged == old_cfg:
            _log(f"CONFIG: no-op (no changes) keys={after}")
            return True, before, after

        _backup_versioned(path, log_fn=_log)
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
    try:
        with open(tmp, "r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass
    os.replace(str(tmp), str(path))


def _backup_versioned(path: Path, log_fn=None) -> None:
    if not path.exists():
        return
    try:
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = backup_dir / f"{path.stem}_{ts}.json"
        shutil.copy2(str(path), str(dst))
        existing = sorted(backup_dir.glob(f"{path.stem}_*.json"))
        for old_file in existing[:-_MAX_BACKUPS]:
            try:
                old_file.unlink()
            except Exception:
                pass
        if log_fn:
            log_fn(f"CONFIG: backup -> {dst.name}")
    except Exception as e:
        if log_fn:
            log_fn(f"CONFIG: backup failed: {e}")


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


# ── Public convenience aliases ──────────────────────────────────────────────────────────────

def load_config_safe(path, defaults=None):
    """Load config and merge with defaults (existing keys take priority)."""
    cfg = load_config(path)
    if defaults:
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v
    return cfg


def backup_config(path):
    """Create a timestamped backup of the config file. Returns backup Path or None."""
    from pathlib import Path as _Path
    import shutil as _shutil, time as _time
    p = _Path(path)
    if not p.exists():
        return None
    try:
        backup_dir = p.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        dst = backup_dir / f"{p.stem}_{ts}.json"
        _shutil.copy2(str(p), str(dst))
        return dst
    except Exception:
        return None
