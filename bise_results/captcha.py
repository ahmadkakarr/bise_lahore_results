"""Local OCR of the challenge rendered in the current browser session."""

from collections import defaultdict
from io import BytesIO
import re

from PIL import Image, ImageOps
import pytesseract
from pytesseract import Output


def recognize(png: bytes) -> dict:
    original = Image.open(BytesIO(png)).convert("RGB")
    # Keep text, discard the screenshot border and padding.
    cropped = original.crop((5, 5, original.width - 5, original.height - 5))
    variants = [cropped, ImageOps.grayscale(cropped).point(lambda p: 255 if p > 160 else 0)]
    votes = defaultdict(list)
    for variant in variants:
        image = variant.resize((variant.width * 4, variant.height * 4))
        data = pytesseract.image_to_data(
            image,
            config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            output_type=Output.DICT,
            nice=10,
            timeout=20,
        )
        tokens = [(text.strip(), float(conf)) for text, conf in zip(data["text"], data["conf"]) if text.strip()]
        candidate = "".join(text for text, _ in tokens)
        if re.fullmatch(r"[A-Z0-9]{6}", candidate):
            votes[candidate].append(min(conf for _, conf in tokens))
    if not votes:
        return {"text": None, "confidence": 0, "variants_agree": False}
    text, scores = max(votes.items(), key=lambda item: (len(item[1]), max(item[1])))
    return {"text": text, "confidence": max(scores), "variants_agree": len(scores) == len(variants)}
