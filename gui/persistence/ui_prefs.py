from typing import Any, Dict


def load_ui_prefs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    prefs = cfg.get("ui_prefs", {}) if isinstance(cfg.get("ui_prefs"), dict) else {}
    return dict(prefs)


def save_ui_prefs(cfg: Dict[str, Any], prefs: Dict[str, Any]) -> Dict[str, Any]:
    current = cfg.get("ui_prefs", {}) if isinstance(cfg.get("ui_prefs"), dict) else {}
    merged = {**current, **prefs}
    cfg["ui_prefs"] = dict(merged)
    return cfg
