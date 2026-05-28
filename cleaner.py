"""
cleaner.py — автоочистка мусора: логи, скриншоты, временные файлы.

Удаляет:
  - Старые .log файлы (по умолчанию старше 3 дней)
  - Старые скриншоты .png / .jpg (по умолчанию старше 1 дня)
  - Старые файлы в папке screenshots/ и logs/
  - plate_read_fail_*.png в корне проекта (всегда)
  - sale/file_dialog_debug_verify_none_*.png
  - sale/_TRASH_* папки
  - Файлы processed.json и photo_hashes.json не трогает (важные данные)

Использование:
    # Разово:
    from cleaner import Cleaner
    Cleaner.run_once()

    # Фоновый поток (запускает очистку раз в N минут):
    cleaner = Cleaner(interval_minutes=60)
    cleaner.start()

    # Остановить:
    cleaner.stop()

Конфиг (все параметры опциональны):
    Cleaner(
        interval_minutes = 60,      # как часто чистить
        log_max_age_days = 3,       # лог-файлы старше X дней
        screenshot_max_age_days = 1,# скриншоты старше X дней
        max_log_lines = 5000,       # обрезать log-файл если строк больше
        keep_last_lines = 1000,     # оставить последних N строк
        base_dir = ".",             # корневая папка проекта
        extra_dirs = [],            # дополнительные папки для очистки
        dry_run = False,            # True = только показать что удалится
    )
"""
from __future__ import annotations

import re
import shutil
import time
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional


class Cleaner(threading.Thread):
    """
    Фоновый поток автоочистки.
    Запускается как daemon, останавливается через stop().
    """

    # Файлы которые никогда не удаляем
    PROTECTED = {
        "processed.json",
        "photo_hashes.json",
        "config.json",
        "config.yaml",
        "settings.json",
        "cleaner.py",
        "archive.csv",
        "decisions.csv",
        "description.txt",
        "price.txt",
        "schedule.json",
        "anchor_form.png",
        "plate_label_anchor.png",
    }

    # Папки которые ПОЛНОСТЬЮ защищены — не трогаем ни один файл внутри.
    # car/ содержит эталонные PNG-иконки машин для image matching — их нельзя удалять!
    # Исключение: _TRASH_ подпапки внутри car/ — их удаляем.
    PROTECTED_DIRS = {
        "car",   # C:\\sale\\car\\*.png — иконки для распознавания машин
    }

    # Расширения скриншотов
    SCREENSHOT_EXTS = {".png", ".jpg", ".jpeg", ".bmp"}

    # Расширения логов
    LOG_EXTS = {".log", ".txt"}

    # Папки со скриншотами которые всегда чистим агрессивно
    SCREENSHOT_DIRS = {"screenshots", "screen", "screens", "img_temp", "tmp"}

    # Папки с логами
    LOG_DIRS = {"logs", "log", "debug_logs"}

    # Паттерны имён которые всегда удаляем (мусорные дебаг-скрины)
    JUNK_NAME_PATTERNS = (
        re.compile(r"^.*\.tmp$",                              re.IGNORECASE),
        re.compile(r"^plate_read_fail_.*\.png$",              re.IGNORECASE),
        re.compile(r"^file_dialog_debug_verify_none_.*\.png$", re.IGNORECASE),
        re.compile(r"^file_dialog_debug_.*\.png$",             re.IGNORECASE),
        # plate_line_YYYYMMDD_HHMMSS.png — скрины номерных знаков (мусор, накапливаются сотнями)
        re.compile(r"^plate_line_\d{8}_\d{6}\.png$",          re.IGNORECASE),
        # item_screen_*.png, monitor_debug_*.png — дебаг item_actions
        re.compile(r"^item_screen_.*\.png$",                   re.IGNORECASE),
        re.compile(r"^monitor_debug_.*\.png$",                 re.IGNORECASE),
        re.compile(r"^screen_debug_.*\.png$",                  re.IGNORECASE),
    )

    def __init__(
        self,
        *,
        interval_minutes:         float = 60.0,
        log_max_age_days:         float = 3.0,
        screenshot_max_age_days:  float = 1.0,
        max_log_lines:            int   = 5000,
        keep_last_lines:          int   = 1000,
        base_dir:                 str   = ".",
        extra_dirs:               Optional[List[str]] = None,
        dry_run:                  bool  = False,
        log_fn:                   Optional[Callable[[str], None]] = None,
    ):
        super().__init__(daemon=True, name="Cleaner")
        self.interval_sec            = interval_minutes * 60.0
        self.log_max_age_sec         = log_max_age_days * 86400.0
        self.screenshot_max_age_sec  = screenshot_max_age_days * 86400.0
        self.max_log_lines           = max_log_lines
        self.keep_last_lines         = keep_last_lines
        self.base_dir                = Path(base_dir).resolve()
        self.extra_dirs              = [Path(d).resolve() for d in (extra_dirs or [])]
        self.dry_run                 = dry_run
        self._log_fn                 = log_fn or print
        self._stop_event             = threading.Event()

    def stop(self) -> None:
        """Остановить поток очистки."""
        self._stop_event.set()

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(f"[Cleaner] {msg}")
        except Exception:
            pass

    # ── Публичные методы ──────────────────────────────────────────────────────────

    @classmethod
    def run_once(
        cls,
        base_dir: str = ".",
        log_max_age_days: float = 3.0,
        screenshot_max_age_days: float = 1.0,
        max_log_lines: int = 5000,
        keep_last_lines: int = 1000,
        dry_run: bool = False,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Запустить очистку один раз синхронно (без потока)."""
        instance = cls(
            base_dir=base_dir,
            log_max_age_days=log_max_age_days,
            screenshot_max_age_days=screenshot_max_age_days,
            max_log_lines=max_log_lines,
            keep_last_lines=keep_last_lines,
            dry_run=dry_run,
            log_fn=log_fn,
        )
        instance._do_cleanup()

    # ── Основной цикл потока ──────────────────────────────────────────────

    def run(self) -> None:
        self._log("Старт автоочистки")
        # Первая очистка — сразу при запуске
        self._safe_cleanup()

        while not self._stop_event.is_set():
            # Ждём до следующей очистки (с возможностью прерваться)
            end = time.time() + self.interval_sec
            while time.time() < end and not self._stop_event.is_set():
                time.sleep(min(10.0, end - time.time()))

            if not self._stop_event.is_set():
                self._safe_cleanup()

        self._log("Очистка остановлена")

    def _safe_cleanup(self) -> None:
        try:
            self._do_cleanup()
        except Exception as e:
            self._log(f"Ошибка очистки: {e}\n{traceback.format_exc()}")

    # ── Логика очистки ──────────────────────────────────────────────

    def _is_junk_name(self, name: str) -> bool:
        """Проверяет, является ли имя файла мусорным дебаг-скрином."""
        for pattern in self.JUNK_NAME_PATTERNS:
            if pattern.match(name):
                return True
        return False

    def _do_cleanup(self) -> None:
        now = time.time()
        deleted_files = 0
        freed_bytes   = 0
        trimmed_logs  = 0
        deleted_dirs  = 0

        search_dirs = [self.base_dir] + self.extra_dirs

        for base in search_dirs:
            if not base.exists():
                continue

            # ── Удаляем _TRASH_ директории в sale/ ──
            sale_dir = base / "sale"
            if sale_dir.exists():
                for entry in list(sale_dir.iterdir()):
                    if entry.is_dir() and entry.name.startswith("_TRASH_"):
                        if not self.dry_run:
                            try:
                                shutil.rmtree(str(entry))
                                deleted_dirs += 1
                                self._log(f"Удалена папка мусора: {entry.name}")
                            except Exception:
                                pass
                        else:
                            self._log(f"  [dry] удалю папку: {entry}")

            # ── Удаляем _TRASH_ папки внутри car/ ──
            car_dir = base / "car"
            if car_dir.exists():
                for entry in list(car_dir.iterdir()):
                    if entry.is_dir() and entry.name.startswith("_TRASH_"):
                        if not self.dry_run:
                            try:
                                shutil.rmtree(str(entry))
                                deleted_dirs += 1
                                self._log(f"Удалена папка мусора в car/: {entry.name}")
                            except Exception:
                                pass
                        else:
                            self._log(f"  [dry] удалю папку car/: {entry}")

            for path in base.rglob("*"):
                if not path.is_file():
                    continue

                # Никогда не удаляем защищённые файлы
                if path.name in self.PROTECTED:
                    continue

                # Пропускаем скрытые файлы и папки (.git, __pycache__ и т.д.)
                parts = path.parts
                if any(p.startswith(".") or p == "__pycache__" for p in parts):
                    continue

                # ── ЗАЩИТА ПАПКИ car/ — иконки машин для image matching ──
                # Пропускаем ВСЕ файлы внутри car/ (кроме _TRASH_ — они уже удалены выше)
                # car/ содержит эталонные PNG по которым скрипт находит нужную папку машины
                rel_parts = path.relative_to(base).parts
                if rel_parts and rel_parts[0].lower() in {p.lower() for p in self.PROTECTED_DIRS}:
                    # Исключение: _TRASH_ подпапки уже удалены выше отдельным блоком
                    continue

                ext = path.suffix.lower()
                age_sec = now - path.stat().st_mtime

                # ── Мусорные дебаг-скрины — удаляем всегда (независимо от возраста) ──
                if self._is_junk_name(path.name):
                    freed_bytes += path.stat().st_size
                    if not self.dry_run:
                        try:
                            path.unlink()
                            deleted_files += 1
                        except Exception:
                            pass
                    else:
                        self._log(f"  [dry] удалю junk: {path.name}")
                    continue

                # ── Скриншоты ──
                if ext in self.SCREENSHOT_EXTS:
                    # В специальных папках — агрессивно (по возрасту)
                    in_ss_dir = any(p.lower() in self.SCREENSHOT_DIRS for p in path.parts)
                    threshold = self.screenshot_max_age_sec if in_ss_dir else self.screenshot_max_age_sec * 2
                    if age_sec > threshold:
                        freed_bytes += path.stat().st_size
                        if not self.dry_run:
                            try:
                                path.unlink()
                                deleted_files += 1
                            except Exception:
                                pass
                        else:
                            self._log(f"  [dry] удалю скрин: {path}")
                    continue

                # ── Лог-файлы: обрезаем если большие, удаляем старые ──
                if ext in self.LOG_EXTS:
                    # Удаляем совсем старые логи
                    if age_sec > self.log_max_age_sec:
                        freed_bytes += path.stat().st_size
                        if not self.dry_run:
                            try:
                                path.unlink()
                                deleted_files += 1
                            except Exception:
                                pass
                        else:
                            self._log(f"  [dry] удалю лог: {path}")
                        continue
                    # Обрезаем лог если слишком большой
                    trimmed = self._trim_log(path)
                    if trimmed:
                        trimmed_logs += 1
                    continue

        if deleted_files or trimmed_logs or deleted_dirs:
            freed_mb = freed_bytes / 1_048_576
            self._log(
                f"Очищено: удалено {deleted_files} файлов ({freed_mb:.1f} МБ), "
                f"обрезано {trimmed_logs} лог-файлов, "
                f"удалено {deleted_dirs} мусорных папок"
            )
        else:
            self._log("Нечего чистить — всё в порядке")

    def _trim_log(self, path: Path) -> bool:
        """
        Если лог длиннее max_log_lines — оставляет последние keep_last_lines строк.
        Возвращает True если файл был обрезан.
        """
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            if len(lines) <= self.max_log_lines:
                return False

            trimmed = lines[-self.keep_last_lines:]
            header = f"[Cleaner] Лог обрезан {datetime.now().strftime('%Y-%m-%d %H:%M')} " \
                     f"(было {len(lines)} строк, оставлено {len(trimmed)})\n"

            if not self.dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(header)
                    f.writelines(trimmed)
                return True
            else:
                self._log(f"  [dry] обрежу лог {path.name}: {len(lines)} → {len(trimmed)} строк")
                return False

        except Exception:
            return False


# ── Удобная функция для быстрого запуска из main.py ────────────────────────

def start_cleaner(
    base_dir: str = ".",
    interval_minutes: float = 60.0,
    screenshot_max_age_days: float = 1.0,
    log_max_age_days: float = 3.0,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Cleaner:
    """
    Запускает фоновую очистку и возвращает экземпляр Cleaner.

    Пример:
        from cleaner import start_cleaner
        cleaner = start_cleaner(base_dir=".", log_fn=self.log)
        # Чтобы остановить:
        # cleaner.stop()
    """
    cleaner = Cleaner(
        base_dir=base_dir,
        interval_minutes=interval_minutes,
        screenshot_max_age_days=screenshot_max_age_days,
        log_max_age_days=log_max_age_days,
        log_fn=log_fn,
    )
    cleaner.start()
    return cleaner
