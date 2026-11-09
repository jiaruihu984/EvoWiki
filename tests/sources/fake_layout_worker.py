"""Fake layout worker for test_layout_client (stdlib only; speaks layout_worker's protocol).

Start: env FAKE_LAYOUT_START = "ok" (default) | "fail" (ready:false, exit) | "smoke_fail" (the
start-up smoke read failed: ready:false "smoke: ...", exit) | "hang" (never ready). The ready
line names the backend from EVOWIKI_LAYOUT_BACKEND (default "mineru") and version "4.0.12".
Every start appends "start" to the file named by FAKE_LAYOUT_LOG (if set), so tests count
restarts; every pdf request appends "pdf <first> <last>".

op "image" (or no op): the "png" is a JSON page script: {"markdown", "blocks", "do"} with do =
"ok" (default) | "fail" (always ok:false) | "flaky" (ok:false for the first "flaky" request
across all worker processes, tracked through FAKE_LAYOUT_LOG; ok after) | "hang" (no reply) |
"crash" (exit) | "id_none" (ok:false with id null, as for an unparseable request) | "bad_reply"
(markdown not a string).

op "pdf": answers {"ok": true, "raw": <the JSON file named by FAKE_LAYOUT_RAW>}; for mineru each
item's page_idx is shifted by first-1 (MinerU's page_idx is absolute in the whole PDF). Env
FAKE_LAYOUT_PDF scripts a failure: "fail_once" (ok:false for the first pdf request across all
worker processes), "fail" (always ok:false), "hang" (no reply), "crash" (exit).
"""

import base64
import json
import os
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def logged(entry):
    path = os.environ.get("FAKE_LAYOUT_LOG")
    if not path:
        return []
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def pdf(req, backend):
    first, last = req["first"], req["last"]
    entries = logged(f"pdf {first} {last}")
    do = os.environ.get("FAKE_LAYOUT_PDF", "ok")
    if do == "hang":
        time.sleep(60)
    if do == "crash":
        sys.exit(3)
    if do == "fail" or (do == "fail_once"
                        and sum(e.startswith("pdf ") for e in entries) == 1):
        send({"id": req["id"], "ok": False, "error": "RuntimeError: CUDA out of memory"})
        return
    with open(os.environ["FAKE_LAYOUT_RAW"], encoding="utf-8") as f:
        raw = json.load(f)
    if backend == "mineru":
        for item in (x for x in raw if isinstance(x, dict)):
            item["page_idx"] = int(item.get("page_idx", 0)) + first - 1
    send({"id": req["id"], "ok": True, "raw": raw})


def main():
    logged("start")
    start = os.environ.get("FAKE_LAYOUT_START", "ok")
    backend = os.environ.get("EVOWIKI_LAYOUT_BACKEND", "mineru")
    if start == "fail":
        send({"ready": False, "error": "no GPU"})
        return 1
    if start == "smoke_fail":
        send({"ready": False, "error": "smoke: no text read from 'EvoWiki 123'"})
        return 1
    if start == "hang":
        time.sleep(60)
        return 1
    send({"ready": True, "backend": backend, "version": "4.0.12"})
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("op") == "pdf":
            pdf(req, backend)
            continue
        page = json.loads(base64.b64decode(req["png_b64"]))
        do = page.get("do", "ok")
        print(f"fake worker: page {req['id']} {do}", file=sys.stderr)
        if do == "hang":
            time.sleep(60)
        if do == "crash":
            return 3
        if do == "id_none":            # as layout_worker does for a request it cannot parse
            send({"id": None, "ok": False, "error": "JSONDecodeError: bad request"})
            continue
        if do == "bad_reply":
            send({"id": req["id"], "ok": True, "markdown": 5, "blocks": []})
            continue
        if do == "fail" or (do == "flaky" and logged("flaky").count("flaky") == 1):
            send({"id": req["id"], "ok": False, "error": "OUT_OF_RANGE: piece id is out of range"})
            continue
        send({"id": req["id"], "ok": True, "markdown": page.get("markdown", ""),
              "blocks": page.get("blocks", [])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
