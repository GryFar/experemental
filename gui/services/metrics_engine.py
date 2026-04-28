from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List


def _parse_iso_ts(value: Any) -> float:
    if not value:
        return 0.0
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return 0.0
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


def _parse_ts(ts: str) -> float:
    return _parse_iso_ts(ts)


def _parse_error_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        return _parse_iso_ts(value)
    return 0.0


def compute_error_counts(errors: List[Dict[str, Any]], now_ts: float) -> Dict[str, int]:
    window_10m = 10 * 60
    window_1h = 60 * 60
    count_10m = 0
    count_1h = 0
    for err in errors:
        ts = _parse_error_ts(err.get("timestamp"))
        if not ts:
            continue
        age = now_ts - ts
        if age <= window_10m:
            count_10m += 1
        if age <= window_1h:
            count_1h += 1
    return {"10m": count_10m, "1h": count_1h}


def compute_metrics(records: List[Dict[str, Any]], now_ts: float) -> Dict[str, Any]:
    day_sec = 24 * 3600
    week_sec = 7 * day_sec
    month_sec = 30 * day_sec

    totals = {
        "today": 0.0,
        "week": 0.0,
        "month": 0.0,
    }
    active = 0
    # ИСПРАВЛЕНО: считаем сумму и количество записей с ненулевым price_per_hour
    rate_sum = 0.0
    rate_count = 0

    by_vehicle: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "income_7d": 0.0,
            "income_30d": 0.0,
            "income_total": 0.0,
            "hours": 0.0,
            "count": 0,
            "plate": "",
        }
    )

    for rec in records:
        try:
            ts = rec.get("timestamp")
            total = float(rec.get("total_sum") or 0)
            hours = float(rec.get("hours") or 0)
            plate = str(rec.get("plate") or "")
            rate = float(rec.get("price_per_hour") or 0)
        except Exception:
            continue

        start_ts = _parse_ts(str(ts))
        if not start_ts:
            continue
        age = now_ts - start_ts
        if age <= day_sec:
            totals["today"] += total
        if age <= week_sec:
            totals["week"] += total
        if age <= month_sec:
            totals["month"] += total

        # ИСПРАВЛЕНО: avg_rate = средняя цена/час по всем записям где она указана
        if rate > 0:
            rate_sum += rate
            rate_count += 1

        vehicle_key = str(rec.get("vehicle_key") or "")
        if not vehicle_key:
            vehicle_key = plate
        info = by_vehicle[vehicle_key]
        info["income_total"] += total
        if age <= week_sec:
            info["income_7d"] += total
        if age <= month_sec:
            info["income_30d"] += total
        info["hours"] += hours
        info["count"] += 1
        if plate:
            info["plate"] = plate

        # Аренда считается активной если ещё не истёк срок (timestamp + hours)
        if start_ts and hours > 0:
            if now_ts < start_ts + hours * 3600:
                active += 1

    avg_rate_value = rate_sum / rate_count if rate_count else 0.0

    # Топ машина по общему доходу за всё время
    top_vehicle = "—"
    top_income = 0.0
    for key, info in by_vehicle.items():
        if info["income_total"] > top_income:
            top_income = info["income_total"]
            top_vehicle = key

    return {
        "totals": totals,
        "active": active,
        "avg_rate": avg_rate_value,
        "top_vehicle": top_vehicle,
        "by_vehicle": dict(by_vehicle),
    }
