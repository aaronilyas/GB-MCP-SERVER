"""Optional OCR on returned PNGs only. Missing engines must not fail input."""

from __future__ import annotations

import io
from typing import Any


def ocr_pngs(pngs: list[bytes]) -> dict[str, Any]:
    """OCR up to a few PNGs. Prefer the last (usually the useful screen)."""
    if not pngs:
        return {"ocr_text": "", "ocr_engine": "none", "ocr_error": None}
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return {"ocr_text": None, "ocr_engine": None, "ocr_error": "disabled"}

    texts: list[str] = []
    try:
        for png in pngs[-2:]:
            image = Image.open(io.BytesIO(png))
            if image.mode != "RGB":
                image = image.convert("RGB")
            texts.append(pytesseract.image_to_string(image) or "")
    except Exception as exc:  # noqa: BLE001
        return {"ocr_text": None, "ocr_engine": "pytesseract", "ocr_error": str(exc) or "disabled"}
    return {
        "ocr_text": "\n".join(part.strip() for part in texts if part.strip()),
        "ocr_engine": "pytesseract",
        "ocr_error": None,
    }


def ocr_textbox_png(png: bytes) -> str | None:
    """OCR the inner Gen 1 textbox (y>=96). Missing engines return None."""
    if not png:
        return None
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return None
    try:
        image = Image.open(io.BytesIO(png))
        if image.mode != "RGB":
            image = image.convert("RGB")
        width, height = image.size
        # Native 160×144 inner window; scaled previews keep the same fractions.
        y0 = max(0, int(round(height * 102 / 144)))
        y1 = min(height, int(round(height * 138 / 144)))
        x0 = max(0, int(round(width * 8 / 160)))
        x1 = min(width, int(round(width * 152 / 160)))
        if y1 <= y0 or x1 <= x0:
            return None
        crop = image.crop((x0, y0, x1, y1))
        # Tesseract needs more than 8px glyphs; nearest-neighbor 3× is still LCD-faithful.
        scaled = crop.resize((crop.width * 3, crop.height * 3), Image.NEAREST)
        text = pytesseract.image_to_string(scaled) or ""
        cleaned = text.strip()
        return cleaned or None
    except Exception:
        return None
