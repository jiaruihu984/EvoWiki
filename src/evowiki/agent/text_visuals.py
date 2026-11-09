"""Recognize obvious character-drawn boxes, leaving tables, formulas and real code alone."""

import re


def has_character_diagram(text: str) -> bool:
    lines = []
    fenced = False
    diagram_fence = False
    for line in text.splitlines():
        if match := re.match(r"^\s*(```+|~~~+)\s*(\S*)", line):
            if fenced:
                fenced = False
                diagram_fence = False
            else:
                fenced = True
                diagram_fence = match[2].lower() in {"", "text", "plain", "plaintext", "ascii"}
            continue
        if not fenced or diagram_fence:
            lines.append(line)
    visible = "\n".join(lines)
    if len(re.findall(r"[┌┐└┘╔╗╚╝]", visible)) >= 2:
        return True
    borders = sum(bool(re.search(r"\+[-=]{3,}\+", line)) for line in lines)
    return borders >= 2 and any(re.match(r"^\s*\|.*\|\s*$", line) for line in lines)
