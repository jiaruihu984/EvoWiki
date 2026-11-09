import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from evowiki.sources.images import MAX_EDGE, image_part, prepare_image
from evowiki.sources.parsed import ParseError


def png(w, h):
    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_large_png_is_scaled_to_max_edge():
    media, out = prepare_image(png(3000, 2000))
    img = Image.open(BytesIO(out))
    assert media == "image/png" and img.size == (MAX_EDGE, round(2000 * MAX_EDGE / 3000))


def test_small_image_is_untouched():
    data = png(100, 80)
    assert prepare_image(data) == ("image/png", data)


def test_bad_bytes_raise_parse_error():
    with pytest.raises(ParseError, match="这张图片打不开"):
        prepare_image(b"not an image")


def test_image_part_hashes_the_original_bytes():
    data = png(3000, 2000)
    part = image_part(data, "截图.png")
    assert part.sha256 == hashlib.sha256(data).hexdigest() and part.name == "截图.png"
    assert Image.open(BytesIO(base64.b64decode(part.data_b64))).size[0] == MAX_EDGE
