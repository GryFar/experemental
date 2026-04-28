import hashlib
import os
import time
from typing import Any, Dict, Optional


_LOCKOUT_SECONDS = 60
_ATTEMPT_WINDOW = 60
_MAX_ATTEMPTS = 5


class AdminGate:
    def __init__(self) -> None:
        self._attempts = []
        self._locked_until = 0.0

    def ensure_config(self, cfg: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        gate = dict(cfg.get("admin_gate", {}) if isinstance(cfg.get("admin_gate"), dict) else {})
        changed = False
        if "enabled" not in gate:
            gate["enabled"] = os.getenv("ADMIN_GATE_ENABLED", "0") == "1"
            changed = True
        if not gate.get("min_length"):
            gate["min_length"] = 8
            changed = True
        if gate.get("enabled"):
            if not gate.get("salt"):
                gate["salt"] = os.urandom(8).hex()
                changed = True
            if not gate.get("password_hash"):
                default_password = os.getenv("ADMIN_PASSWORD")
                if default_password:
                    gate["password_hash"] = self._hash(gate["salt"], default_password)
                    changed = True
                else:
                    gate["enabled"] = False
                    changed = True
        cfg["admin_gate"] = gate
        return cfg, changed

    def check_password(self, cfg: Dict[str, Any], candidate: str) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        gate = cfg.get("admin_gate", {}) if isinstance(cfg.get("admin_gate"), dict) else {}
        if not gate.get("enabled"):
            return False
        min_length = int(gate.get("min_length") or 0)
        if len(candidate) < min_length:
            return False
        now = time.time()
        if now < self._locked_until:
            return False
        self._attempts = [t for t in self._attempts if now - t <= _ATTEMPT_WINDOW]
        if len(self._attempts) >= _MAX_ATTEMPTS:
            self._locked_until = now + _LOCKOUT_SECONDS
            self._attempts = []
            return False

        salt = str(gate.get("salt") or "")
        expected = str(gate.get("password_hash") or "")
        if not salt or not expected:
            return False

        if self._hash(salt, candidate) == expected:
            self._attempts = []
            return True

        self._attempts.append(now)
        return False

    def lockout_remaining(self) -> Optional[int]:
        now = time.time()
        if now < self._locked_until:
            return int(self._locked_until - now)
        return None

    def accept_override(self, cfg: Dict[str, Any], candidate: str) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        override = os.getenv("ADMIN_PASSWORD")
        if not override or candidate != override:
            return False
        gate = cfg.get("admin_gate", {}) if isinstance(cfg.get("admin_gate"), dict) else {}
        salt = str(gate.get("salt") or "") or os.urandom(8).hex()
        gate["salt"] = salt
        gate["password_hash"] = self._hash(salt, candidate)
        gate["enabled"] = True
        if not gate.get("min_length"):
            gate["min_length"] = 8
        cfg["admin_gate"] = gate
        return True

    @staticmethod
    def _hash(salt: str, password: str) -> str:
        return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
