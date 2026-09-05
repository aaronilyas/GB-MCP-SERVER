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
