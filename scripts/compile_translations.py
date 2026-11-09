"""Compile checked-in gettext catalogs with Babel's standard PO/MO implementation.

Run after editing PO files: python scripts/compile_translations.py
"""
import json
import re
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "src/evowiki/locales"
    for source in sorted(root.glob("*/LC_MESSAGES/evowiki.po")):
        with source.open("rb") as stream:
            catalog = read_po(stream, locale=source.parent.parent.name)
        errors = [(message.id, error) for message, problems in catalog.check() for error in problems]
        if errors:
            raise ValueError(errors)
        with source.with_suffix(".mo").open("wb") as stream:
            write_mo(stream, catalog)
        if source.parent.parent.name == "en":
            seeds: dict[str, dict[str, str]] = {}
            runtime: dict[str, str] = {}
            for message in catalog:
                if message.context and message.context.startswith("seed:"):
                    seeds.setdefault(message.context.removeprefix("seed:"), {})[message.id] = message.string
                elif message.context == "evoui":
                    runtime[message.id] = message.string
            product = root.parent / "deep"
            if seeds:
                (product / "seed_messages.json").write_text(
                    json.dumps(seeds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if runtime:
                script = product / "evoui/evoui.js"
                text = script.read_text(encoding="utf-8")
                literal = "    const labels = " + json.dumps(runtime, ensure_ascii=False) + ";"
                text = re.sub(r"^    const labels = .*;$", lambda _, literal=literal: literal, text,
                              count=1, flags=re.MULTILINE)
                script.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
