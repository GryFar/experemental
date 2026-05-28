import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import plate_registry


@dataclass
class Decision:
    allowed: bool
    reason: str
    details: Optional[Dict[str, Any]] = None


def allow(vehicle_key: str, plate: Optional[str], tg_state: Dict[str, Any], scan_state: Dict[str, Any], now) -> Decision:
    limits = tg_state.get("limits", {}) if isinstance(tg_state, dict) else {}

    hard_block_no_plate = os.getenv("HARD_BLOCK_NO_PLATE", "0") == "1"
    if isinstance(limits, dict) and limits.get("hard_block_no_plate") is True:
        hard_block_no_plate = True

    hard_block_unknown = False
    if isinstance(limits, dict) and limits.get("hard_block_unknown_plate") is True:
        hard_block_unknown = True

    min_conf = None
    if isinstance(limits, dict):
        min_conf = limits.get("min_plate_confidence")

    if not plate:
        if hard_block_no_plate:
            return Decision(False, "no_plate", {"hard_block_no_plate": True})
        return Decision(True, "no_plate")

    try:
        if plate_registry.is_never_rent(plate):
            return Decision(False, "never_rent", {"plate": plate})
    except Exception:
        pass

    if not vehicle_key and hard_block_unknown:
        return Decision(False, "unknown_plate_blocked", {"plate": plate})

    confidence = scan_state.get("plate_confidence") if isinstance(scan_state, dict) else None
    if min_conf is not None and confidence is not None:
        try:
            if float(confidence) < float(min_conf):
                return Decision(False, "low_confidence", {"confidence": confidence, "min": min_conf})
        except Exception:
            pass

    if isinstance(limits, dict):
        use_tg_active = bool(limits.get("use_tg_active_truth", False))
        max_active = limits.get("max_active_rentals_per_vehicle")
        if use_tg_active and max_active is not None:
            active = tg_state.get("active_for_vehicle", 0)
            try:
                if int(active) >= int(max_active):
                    return Decision(False, "over_active_limit", {"active": active, "limit": max_active})
            except Exception:
                pass

        use_fastscan = bool(limits.get("use_fastscan_truth", False))
        if use_fastscan:
            free = scan_state.get("fastscan_free") if isinstance(scan_state, dict) else None
            try:
                if free is not None and int(free) <= 0:
                    return Decision(False, "over_active_limit", {"fastscan_free": free})
            except Exception:
                pass

        cooldown_min = limits.get("cooldown_minutes_after_return")
        last_end = tg_state.get("last_end_ts")
        if cooldown_min and last_end:
            try:
                cooldown_s = float(cooldown_min) * 60.0
                if float(now) < float(last_end) + cooldown_s:
                    return Decision(False, "cooldown", {"cooldown_min": cooldown_min})
            except Exception:
                pass

        max_daily = limits.get("max_daily_hours")
        daily_hours = tg_state.get("hours_today", 0)
        if max_daily is not None:
            try:
                if float(daily_hours) >= float(max_daily):
                    return Decision(False, "over_daily_hours", {"hours": daily_hours, "limit": max_daily})
            except Exception:
                pass

        max_weekly = limits.get("max_weekly_hours")
        weekly_hours = tg_state.get("hours_week", 0)
        if max_weekly is not None:
            try:
                if float(weekly_hours) >= float(max_weekly):
                    return Decision(False, "over_weekly_hours", {"hours": weekly_hours, "limit": max_weekly})
            except Exception:
                pass

    return Decision(True, "allowed")
