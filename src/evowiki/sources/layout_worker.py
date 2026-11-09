"""Layout worker (MinerU or Docling) for `sources.layout_client.LayoutWorker`.

Runs under the layout venv's Python 3.12 (`EVOWIKI_LAYOUT_PYTHON`), NOT the service's: standard
library + `mineru` or `docling` (and their PIL) only, never `evowiki`. The backend comes from
`EVOWIKI_LAYOUT_BACKEND` ("mineru", the default, or "docling"). JSON lines:

    first line  <- {"ready": true, "backend": "mineru", "version": "4.0.12"}
                   (models loaded and the smoke read worked; else {"ready": false, "error"}, exit)
    page image  -> {"id": 1, "op": "image", "png_b64": "..."}
                <- {"id": 1, "ok": true, "markdown": "...", "blocks": [{"label": "chart", "area": 0.42}]}
    PDF batch   -> {"id": 2, "op": "pdf", "path": "C:/.../x.pdf", "first": 1, "last": 16}
                <- {"id": 2, "ok": true, "raw": <MinerU content list | DoclingDocument dict>}
    a failure   <- {"id": 1, "ok": false, "error": "..."}

`area` is the layout block's bbox area over the page area. Pages are 1-based and inclusive.
Embedded images are dropped from `raw` (`img_path` / `image` / `uri` set to ""). Before `ready`
the worker reads one tiny built-in image ("EvoWiki 123"): an error or no text there is a failed
start. A failing request answers ok:false and the worker keeps running; stdin EOF ends it. stdout
carries only protocol lines: the original stdout is kept for them and fd 1 is pointed at stderr,
so library prints (C code included) land in the log, not in the protocol.

Every MinerU / Docling import happens inside functions called from `main()`: on Windows MinerU
spawns worker processes, which re-import this module.
"""

import base64
import io
import json
import os
import re
import sys
import tempfile

SMOKE_TEXT = "EvoWiki 123"
_SCRUB = frozenset({"img_path", "image", "uri"})


def main() -> int:
    proto = open(os.dup(1), "w", encoding="utf-8", newline="\n")   # noqa: SIM115
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    backend = os.environ.get("EVOWIKI_LAYOUT_BACKEND") or "mineru"
    try:
        if backend == "mineru":
            version, pdf, image = _mineru()
        elif backend == "docling":
            version, pdf, image = _docling()
        else:
            raise ValueError(f"unknown backend {backend!r}")
    except Exception as exc:  # noqa: BLE001 - report any load failure, then exit
        send({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    try:
        smoke, _ = _with_png(_smoke_png(), image)
    except Exception as exc:  # noqa: BLE001
        send({"ready": False, "error": f"smoke: {type(exc).__name__}: {exc}"})
        return 1
    if not smoke.strip():
        send({"ready": False, "error": f"smoke: no text read from {SMOKE_TEXT!r}"})
        return 1
    print(f"layout worker: {backend} {version} smoke read {smoke.strip()!r}", file=sys.stderr,
          flush=True)
    send({"ready": True, "backend": backend, "version": version})

    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        m = re.search(rb'"id"\s*:\s*(\d+)', raw)   # echo the id even if the line is bad JSON
        rid = int(m.group(1)) if m else None
        try:
            req = json.loads(raw)
            rid = req.get("id")
            if req.get("op") == "pdf":
                first, last = int(req["first"]), int(req["last"])
                send({"id": rid, "ok": True, "raw": _scrub(pdf(req["path"], first, last))})
            else:
                markdown, blocks = _with_png(base64.b64decode(req["png_b64"]), image)
                send({"id": rid, "ok": True, "markdown": markdown, "blocks": blocks})
        except Exception as exc:  # noqa: BLE001 - one request fails, the worker stays
            print(f"layout worker: request {rid} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            send({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


# -- backends: (version, pdf(path, first, last) -> raw, image(path) -> (markdown, blocks)) --

def _mineru():
    import importlib.metadata as md

    from mineru.parser import parse
    from mineru.render import render_content_list

    def pdf(path, first, last):
        r = parse(path, tier="standard", page_range=f"{first}-{last}")
        return render_content_list(r.middle_json)

    def image(path):
        r = parse(path, tier="standard")
        cl = render_content_list(r.middle_json)
        texts, blocks = [], []
        for item in cl:
            t = _mineru_text(item)
            if t:
                texts.append(t)
            box = item.get("bbox")
            area = 0.0
            if box and len(box) >= 4:      # MinerU boxes are 0..1000 on each axis
                area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) / 1e6
            blocks.append({"label": str(item.get("type") or ""),
                           "area": round(min(area, 1.0), 4)})
        return "\n\n".join(texts), blocks

    return md.version("mineru"), pdf, image


def _mineru_text(item: dict) -> str:
    kind = item.get("type")
    if kind == "text":
        text = item.get("text") or ""
        lvl = item.get("text_level") or 0
        return f"{'#' * min(lvl, 6)} {text}" if lvl and text else text
    if kind == "equation":
        text = (item.get("text") or "").strip()
        return text if text.startswith("$$") else f"$${text}$$" if text else ""
    if kind == "table":
        return "\n".join([*_strs(item.get("table_caption")), item.get("table_body") or "",
                          *_strs(item.get("table_footnote"))]).strip()
    if kind in ("image", "chart"):
        return "\n".join(_strs(item.get(f"{kind}_caption"))).strip()
    if kind == "list":
        return "\n".join(_strs(item.get("list_items")))
    if kind == "code":
        return item.get("code_body") or ""
    return item.get("text") or ""


def _strs(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [s for s in (v or ()) if isinstance(s, str) and s.strip()]


def _docling():
    import importlib.metadata as md

    from docling.document_converter import DocumentConverter

    conv = DocumentConverter()

    def pdf(path, first, last):
        return conv.convert(path, page_range=(first, last)).document.export_to_dict()

    def image(path):
        doc = conv.convert(path).document
        sizes = {k: (p.size.width, p.size.height) for k, p in (doc.pages or {}).items()}
        blocks = []
        for item, _level in doc.iterate_items():
            label = str(getattr(getattr(item, "label", ""), "value", None)
                        or getattr(item, "label", ""))
            label = "image" if label == "picture" else label
            area = 0.0
            prov = item.prov[0] if getattr(item, "prov", None) else None
            if prov is not None:
                w, h = sizes.get(prov.page_no, (0, 0))
                if w and h:
                    area = abs(prov.bbox.r - prov.bbox.l) * abs(prov.bbox.t - prov.bbox.b) / (w * h)
            blocks.append({"label": label, "area": round(min(area, 1.0), 4)})
        return doc.export_to_markdown(image_placeholder=""), blocks

    return md.version("docling"), pdf, image


# -- helpers --

def _with_png(png: bytes, image) -> tuple[str, list[dict]]:
    """image(path) over `png` written to a temp file."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="evowiki-layout-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        return image(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _scrub(obj):
    """`obj` without embedded image payloads (`img_path` / `image` / `uri` -> "")."""
    if isinstance(obj, dict):
        return {k: ("" if k in _SCRUB else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _smoke_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (400, 120), "white")
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:                       # Pillow < 10.1: fixed-size default font
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((20, 35), SMOKE_TEXT, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


if __name__ == "__main__":
    sys.exit(main())
