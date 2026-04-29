from typing import Any, Dict


def load_ui_prefs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw = cfg.get("ui_prefs")
    return dict(raw) if isinstance(raw, dict) else {}


def save_ui_prefs(cfg: Dict[str, Any], prefs: Dict[str, Any]) -> Dict[str, Any]:
    current = cfg.get("ui_prefs")
    current = dict(current) if isinstance(current, dict) else {}
    current.update(prefs or {})
    cfg["ui_prefs"] = current
    return cfg


def get_pref(cfg: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Безопасно вернуть значение из ui_prefs с fallback."""
    prefs = load_ui_prefs(cfg)
    return prefs.get(key, default)


def set_pref(cfg: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    """Безопасно установить одно значение в ui_prefs."""
    return save_ui_prefs(cfg, {key: value})
