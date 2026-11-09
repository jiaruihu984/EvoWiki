"""Images for the model: shrink to what Claude reads well, keep the original's sha for identity."""

import base64
import hashlib
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from evowiki.llm.chat import ImageMedia, ImagePart
from evowiki.sources.parsed import ParseError

MAX_EDGE = 1568
MAX_BYTES = 4_500_000
MSG_BAD_IMAGE = "这张图片打不开"

_FORMATS: dict[str, tuple[str, ImageMedia]] = {
    "PNG": ("PNG", "image/png"), "JPEG": ("JPEG", "image/jpeg"), "WEBP": ("WEBP", "image/webp"),
}


def _encode(img: Image.Image, fmt: str) -> bytes:
    buf = BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, "JPEG", quality=85)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def prepare_image(data: bytes) -> tuple[ImageMedia, bytes]:
    """Long edge above MAX_EDGE is scaled down in proportion; PNG/JPEG/WebP keep their format
    (anything else becomes PNG); a result over MAX_BYTES is saved as JPEG q=85 instead."""
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ParseError(MSG_BAD_IMAGE) from exc
    fmt, media = _FORMATS.get(img.format or "", ("PNG", "image/png"))
    resized = max(img.size) > MAX_EDGE
    if resized:
        scale = MAX_EDGE / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)
    elif img.format in _FORMATS and len(data) <= MAX_BYTES:
        return media, data   # already fine: send the bytes untouched
    try:
        out = _encode(img, fmt)
        if len(out) > MAX_BYTES:
            fmt, media = "JPEG", "image/jpeg"
            out = _encode(img, fmt)
    except (OSError, ValueError) as exc:
        raise ParseError(MSG_BAD_IMAGE) from exc
    return media, out


def image_part(data: bytes, name: str) -> ImagePart:
    media, out = prepare_image(data)
    return ImagePart(media_type=media, sha256=hashlib.sha256(data).hexdigest(),
                     data_b64=base64.b64encode(out).decode("ascii"), name=name)
