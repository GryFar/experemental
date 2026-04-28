"""
item_actions.py — автоматические действия на страницах маркетплейса Wiwang.

Полный цикл когда нас перебили:
  1. navigate_to_items_tab()   — клик «Предметы» в левом меню
  2. search_item()             — клик в строку поиска, вводим название
  3. click_found_item()        — кликаем на найденную карточку
  4. read_sellers_table()      — читаем таблицу продавцов (OCR + пикселя)
  5. check_our_position()      — возвращаем (rank, price)
  6. delete_lot_from_table()   — клик «Удалить лот» (красный текст в таблице)
     ИЛИ
     delete_lot_from_page()    — клик «Удалить лот с продажи» (кнопка внизу)
  7. confirm_delete()          — диалог → кнопка «Удалить объявление»
  8. click_add_lot_btn()       — «Добавить лот на продажу»
  9. fill_and_submit_lot_form() — поле цены + слайдеры кол-ва/часов + «Оплатить наличными»

Все координаты замерены по реальным скринам 1456×804.
Можно переопределить через cfg-словарь.

Структура формы «Выставить лот» (из скринов):
  ┌─────────────────────────────────────────────────┐
  │  Название товара:  <текст>                      │
  │  Доступно кол-во:  <текст>                      │
  │                                                 │
  │  Стоимость продажи за 1 шт.                     │
  │  ┌──────────────────────────────────────────┐   │
  │  │  Введите значение / 19179               │   │  ← input (714, 311)
  │  └──────────────────────────────────────────┘   │
  │                                                 │
  │  Выставить на продажу    Доступно к продаже     │
  │  [1 шт.] ●────────────────────── [107 шт.]     │  ← слайдер (529,424)→(899,424)
  │                                                 │
  │  Часы размещения         Максимальное значение  │
  │  [5 ч.] ●──────────────────────  [120 ч.]     │  ← слайдер (529,524)→(899,524)
  │                                                 │
  │  Итоговая сумма:  $10  $20  -50%                │
  │                                                 │
  │  [Оплатить наличными]  [Оплатить картой]        │  ← (607,664) / (810,664)
  └─────────────────────────────────────────────────┘
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import pyautogui as pg
from PIL import ImageGrab, ImageEnhance

# pyperclip опционален — используется только для резервного копирования буфера
try:
    import pyperclip as _pyperclip
    _CLIP = True
except ImportError:
    _pyperclip = None
    _CLIP = False

# pynput — для посимвольного ввода кириллицы (WM_CHAR события, как ручной ввод)
try:
    from pynput.keyboard import Controller as _KbController
    _PYNPUT = True
except ImportError:
    _KbController = None
    _PYNPUT = False

# keyboard — резервный вариант для ввода текста
try:
    import keyboard as _keyboard
    _KEYBOARD_LIB = True
except ImportError:
    _keyboard = None
    _KEYBOARD_LIB = False

# pytesseract опционален — проверяем и импорт и наличие бинарника tesseract
try:
    import pytesseract
    # Проверяем что tesseract binary доступен (не только Python-пакет)
    try:
        pytesseract.get_tesseract_version()
        _OCR = True
    except Exception:
        # tesseract не установлен или не в PATH — OCR недоступен, используем fallback
        _OCR = False
except ImportError:
    _OCR = False

from debug_state import DEBUG_SLOW as _DEBUG_SLOW

# ─── Константы — координаты по скринам 1456×804 ───────────────────────────────

# Левое меню
MENU_ITEMS_X,   MENU_ITEMS_Y   = 105, 268

# Строка поиска (вкладка Предметы)
SEARCH_X,       SEARCH_Y       = 385, 32

# Первая карточка результатов поиска (центр картинки)
CARD_X,         CARD_Y         = 345, 190

# Таблица продавцов — регион (x1, y1, x2, y2)
TABLE_REGION = (645, 95, 1415, 410)

# Y-координаты строк таблицы (шаг ~35px)
TABLE_ROW_Y   = [141, 176, 211, 246, 281, 316, 351, 386]
TABLE_ACTION_X = 1350   # колонка «Действие»
TABLE_PRICE_X  = 1130   # колонка «Цена за 1 шт.»
TABLE_PLAYER_X = 810    # колонка «Имя игрока»
TABLE_QTY_X    = 1020   # колонка «Кол-во»

# Кнопки на странице предмета (под картинкой)
ADD_LOT_BTN_X,    ADD_LOT_BTN_Y    = 341, 503   # «Добавить лот на продажу» (синяя)
DELETE_LOT_BTN_X, DELETE_LOT_BTN_Y = 457, 503   # иконка корзины (удалить)

# ─── Форма «Выставить лот» (замерено по скринам ttpojhuv5L / SBoF5mtAXs) ──────

# Поле «Стоимость продажи за 1 шт.» — текстовый input
FORM_PRICE_X, FORM_PRICE_Y = 714, 311

# Слайдер «Выставить на продажу» (кол-во)
#   Трек: от (529, 424) до (899, 424)
#   Левый край = 1 шт., правый край = макс. доступно
FORM_QTY_TRACK_X1, FORM_QTY_TRACK_X2 = 529, 899
FORM_QTY_TRACK_Y   = 424

# Слайдер «Часы размещения»
#   Трек: от (529, 524) до (899, 524)
#   Левый край = 1 ч., правый край = 120 ч.
FORM_HOURS_TRACK_X1, FORM_HOURS_TRACK_X2 = 529, 899
FORM_HOURS_TRACK_Y   = 524
FORM_HOURS_MAX       = 120  # максимум в часах

# Кнопка подтверждения — «Оплатить наличными»
FORM_SUBMIT_X, FORM_SUBMIT_Y = 607, 664

# ─── Диалог подтверждения удаления ────────────────────────────────────────────
# Кнопка «Удалить объявление» (красная, центр кнопки)
CONFIRM_DELETE_BTN_X, CONFIRM_DELETE_BTN_Y = 622, 478
# Регион диалога
DIALOG_REGION = (475, 260, 960, 545)


# ─── Базовые утилиты ──────────────────────────────────────────────────────────

def _log(cfg: Dict, msg: str) -> None:
    fn = cfg.get("_log_fn", print)
    try:
        fn(msg)
    except Exception:
        pass


def _sleep(s: float) -> None:
    delay = max(0.0, float(s))
    if _DEBUG_SLOW["enabled"]:
        delay = max(delay, _DEBUG_SLOW["delay"])
    time.sleep(delay)


def _click(x: int, y: int, delay: float = 0.3) -> None:
    """Плавный клик с задержкой после."""
    pg.moveTo(x, y, duration=0.10)
    pg.click()
    _sleep(delay)


def _triple_click(x: int, y: int, delay: float = 0.25) -> None:
    """Тройной клик — выделить всё в поле."""
    pg.moveTo(x, y, duration=0.08)
    pg.tripleClick()
    _sleep(delay)


def _clear_type(x: int, y: int, text: str, interval: float = 0.05) -> None:
    """Кликнуть, выделить всё (Ctrl+A), стереть (Backspace), напечатать."""
    _triple_click(x, y, 0.12)
    pg.hotkey("ctrl", "a")
    _sleep(0.05)
    pg.press("backspace")
    _sleep(0.05)
    pg.typewrite(str(text), interval=interval)
    _sleep(0.15)


def _click_img(
    path: str,
    confidence: float = 0.80,
    timeout: float = 3.0,
    region: Optional[Tuple] = None,
    delay: float = 0.3,
) -> Optional[Tuple[int, int]]:
    """Найти изображение на экране и кликнуть. Возвращает (x,y) или None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            loc = pg.locateCenterOnScreen(path, confidence=confidence, region=region)
            if loc:
                _click(int(loc.x), int(loc.y), delay)
                return (int(loc.x), int(loc.y))
        except Exception:
            pass
        _sleep(0.15)
    return None


def _find_img(
    path: str,
    confidence: float = 0.78,
    timeout: float = 2.0,
    region: Optional[Tuple] = None,
) -> Optional[Tuple[int, int]]:
    """Найти изображение, вернуть центр без клика."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            loc = pg.locateCenterOnScreen(path, confidence=confidence, region=region)
            if loc:
                return (int(loc.x), int(loc.y))
        except Exception:
            pass
        _sleep(0.15)
    return None


def _grab(region: Tuple) -> "PIL.Image.Image":
    return ImageGrab.grab(bbox=region)


def _ocr(region: Tuple, lang: str = "rus+eng") -> str:
    if not _OCR:
        return ""
    img = _grab(region)
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.5)
    img = img.point(lambda p: 255 if p > 130 else 0)
    return pytesseract.image_to_string(img, lang=lang, config="--psm 6")


def _c(cfg: Dict, key: str, default):
    """Получить значение из конфига или вернуть default."""
    return cfg.get(key, default)


# ─── Управление слайдером ──────────────────────────────────────────────────────

def _drag_slider(
    track_x1: int,
    track_x2: int,
    track_y: int,
    value: float,
    min_val: float,
    max_val: float,
) -> None:
    """
    Кликает на нужную позицию слайдера.

    Рассчитывает X-координату по формуле:
        x = track_x1 + (value - min_val) / (max_val - min_val) * (track_x2 - track_x1)

    Затем кликает прямо по треку — бегунок прыгнет туда.
    Если нужно точнее — используется медленный drag от текущего положения.
    """
    if max_val <= min_val:
        return
    ratio = max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))
    target_x = int(track_x1 + ratio * (track_x2 - track_x1))
    # Кликаем по треку — это самый надёжный способ на таких слайдерах
    pg.moveTo(target_x, track_y, duration=0.15)
    pg.click()
    _sleep(0.2)


def _read_slider_value_ocr(label_region: Tuple, lang: str = "rus+eng") -> Optional[float]:
    """
    Читает текущее значение слайдера через OCR области с подписью.
    Возвращает float или None если не удалось.
    """
    if not _OCR:
        return None
    text = _ocr(label_region, lang=lang)
    m = re.search(r"(\d+)", text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return None


# ─── Шаг 1: Перейти на вкладку «Предметы» ────────────────────────────────────

def navigate_to_items_tab(cfg: Dict) -> None:
    """Кликает «Предметы» в левом меню маркетплейса."""
    img = cfg.get("items_tab_img")
    if img:
        pos = _click_img(img, confidence=0.82, timeout=2.0)
        if pos:
            _sleep(_c(cfg, "item_nav_delay", 0.7))
            return

    x = int(_c(cfg, "items_tab_x", MENU_ITEMS_X))
    y = int(_c(cfg, "items_tab_y", MENU_ITEMS_Y))
    _click(x, y, _c(cfg, "item_nav_delay", 0.7))
    _log(cfg, f"[actions] → Предметы ({x},{y})")


# ─── Шаг 2–3: Поиск предмета ─────────────────────────────────────────────────

def _pynput_press_key(kb, key) -> None:
    """pynput: нажать + отпустить специальную клавишу."""
    from pynput.keyboard import Key as _Key
    kb.press(key)
    _sleep(0.03)
    kb.release(key)
    _sleep(0.03)


def _search_clear_field(cfg: Dict) -> None:
    """
    Очищает поле поиска через End + N нажатий Backspace.

    Почему Ctrl+A не работает: игра обрабатывает Ctrl+A как свою горячую клавишу,
    а игнорирует Ctrl+V и Ctrl+A в контексте текстовых полей.
    Backspace же работает (ввод через pynput работает — значит Backspace тоже будет работать).

    Стратегия: End (перейти в конец) + 100 Backspace (стереть всё).
    100 = запас с запасом, самое длинное название ~ 40 символов.
    """
    if _PYNPUT and _KbController is not None:
        try:
            from pynput.keyboard import Key as _Key
            kb = _KbController()
            # End — перейти в конец текста
            _pynput_press_key(kb, _Key.end)
            _sleep(0.05)
            # 100 Backspace — стереть всё справа налево
            for _ in range(100):
                kb.press(_Key.backspace)
                kb.release(_Key.backspace)
            _sleep(0.15)
            return
        except Exception:
            pass
    # Fallback: pyautogui — End + 100 Backspace
    pg.press("end")
    _sleep(0.05)
    for _ in range(100):
        pg.press("backspace")
    _sleep(0.15)


def _search_type_text(text: str, cfg: Dict) -> None:
    """
    Вводит текст в поисковое поле посимвольно через pynput (WM_CHAR события).
    Это единственный надёжный способ для кириллицы в браузерных играх —
    игра игнорирует Ctrl+V (буфер обмена), но принимает реальные key events.

    Порядок приоритетов:
    1. pynput Controller.type() — посимвольный ввод, поддерживает Unicode/кириллицу
    2. keyboard.write() — альтернатива если pynput недоступен
    3. pg.typewrite() — только ASCII, fallback крайнего случая
    """
    interval = float(_c(cfg, "type_interval", 0.04))  # задержка между символами

    # ── Способ 1: pynput (самый надёжный для кириллицы) ──
    if _PYNPUT and _KbController is not None:
        try:
            kb = _KbController()
            for ch in text:
                kb.type(ch)
                _sleep(interval)
            return
        except Exception:
            pass  # При ошибке — пробуем следующий способ

    # ── Способ 2: keyboard.write() ──
    if _KEYBOARD_LIB and _keyboard is not None:
        try:
            _keyboard.write(text, delay=interval)
            return
        except Exception:
            pass

    # ── Способ 3: pg.typewrite (только ASCII, крайний fallback) ──
    pg.typewrite(text, interval=interval)


def search_item(cfg: Dict, item_name: str) -> None:
    """
    Кликает в строку поиска и вводит название предмета.
    Строка поиска находится в верхней части страницы Предметов (~456, 47).
    """
    x = int(_c(cfg, "search_x", SEARCH_X))
    y = int(_c(cfg, "search_y", SEARCH_Y))

    img = cfg.get("search_field_img")
    if img:
        pos = _find_img(img, confidence=0.80, timeout=2.0)
        if pos:
            x, y = pos[0], pos[1]

    # Пауза после навигации — ждём пока страница предметов полностью загрузится
    _sleep(float(_c(cfg, "item_nav_extra_delay", 0.8)))

    # Одиночный клик — фокусируем поле поиска
    _click(x, y, 0.35)

    # Очищаем поле через pynput (Ctrl+A + Backspace) —
    # не используем pyautogui чтобы не терять фокус в игре
    _search_clear_field(cfg)

    # Посимвольный ввод через pynput — как ручной ввод с клавиатуры
    # Игра принимает WM_CHAR события, но игнорирует Ctrl+V (буфер обмена)
    _search_type_text(item_name, cfg)

    # Пауза после ввода — ждём пока UI отфильтрует карточки
    # Время ввода = len(item_name) * interval плюс запас 0.8с
    _sleep(float(_c(cfg, "search_type_delay", 0.8)))
    _log(cfg, f"[actions] Поиск: '{item_name}'")

    # Дополнительная пауза пока результаты поиска появятся
    _sleep(float(_c(cfg, "search_results_delay", 0.8)))


# ─── Шаг 4: Кликнуть на найденный предмет ────────────────────────────────────

def click_found_item(cfg: Dict, item_name: str) -> bool:
    """
    Кликает на карточку найденного предмета.

    Стратегии (в порядке приоритета):
    1. PNG-шаблон из папки item_img_dir/{item_name}.png
    2. OCR области карточек
    3. Первая карточка по фиксированным координатам (~345, 190)
    """
    import os

    # ── Стратегия 1: PNG-шаблон предмета ──
    img_dir = cfg.get("item_img_dir", "items")
    img_path = os.path.join(img_dir, f"{item_name}.png")
    if os.path.isfile(img_path):
        region = tuple(_c(cfg, "cards_region", (225, 80, 1415, 760))) or None
        pos = _find_img(
            img_path,
            confidence=float(_c(cfg, "item_img_confidence", 0.78)),
            timeout=float(_c(cfg, "item_find_timeout", 3.0)),
            region=region,
        )
        if pos:
            _click(pos[0], pos[1], float(_c(cfg, "item_click_delay", 0.8)))
            _log(cfg, f"[actions] Найден по шаблону: {item_name}")
            return True

    # ── Стратегия 2: OCR карточек ──
    if _OCR:
        cards_region = tuple(_c(cfg, "cards_region", (225, 80, 1415, 760)))
        data = pytesseract.image_to_data(
            _grab(cards_region).convert("L"),
            lang="rus+eng", config="--psm 6",
            output_type=pytesseract.Output.DICT,
        )
        target_lower = item_name.lower()
        for i, word in enumerate(data["text"]):
            if target_lower in word.lower():
                cx = data["left"][i] + data["width"][i] // 2 + cards_region[0]
                cy = data["top"][i] + data["height"][i] // 2 + cards_region[1]
                _click(cx, cy, float(_c(cfg, "item_click_delay", 0.8)))
                _log(cfg, f"[actions] Найден по OCR: {item_name} ({cx},{cy})")
                return True

    # ── Стратегия 3: первая карточка ──
    x = int(_c(cfg, "first_card_x", CARD_X))
    y = int(_c(cfg, "first_card_y", CARD_Y))
    _click(x, y, float(_c(cfg, "item_click_delay", 0.8)))
    _log(cfg, f"[actions] Клик на первую карточку ({x},{y})")
    return True


# ─── Шаг 5: Читать таблицу продавцов ─────────────────────────────────────────

def read_sellers_table(cfg: Dict) -> List[Dict[str, Any]]:
    """
    Читает таблицу продавцов на открытой странице предмета.

    Колонки таблицы (1456×804):
      ID | Имя игрока | Static | Кол-во | Цена за 1 шт. | Таймер | Действие
      X:  ~680         ~810     ~920     ~1020           ~1130    ~1225   ~1350
      Y строк: 141, 176, 211, 246, 281, 316, 351, 386 (шаг ~35px)

    Возвращает список словарей, отсортированных по цене:
      [{"player": str, "price": float, "qty": int, "is_ours": bool, "row_y": int}, ...]
    """
    region = tuple(_c(cfg, "sellers_region", TABLE_REGION))
    sellers: List[Dict] = []

    if not _OCR:
        # Без OCR — пиксельный метод:
        # 1) Ищем наши строки по красному тексту «Удалить лот»
        # 2) Ищем чужие строки по наличию светлого текста в колонке «Имя игрока»
        action_x   = int(_c(cfg, "table_action_x",  TABLE_ACTION_X))
        player_x   = int(_c(cfg, "table_player_x",  TABLE_PLAYER_X))

        for row_y in TABLE_ROW_Y:
            is_ours = _is_delete_row(action_x, row_y)

            if is_ours:
                # Наша строка — добавляем
                sellers.append({
                    "player": "наш_игрок",
                    "price": 0.0,
                    "qty":   1,
                    "is_ours": True,
                    "row_y":   row_y,
                })
            else:
                # Проверяем есть ли чужая строка — светлый текст в колонке игрока
                try:
                    reg = (player_x - 80, row_y - 10, player_x + 80, row_y + 10)
                    img_row = _grab(reg)
                    pixels  = list(img_row.getdata())
                    # Светлые пиксели (R,G,B > 180) = текст присутствует
                    light = sum(1 for r, g, b in pixels if r > 180 and g > 180 and b > 180)
                    if light > 5:
                        sellers.append({
                            "player": "",    # имя неизвестно без OCR
                            "price":  0.0,   # цена неизвестна без OCR
                            "qty":    1,
                            "is_ours": False,
                            "row_y":   row_y,
                        })
                except Exception:
                    pass

        return sellers

    # OCR всей таблицы
    img = _grab(region)
    img_gray = img.convert("L")
    img_enh = ImageEnhance.Contrast(img_gray).enhance(2.5)
    img_bin = img_enh.point(lambda p: 255 if p > 130 else 0)

    data = pytesseract.image_to_data(
        img_bin, lang="rus+eng", config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )

    row_map: Dict[int, Dict] = {}  # row_y -> seller dict
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        wx = data["left"][i] + region[0]
        wy = data["top"][i] + region[1]

        # Определяем строку (ближайшая row_y)
        row_y = min(TABLE_ROW_Y, key=lambda ry: abs(wy - ry))
        if abs(wy - row_y) > 20:
            continue

        if row_y not in row_map:
            row_map[row_y] = {"player": "", "price": 0.0, "qty": 1, "is_ours": False, "row_y": row_y}

        # Имя игрока
        if abs(wx - int(_c(cfg, "table_player_x", TABLE_PLAYER_X))) < 80:
            row_map[row_y]["player"] += (" " + word).strip()

        # Цена
        if abs(wx - int(_c(cfg, "table_price_x", TABLE_PRICE_X))) < 80:
            m = re.search(r"[\d.,]+", word)
            if m:
                try:
                    row_map[row_y]["price"] = float(m.group().replace(",", "."))
                except Exception:
                    pass

        # Кол-во
        if abs(wx - int(_c(cfg, "table_qty_x", TABLE_QTY_X))) < 60:
            m = re.search(r"\d+", word)
            if m:
                try:
                    row_map[row_y]["qty"] = int(m.group())
                except Exception:
                    pass

    # Проверяем is_ours пиксельным методом
    for row_y, s in row_map.items():
        s["is_ours"] = _is_delete_row(
            int(_c(cfg, "table_action_x", TABLE_ACTION_X)), row_y
        )

    sellers = sorted(row_map.values(), key=lambda s: s["price"])
    return sellers


def _is_delete_row(action_x: int, row_y: int) -> bool:
    """
    Проверяет, является ли строка нашей (по наличию красного текста «Удалить лот»
    в колонке «Действие»).

    Метод: пиксель пиксель.  Красный текст = R > 180, G < 80, B < 80.
    """
    region = (action_x - 60, row_y - 12, action_x + 60, row_y + 12)
    try:
        img = _grab(region)
        pixels = list(img.getdata())
        red_count = sum(
            1 for r, g, b in pixels
            if r > 180 and g < 80 and b < 80
        )
        return red_count > 15
    except Exception:
        return False


# ─── Шаг 6: Удаление лота ──────────────────────────────────────────────────────────────

def delete_lot_from_table(cfg: Dict, row_y: int) -> None:
    """
    Клик «Удалить лот» в таблице (красный текст) для заданной строки.
    Затем вызывает confirm_delete().
    """
    x = int(_c(cfg, "table_action_x", TABLE_ACTION_X))
    _click(x, row_y, _c(cfg, "delete_click_delay", 0.5))
    _log(cfg, f"[actions] Клик «Удалить лот» в строке y={row_y}")
    confirm_delete(cfg)


def delete_lot_from_page(cfg: Dict) -> None:
    """
    Нажать кнопку корзины («Удалить лот с продажи») на странице предмета.
    Используется, если лот не виден в таблице.
    """
    img = cfg.get("delete_lot_btn_img")
    if img:
        pos = _click_img(img, confidence=0.82, timeout=2.0)
        if pos:
            _log(cfg, f"[actions] Кнопка корзины найдена по шаблону")
            confirm_delete(cfg)
            return

    x = int(_c(cfg, "delete_lot_btn_x", DELETE_LOT_BTN_X))
    y = int(_c(cfg, "delete_lot_btn_y", DELETE_LOT_BTN_Y))
    _click(x, y, _c(cfg, "delete_click_delay", 0.5))
    _log(cfg, f"[actions] Кнопка корзины ({x},{y})")
    confirm_delete(cfg)


def confirm_delete(cfg: Dict) -> None:
    """
    Ожидает диалог и нажимает «Удалить объявление».
    Приоритет: PNG-шаблон → OCR → фиксированные координаты.
    """
    _sleep(float(_c(cfg, "dialog_wait", 0.6)))

    # ── Попытка 1: по шаблону ──
    img = cfg.get("confirm_delete_btn_img")
    if img:
        pos = _click_img(img, confidence=0.82, timeout=2.0)
        if pos:
            _log(cfg, f"[actions] Подтверждение удаления по шаблону")
            _sleep(_c(cfg, "after_delete_delay", 0.8))
            return

    # ── Попытка 2: OCR диалога ──
    if _OCR:
        dialog_region = tuple(_c(cfg, "dialog_region", DIALOG_REGION))
        text = _ocr(dialog_region)
        if "удалить" in text.lower() or "delete" in text.lower():
            # ищем красную кнопку
            data_d = pytesseract.image_to_data(
                _grab(dialog_region).convert("L"),
                lang="rus+eng", config="--psm 6",
                output_type=pytesseract.Output.DICT,
            )
            for i, w in enumerate(data_d["text"]):
                if "удалить" in w.lower() or "delete" in w.lower():
                    bx = data_d["left"][i] + data_d["width"][i] // 2 + dialog_region[0]
                    by = data_d["top"][i] + data_d["height"][i] // 2 + dialog_region[1]
                    _click(bx, by)
                    _log(cfg, f"[actions] Подтверждение по OCR ({bx},{by})")
                    _sleep(_c(cfg, "after_delete_delay", 0.8))
                    return

    # ── Попытка 3: фиксированные координаты ──
    x = int(_c(cfg, "confirm_delete_btn_x", CONFIRM_DELETE_BTN_X))
    y = int(_c(cfg, "confirm_delete_btn_y", CONFIRM_DELETE_BTN_Y))
    _click(x, y, _c(cfg, "after_delete_delay", 0.8))
    _log(cfg, f"[actions] Подтверждение ({x},{y})")


# ─── Шаг 7: Нажать «Добавить лот на продажу» ───────────────────────────────────

def click_add_lot_btn(cfg: Dict) -> None:
    """Нажать кнопку «Добавить лот на продажу» (sinяя кнопка под картинкой)."""
    img = cfg.get("add_lot_btn_img")
    if img:
        pos = _click_img(img, confidence=0.82, timeout=2.0)
        if pos:
            _log(cfg, "[actions] Кнопка Добавить лот по шаблону")
            _sleep(_c(cfg, "add_lot_btn_delay", 0.6))
            return

    x = int(_c(cfg, "add_lot_btn_x", ADD_LOT_BTN_X))
    y = int(_c(cfg, "add_lot_btn_y", ADD_LOT_BTN_Y))
    _click(x, y, _c(cfg, "add_lot_btn_delay", 0.6))
    _log(cfg, f"[actions] Добавить лот ({x},{y})")


# ─── Шаг 8: Заполнить и отправить форму ─────────────────────────────────────────

def fill_and_submit_lot_form(
    cfg: Dict,
    price: float,
    qty: int = 1,
) -> None:
    """
    Заполняет форму «Выставить лот»:
      - Поле цены
      - Слайдер кол-ва
      - Слайдер часов (максимум)
      - Кнопка «Оплатить наличными»
    """
    _sleep(float(_c(cfg, "form_open_wait", 0.5)))

    # ── Поле цены ──
    px = int(_c(cfg, "form_price_x", FORM_PRICE_X))
    py = int(_c(cfg, "form_price_y", FORM_PRICE_Y))
    _clear_type(px, py, f"{price:.0f}")
    _log(cfg, f"[actions] Цена введена: {price:.0f}")

    # ── Слайдер кол-ва ──
    # Определяем макс. доступное кол-во через OCR (опционально)
    qty_max_region = _c(cfg, "form_qty_max_region", None)
    qty_max = float(_c(cfg, "form_qty_max", 1))
    if qty_max_region and _OCR:
        v = _read_slider_value_ocr(tuple(qty_max_region))
        if v is not None:
            qty_max = v

    qty_val = max(1.0, min(float(qty), qty_max))
    _drag_slider(
        int(_c(cfg, "form_qty_track_x1", FORM_QTY_TRACK_X1)),
        int(_c(cfg, "form_qty_track_x2", FORM_QTY_TRACK_X2)),
        int(_c(cfg, "form_qty_track_y",  FORM_QTY_TRACK_Y)),
        value=qty_val,
        min_val=1.0,
        max_val=qty_max,
    )
    _log(cfg, f"[actions] Слайдер кол-ва: {qty_val:.0f} / {qty_max:.0f}")

    # ── Слайдер часов (максимум) ──
    hours_max = float(_c(cfg, "form_hours_max", FORM_HOURS_MAX))
    _drag_slider(
        int(_c(cfg, "form_hours_track_x1", FORM_HOURS_TRACK_X1)),
        int(_c(cfg, "form_hours_track_x2", FORM_HOURS_TRACK_X2)),
        int(_c(cfg, "form_hours_track_y",  FORM_HOURS_TRACK_Y)),
        value=hours_max,
        min_val=1.0,
        max_val=hours_max,
    )
    _log(cfg, f"[actions] Слайдер часов: {hours_max:.0f}")

    # ── Кнопка подтверждения ──
    img = cfg.get("form_submit_img")
    if img:
        pos = _click_img(img, confidence=0.82, timeout=2.0)
        if pos:
            _log(cfg, "[actions] Оплатить наличными по шаблону")
            _sleep(_c(cfg, "after_submit_delay", 1.0))
            return

    sx = int(_c(cfg, "form_submit_x", FORM_SUBMIT_X))
    sy = int(_c(cfg, "form_submit_y", FORM_SUBMIT_Y))
    _click(sx, sy, _c(cfg, "after_submit_delay", 1.0))
    _log(cfg, f"[actions] Оплатить наличными ({sx},{sy})")


# ─── Проверка позиции ────────────────────────────────────────────────────────────

def check_our_position(
    sellers: List[Dict[str, Any]],
    our_player: str,
    min_price: float = 0.0,
) -> Tuple[int, float, str]:
    """
    Возвращает (rank, our_price, status).

    rank=0  — наш лот не найден.
    rank=1  — мы первые.
    rank>1  — нас перебили.

    min_price: если лучшая цена рынка < min_price, возвращает PRICE_FLOOR.
    """
    from item_sale_monitor import (
        STATUS_TOP, STATUS_OUTBID, STATUS_NO_LOT, STATUS_PRICE_FLOOR
    )

    our_idx: Optional[int] = None
    for i, s in enumerate(sellers):
        name = s.get("player", "")
        if our_player.lower() in name.lower() or s.get("is_ours", False):
            our_idx = i
            break

    if our_idx is None:
        return (0, 0.0, STATUS_NO_LOT)

    rank       = our_idx + 1
    our_price  = sellers[our_idx]["price"]
    best_price = sellers[0]["price"] if sellers else our_price

    if rank == 1:
        return (rank, our_price, STATUS_TOP)

    # Нас перебили — проверяем min_price
    if min_price > 0 and best_price <= min_price:
        return (rank, our_price, STATUS_PRICE_FLOOR)

    return (rank, our_price, STATUS_OUTBID)


# ─── Полный цикл мониторинга ────────────────────────────────────────────────────────

_OCR_WARN_LOGGED = False  # предупреждение выводится только один раз чтобы не спамить лог

def full_monitor_cycle(
    tracked_items: List[Any],  # List[TrackedItem]
    cfg: Dict[str, Any],
    log: Any,
    stop_event: Any,
) -> List[Any]:  # List[ItemCheckResult]
    """
    Полный цикл мониторинга для нескольких предметов.

    Для каждого TrackedItem:
      1. Навигация на вкладку Предметы
      2. Поиск предмета
      3. Клик на карточку
      4. Чтение таблицы
      5. Проверка позиции
      6. Если OUTBID: удалить и перевыставить
      7. Если NO_LOT: выставить новый

    Возвращает List[ItemCheckResult].
    """
    from item_sale_monitor import (
        ItemCheckResult,
        STATUS_TOP, STATUS_OUTBID, STATUS_NOT_FOUND,
        STATUS_NO_LOT, STATUS_ERROR, STATUS_PRICE_FLOOR,
    )

    # Предупреждаем один раз если tesseract недоступен
    global _OCR_WARN_LOGGED
    if not _OCR and not _OCR_WARN_LOGGED:
        log(
            "[actions] ⚠️ OCR недоступен (tesseract не установлен или не в PATH). "
            "Поиск предметов будет работать по шаблонам и первой карточке (fallback). "
            "Для точного OCR: https://github.com/UB-Mannheim/tesseract/wiki"
        )
        _OCR_WARN_LOGGED = True

    results: List[ItemCheckResult] = []

    for item in tracked_items:
        if stop_event.is_set():
            break

        result = ItemCheckResult(item_name=item.name, status=STATUS_ERROR)

        try:
            cfg["_log_fn"] = log

            # Шаг 1–2: навигация + поиск
            navigate_to_items_tab(cfg)
            search_item(cfg, item.name)

            # Шаг 3: клик на предмет
            found = click_found_item(cfg, item.name)
            if not found:
                result.status = STATUS_NOT_FOUND
                results.append(result)
                log(f"[monitor] {item.name}: НЕ НАЙДЕН")
                continue

            # Шаг 4: чтение таблицы
            sellers = read_sellers_table(cfg)
            if not sellers:
                result.status = STATUS_NOT_FOUND
                results.append(result)
                log(f"[monitor] {item.name}: таблица пуста")
                continue

            # Без OCR: цены неизвестны — ограниченный режим
            no_price_data = not _OCR

            # Шаг 5: проверка позиции
            rank, our_price, status = check_our_position(
                sellers, item.our_player, min_price=item.min_price
            )

            # Определяем лучшую цену: без OCR — проверяем только через позицию
            our_sellers = [s for s in sellers if s.get("is_ours")]
            other_sellers = [s for s in sellers if not s.get("is_ours")]

            best = sellers[0] if sellers else {}
            result.status      = status
            result.our_price   = our_price
            result.best_price  = best.get("price", 0.0)
            result.best_seller = best.get("player", "")
            result.rank        = rank

            if no_price_data:
                # Без OCR: знаем только есть ли мы в таблице и есть ли конкуренты
                has_ours   = bool(our_sellers)
                has_others = bool(other_sellers)
                if has_ours:
                    # Наш лот есть, но неизвестно перебили ли нас
                    if has_others:
                        # Есть конкуренты — не можем определить без OCR перебили ли
                        result.status = STATUS_TOP  # трактуем как ТОП (не перевыставляем без цены)
                        log(f"[monitor] {item.name}: наш лот + конкуренты — без OCR позицию не определить")
                    else:
                        result.status = STATUS_TOP
                        log(f"[monitor] {item.name}: наш лот в таблице, конкурентов нет")
                else:
                    if has_others:
                        # Нашего лота нет, но есть чужие — без OCR не выставляем (неизвестна цена)
                        result.status = STATUS_NO_LOT
                        log(f"[monitor] {item.name}: нашего лота нет, есть конкуренты — без OCR репост невозможен")
                    else:
                        result.status = STATUS_NO_LOT
                        log(f"[monitor] {item.name}: таблица пуста (нет лотов)")
                results.append(result)
                continue

            log(f"[monitor] {item.name}: {status} rank={rank} "
                f"our={our_price} best={result.best_price}")

            # Шаг 6–7: действие при необходимости
            if status == STATUS_OUTBID:
                new_price = max(
                    item.min_price,
                    result.best_price - float(cfg.get("price_step", 1)),
                )
                if item.min_price > 0 and new_price <= item.min_price:
                    result.status = STATUS_PRICE_FLOOR
                    log(f"[monitor] {item.name}: опускаемся до min_price={item.min_price}")
                else:
                    _do_repost(cfg, item, sellers, new_price, log)
                    result.reposted = True
                    result.our_price = new_price

            elif status == STATUS_NO_LOT:
                new_price = item.target_price
                if item.min_price > 0 and new_price < item.min_price:
                    new_price = item.min_price
                _do_repost(cfg, item, sellers, new_price, log, delete_first=False)
                result.reposted = True
                result.our_price = new_price

        except Exception as exc:
            import traceback
            result.status = STATUS_ERROR
            result.reason = str(exc)
            log(f"[monitor] {item.name} ошибка: {exc}\n{traceback.format_exc()}")

        results.append(result)

    return results


def _do_repost(
    cfg: Dict,
    item: Any,
    sellers: List[Dict],
    new_price: float,
    log: Any,
    delete_first: bool = True,
) -> None:
    """Внутренняя: удалить старый лот и выставить новый."""
    if delete_first:
        our_rows = [
            s["row_y"] for s in sellers
            if s.get("is_ours") or item.our_player.lower() in s.get("player", "").lower()
        ]
        if our_rows:
            delete_lot_from_table(cfg, our_rows[0])
        else:
            delete_lot_from_page(cfg)

    click_add_lot_btn(cfg)
    fill_and_submit_lot_form(cfg, price=new_price, qty=int(cfg.get("lot_qty", 1)))
    log(f"[monitor] Репост: {item.name} по цене {new_price:.0f}")


# ─── Дополнительно: чтение кол-ва из формы ─────────────────────────────────────

def read_available_qty(cfg: Dict) -> int:
    """
    Опционально: читает доступное кол-во из текста над слайдером («Доступно к продаже»).
    Возвращает 1 если не удалось прочитать.
    """
    region = _c(cfg, "form_qty_avail_region", None)
    if not region:
        return 1
    if not _OCR:
        return 1
    text = _ocr(tuple(region))
    m = re.search(r"\d+", text)
    if m:
        try:
            best_qty = int(m.group())
            return best_qty
        except Exception:
            pass
    return 1


def get_best_qty_from_table(sellers: List[Dict], our_player: str) -> int:
    """
    Возвращает кол-во из лучшего лота (rank=1).
    Если мы уже первые — возвращает наше кол-во.
    """
    if not sellers:
        return 1
    best_qty = sellers[0].get("qty", 1)
    our_lower = our_player.lower()
    for s in sellers:
        try:
            if our_lower in s.get("player", "").lower() or s.get("is_ours"):
                best_qty  = s.get("qty", best_qty)
        except Exception:
            pass
    return best_qty
