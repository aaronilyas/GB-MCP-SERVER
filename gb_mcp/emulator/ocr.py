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
    """OCR the inner textbox fill via vision.textbox_inner_rect. Missing engines → None."""
    if not png:
        return None
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return None
    try:
        from gb_mcp.emulator.play_limits import NATIVE_HEIGHT, NATIVE_WIDTH
        from gb_mcp.emulator.vision import textbox_inner_rect

        image = Image.open(io.BytesIO(png))
        if image.mode != "RGB":
            image = image.convert("RGB")
        # Classifier geometry is native 160×144; scale the crop if the PNG is upscaled.
        native = image
        if image.size != (NATIVE_WIDTH, NATIVE_HEIGHT):
            native = image.resize((NATIVE_WIDTH, NATIVE_HEIGHT), Image.NEAREST)
        rect = textbox_inner_rect(native)
        if rect is None:
            return None
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return None
        sx = image.width / float(NATIVE_WIDTH)
        sy = image.height / float(NATIVE_HEIGHT)
        crop = image.crop(
            (
                int(round(x * sx)),
                int(round(y * sy)),
                int(round((x + w) * sx)),
                int(round((y + h) * sy)),
            )
        )
        # Tesseract needs more than 8px glyphs; nearest-neighbor 3× is still LCD-faithful.
        scaled = crop.resize((crop.width * 3, crop.height * 3), Image.NEAREST)
        text = pytesseract.image_to_string(scaled) or ""
        cleaned = text.strip()
        return cleaned or None
    except Exception:
        return None
