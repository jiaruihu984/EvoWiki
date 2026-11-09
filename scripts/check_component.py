"""Check configurable library components before registering them (no database, no LLM).

  uv run python scripts/check_component.py src/evowiki/deep/components/distribution-line.json
  uv run python scripts/check_component.py src/evowiki/deep/components/*.json --shots DIR

For each component file (a `SeedComponent` JSON next to its HTML): the config schema must be in the
supported subset, every example config must validate, and component + each example must pass the render
check (headless Chromium at 375 and 1280 px: no console/page errors, no network, no horizontal overflow,
every probe changes its result region). With --shots, screenshots of each example are written to
DIR/<component_id>/ex<n>-<width>-<before|after>.png (after = once all probes ran) plus a dark-mode one
(ex<n>-1280-dark.png). Exit code 1 when anything failed."""

import argparse
import json
import sys
from pathlib import Path

from evowiki.deep.components import (
    ConfigInvalid,
    SchemaError,
    check_schema,
    instantiate,
    validate_config,
)
from evowiki.deep.contracts import InteractiveDraft, LibraryComponent, Probe, RenderCheck
from evowiki.deep.library_seed import REPO_ROOT, load_component, seed_html
from evowiki.deep.render import CHECK_CSP, CHECK_URL, PlaywrightChecker

WIDTHS = (375, 1280)


def _act(page, p: Probe) -> None:
    t = page.locator(p.target).first
    if p.action == "click":
        t.click(timeout=3000)
    elif p.action == "input":
        t.fill(p.value or "", timeout=3000)
        t.dispatch_event("change")
    else:
        t.select_option(p.value, timeout=3000)
    page.wait_for_timeout(250)


def screenshots(html: str, height: int, probes: tuple[Probe, ...], out: Path, stem: str) -> None:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        shots = [(w, "light") for w in WIDTHS] + [(1280, "dark")]
        for w, scheme in shots:
            page = b.new_page(viewport={"width": w, "height": height}, color_scheme=scheme)
            page.route("**/*", lambda r: r.fulfill(body=html, headers={
                "content-type": "text/html; charset=utf-8", "content-security-policy": CHECK_CSP})
                if r.request.url == CHECK_URL else r.abort())
            page.goto(CHECK_URL)
            page.wait_for_timeout(300)
            if scheme == "dark":
                for p in probes:
                    _act(page, p)
                page.screenshot(path=str(out / f"{stem}-{w}-dark.png"), full_page=True)
            else:
                page.screenshot(path=str(out / f"{stem}-{w}-before.png"), full_page=True)
                for p in probes:
                    _act(page, p)
                page.screenshot(path=str(out / f"{stem}-{w}-after.png"), full_page=True)
            page.close()
        b.close()


def check_file(path: Path, shots: Path | None) -> tuple[bool, list[dict]]:
    c = load_component(path)
    report: list[dict] = []
    try:
        if c.config_schema is None:
            raise SchemaError("not a configurable component (no config_schema)")
        check_schema(c.config_schema)
        if len(c.example_configs) < 2:
            raise SchemaError("needs at least 2 example configs")
        for cfg in c.example_configs:
            validate_config(c.config_schema, cfg)
    except (SchemaError, ConfigInvalid) as exc:
        return False, [{"error": str(exc)}]
    html = seed_html(c, REPO_ROOT)
    shell = LibraryComponent.model_construct(
        component_id=c.component_id, name=c.name, learning_question=c.learning_question, html=html,
        height=c.height, probes=c.probes, config_schema=c.config_schema)
    checker = PlaywrightChecker()
    ok = True
    for n, cfg in enumerate(c.example_configs, 1):
        html_i, probes_i, title, question = instantiate(shell, cfg)
        check: RenderCheck = checker.check(InteractiveDraft(
            interactive_id=c.component_id, section_id="library", title=title,
            learning_question=question, html=html_i, height=c.height, probes=probes_i))
        ok &= check.ok
        report.append({"example": n, "title": title, **check.model_dump(exclude={"interactive_id"})})
        if shots is not None:
            screenshots(html_i, c.height, probes_i, shots / c.component_id, f"ex{n}")
    return ok, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--shots", type=Path, help="write screenshots under this directory")
    args = ap.parse_args(argv)
    bad = 0
    for f in args.files:
        ok, report = check_file(f, args.shots)
        bad += not ok
        print(("OK   " if ok else "FAIL ") + f.stem)
        for r in report:
            print("     " + json.dumps(r, ensure_ascii=False))
    print(f"{len(args.files) - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
