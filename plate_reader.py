import importlib
import importlib.util
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

try:
    from PIL import ImageOps
except Exception:
    ImageOps = None

_LAST_MANUAL: dict = {"ts": 0.0, "text": ""}

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

_PLATE_SIMILAR_CHARS = {
    "O": ("O", "0"),
    "0": ("0", "O"),
    "B": ("B", "8"),
    "8": ("8", "B"),
    "Z": ("Z", "2"),
    "2": ("2", "Z"),
    "I": ("I", "1"),
    "1": ("1", "I"),
    "S": ("S", "5"),
    "5": ("5", "S"),
}


def _normalize_plate(text: str) -> str:
    if not text:
        return ""
    text = text.upper()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9A-ZА-Я]", "", text)
    normalized = "".join(_PLATE_CYR_TO_LAT.get(ch, ch) for ch in text)
    return normalized


def _expand_similar_variants(text: str, max_variants: int = 64) -> Iterable[str]:
    variants = [""]
    for ch in text:
        replacements = _PLATE_SIMILAR_CHARS.get(ch, (ch,))
        next_variants = []
        for base in variants:
            for repl in replacements:
                next_variants.append(base + repl)
                if len(next_variants) >= max_variants:
                    break
            if len(next_variants) >= max_variants:
                break
        variants = next_variants
        if len(variants) >= max_variants:
            break
    return variants


def _is_plate_format(candidate: str) -> bool:
    if not candidate:
        return False
    patterns = (
        r"^[A-ZА-Я]{1,2}\d{3}[A-ZА-Я]{2}\d{2,3}$",
        r"^[A-ZА-Я]{1,2}\d{3}[A-ZА-Я]{2}$",
        r"^\d{3}[A-ZА-Я]{2}\d{2,3}$",
        r"^\d[A-ZА-Я]{3,4}$",
        r"^[A-ZА-Я]\d[A-ZА-Я]{2,3}$",
    )
    return any(re.match(pat, candidate) for pat in patterns)


def _extract_plate(text: str) -> Optional[str]:
    cleaned = _normalize_plate(text)
    if not cleaned:
        return None
    cleaned = cleaned.replace("ГОСНОМЕР", "")
    candidates = []
    patterns = (
        r"[A-ZА-Я]{1,2}\d{3}[A-ZА-Я]{2}\d{2,3}",
        r"[A-ZА-Я]{1,2}\d{3}[A-ZА-Я]{2}",
        r"\d{3}[A-ZА-Я]{2}\d{2,3}",
        r"\d[A-ZА-Я]{3,4}",
        r"[A-ZА-Я]\d[A-ZА-Я]{2,3}",
    )
    for variant in _expand_similar_variants(cleaned):
        for pat in patterns:
            for match in re.finditer(pat, variant):
                candidates.append(match.group(0))
    if candidates:
        return max(candidates, key=len)
    if any(ch.isdigit() for ch in cleaned):
        return cleaned
    return None


def _select_best_plate(candidates: Iterable[Tuple[str, str]]) -> Tuple[Optional[str], Optional[str]]:
    best_plate = None
    best_text = None
    best_score = (-1, -1)
    for text, source in candidates:
        candidate = _extract_plate(text)
        if not candidate:
            continue
        score = (1 if _is_plate_format(candidate) else 0, len(candidate))
        if score > best_score:
            best_plate = candidate
            best_text = text
            best_score = score
    return best_plate, best_text


def _get_tesseract():
    if importlib.util.find_spec("pytesseract") is None:
        return None
    return importlib.import_module("pytesseract")


def _capture_roi(roi: Tuple[int, int, int, int]):
    if importlib.util.find_spec("pyautogui") is None:
        return None
    pyautogui = importlib.import_module("pyautogui")
    try:
        return pyautogui.screenshot(region=roi)
    except Exception:
        return None


def _prompt_plate_manual(default_text: str) -> Optional[str]:
    if importlib.util.find_spec("tkinter") is None:
        return None
    tk = importlib.import_module("tkinter")
    simpledialog = importlib.import_module("tkinter.simpledialog")
    root = None
    try:
        try:
            ttl = float(os.getenv("PLATE_READ_MANUAL_CACHE_TTL", "120"))
        except Exception:
            ttl = 120.0
        if ttl > 0:
            try:
                last_text = str(_LAST_MANUAL.get("text") or "").strip()
                last_ts = float(_LAST_MANUAL.get("ts") or 0.0)
                if last_text and (time.time() - last_ts) <= ttl:
                    return last_text
            except Exception:
                pass
        root = tk.Tk()
        root.withdraw()
        plate = simpledialog.askstring("Введите госномер", "Введите госномер", initialvalue=default_text)
        if plate is None:
            return None
        plate = str(plate).strip()
        if plate:
            try:
                _LAST_MANUAL["text"] = plate
                _LAST_MANUAL["ts"] = time.time()
            except Exception:
                pass
        return plate or None
    except Exception:
        return None
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def _prepare_ocr_variants(image) -> Iterable:
    variants = []
    base_images = []
    if ImageOps is not None:
        try:
            pad = int(os.getenv("PLATE_READ_OCR_PAD", "2"))
        except Exception:
            pad = 2
        if pad > 0:
            try:
                base_images.append(ImageOps.expand(image, border=pad, fill=0))
                base_images.append(ImageOps.expand(image, border=pad, fill=255))
            except Exception:
                base_images.append(image)
        else:
            base_images.append(image)
    else:
        base_images.append(image)
    for base_image in base_images:
        variants.append(base_image)
    if ImageOps is None:
        return variants
    try:
        for base_image in base_images:
            gray = ImageOps.grayscale(base_image)
            variants.append(gray)
            variants.append(ImageOps.autocontrast(gray))
            variants.append(ImageOps.equalize(gray))
    except Exception:
        return variants
    try:
        inv = ImageOps.invert(gray)
        variants.append(inv)
        variants.append(ImageOps.autocontrast(inv))
        variants.append(ImageOps.equalize(inv))
    except Exception:
        pass
    try:
        thresholds = os.getenv("PLATE_READ_OCR_THRESHOLDS", "120,140,160")
        thr_values = []
        for part in str(thresholds).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                thr_values.append(int(part))
            except Exception:
                continue
        if not thr_values:
            thr_values = [120, 140, 160]
        for thr_value in thr_values:
            thr = gray.point(lambda p, t=thr_value: 255 if p > t else 0)
            variants.append(thr)
    except Exception:
        pass
    base_image = base_images[0] if base_images else image
    if importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None:
        import cv2
        import numpy as np
        gray_arr = None
        try:
            gray_arr = np.array(base_image.convert("L"))
        except Exception:
            gray_arr = None
        if gray_arr is not None:
            try:
                _thr, otsu = cv2.threshold(gray_arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                variants.append(otsu)
            except Exception:
                pass
            try:
                adaptive = cv2.adaptiveThreshold(
                    gray_arr, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 10
                )
                variants.append(adaptive)
            except Exception:
                pass
            try:
                adaptive_g = cv2.adaptiveThreshold(
                    gray_arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
                )
                variants.append(adaptive_g)
            except Exception:
                pass
            try:
                inv = cv2.bitwise_not(gray_arr)
                variants.append(inv)
            except Exception:
                pass
    try:
        scale = int(os.getenv("PLATE_READ_OCR_SCALE", "2"))
    except Exception:
        scale = 2
    if scale > 1:
        for img in list(variants):
            try:
                size = getattr(img, "size", None)
                if isinstance(size, tuple) and len(size) == 2:
                    w, h = size
                    if w > 0 and h > 0:
                        variants.append(img.resize((w * scale, h * scale)))
            except Exception:
                continue
    return variants


def _ocr_texts(tesseract,
               image,
               lang: str,
               psm_list: Iterable[str],
               config_base: str,
               short_psm_list: Optional[Iterable[str]] = None,
               short_min_len: int = 6,
               short_config_base: Optional[str] = None) -> Iterable[str]:
    seen = set()
    variants = list(_prepare_ocr_variants(image))
    short_mode_needed = False
    for psm in psm_list:
        config = f"--psm {psm} {config_base}".strip()
        for variant in variants:
            try:
                text = tesseract.image_to_string(variant, lang=lang, config=config) or ""
            except Exception:
                text = ""
            text = text.strip()
            if text:
                if len(text) < max(1, int(short_min_len)):
                    short_mode_needed = True
                if text not in seen:
                    seen.add(text)
                    yield text
    if short_psm_list and (short_mode_needed or not seen):
        base = short_config_base or config_base
        whitelist = os.getenv(
            "PLATE_READ_SHORT_WHITELIST",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ0123456789",
        )
        forced_base = f"{base} -c tessedit_char_whitelist={whitelist}".strip()
        for psm in short_psm_list:
            config = f"--psm {psm} {forced_base}".strip()
            for variant in variants:
                try:
                    text = tesseract.image_to_string(variant, lang=lang, config=config) or ""
                except Exception:
                    text = ""
                text = text.strip()
                if text and text not in seen:
                    seen.add(text)
                    yield text


@dataclass
class PlateReadResult:
    plate: Optional[str]
    confidence: Optional[float]
    raw_text: str
    method: str
    debug_path: Optional[str]


def read_plate_from_ui(screen, roi) -> PlateReadResult:
    """
    Read plate text from UI using OCR on the provided ROI.
    If screen is None, captures ROI from screen.
    """
    debug_path = None
    raw_text = ""
    method = "none"
    confidence = None

    if roi is None:
        return PlateReadResult(None, None, raw_text, method, debug_path)

    try:
        rx, ry, rw, rh = roi
        roi_tuple = (int(rx), int(ry), int(rw), int(rh))
    except Exception:
        return PlateReadResult(None, None, raw_text, method, debug_path)

    image = screen
    if image is None:
        image = _capture_roi(roi_tuple)

    if image is None:
        return PlateReadResult(None, None, raw_text, method, debug_path)

    tesseract = _get_tesseract()
    if tesseract is None:
        method = "none"
        if os.getenv("PLATE_READ_PROMPT_ON_FAIL", "1") == "1":
            manual = _prompt_plate_manual("")
            plate = _extract_plate(manual or "")
            return PlateReadResult(plate, None, manual or "", "manual", debug_path)
        return PlateReadResult(None, None, raw_text, method, debug_path)

    lang = os.getenv("PLATE_READ_OCR_LANG", "rus+eng")
    try:
        def _parse_psm_list(raw, fallback):
            items = []
            for item in str(raw).split(","):
                item = item.strip()
                if item:
                    items.append(item)
            return items or list(fallback)

        psm_list = _parse_psm_list(os.getenv("PLATE_READ_OCR_PSM_LIST", "7,6"), ["7", "6"])
        short_psm_list = _parse_psm_list(os.getenv("PLATE_READ_SHORT_PSM_LIST", "7,8"), ["7", "8"])
        try:
            short_min_len = int(os.getenv("PLATE_READ_SHORT_MIN_LEN", "6"))
        except Exception:
            short_min_len = 6
        config_base = "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ0123456789"
        texts = list(
            _ocr_texts(
                tesseract,
                image,
                lang=lang,
                psm_list=psm_list,
                config_base=config_base,
                short_psm_list=short_psm_list,
                short_min_len=short_min_len,
                short_config_base=config_base,
            )
        )
        raw_text = texts[0] if texts else ""
        method = "ocr"
    except Exception:
        raw_text = ""
        method = "ocr"

    plate = None
    def _consider_text(text: str, source: str) -> None:
        nonlocal plate, raw_text, method
        best_plate, best_text = _select_best_plate([(text, source)])
        if best_plate:
            plate = best_plate
            raw_text = best_text or text
            method = source

    if raw_text:
        _consider_text(raw_text, method)
    if "texts" in locals():
        for text in texts:
            _consider_text(text, "ocr")

    try:
        loose_enabled = os.getenv("PLATE_READ_OCR_LOOSE_ENABLED", "1") != "0"
    except Exception:
        loose_enabled = True
    if not plate and loose_enabled:
        loose_psm_list = _parse_psm_list(
            os.getenv("PLATE_READ_OCR_LOOSE_PSM_LIST", "7,8,6,11"),
            ["7", "8", "6", "11"],
        )
        loose_lang = os.getenv("PLATE_READ_OCR_LOOSE_LANG", lang)
        loose_config = os.getenv("PLATE_READ_OCR_LOOSE_CONFIG", "").strip()
        loose_texts = list(
            _ocr_texts(
                tesseract,
                image,
                lang=loose_lang,
                psm_list=loose_psm_list,
                config_base=loose_config,
                short_psm_list=None,
            )
        )
        if not raw_text and loose_texts:
            raw_text = loose_texts[0]
        for text in loose_texts:
            _consider_text(text, "ocr-loose")
    try:
        data_enabled = os.getenv("PLATE_READ_OCR_DATA_ENABLED", "1") != "0"
    except Exception:
        data_enabled = True
    if not plate and data_enabled:
        try:
            def _parse_psm_list(raw, fallback):
                items = []
                for item in str(raw).split(","):
                    item = item.strip()
                    if item:
                        items.append(item)
                return items or list(fallback)

            data_psm_list = _parse_psm_list(os.getenv("PLATE_READ_OCR_DATA_PSM_LIST", "6,7,11"), ["6", "7", "11"])
            data_config = os.getenv("PLATE_READ_OCR_DATA_CONFIG", "").strip()
            data_config = data_config or "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ0123456789"
            output = getattr(tesseract, "Output", None)
            data_candidates = []
            for psm in data_psm_list:
                config = f"--psm {psm} {data_config}".strip()
                for variant in _prepare_ocr_variants(image):
                    try:
                        if output is not None:
                            data = tesseract.image_to_data(variant, lang=lang, config=config, output_type=output.DICT)
                        else:
                            continue
                    except Exception:
                        continue
                    words = data.get("text", []) if isinstance(data, dict) else []
                    if not words:
                        continue
                    for word in words:
                        if not word:
                            continue
                        data_candidates.append((word, "ocr-data"))
            best_plate, best_text = _select_best_plate(data_candidates)
            if best_plate:
                plate = best_plate
                raw_text = best_text or raw_text
                method = "ocr-data"
        except Exception:
            pass
    if not plate and os.getenv("PLATE_READ_PROMPT_ON_FAIL", "1") == "1":
        manual = _prompt_plate_manual(raw_text)
        plate = _extract_plate(manual or "")
        if manual:
            raw_text = manual
        method = "manual"

    if not plate:
        try:
            debug_dir = Path(os.getenv("PLATE_READ_DEBUG_DIR", "."))
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out = debug_dir / f"plate_read_fail_{stamp}.png"
            image.save(out)
            debug_path = str(out)
        except Exception:
            debug_path = None

    return PlateReadResult(plate, confidence, raw_text, method, debug_path)
