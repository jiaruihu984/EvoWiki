"""Disposable /wiki backend for the Stage 2 「进入一块」 walkthrough in REAL Obsidian (plan
the product specification T5; driver obsidian-plugin/scripts/real-obsidian/
stage2-enter-walkthrough.mjs). Test-only: tests/e2e/blocks_backend.py (fresh test database, the
scripted chat turn, S1's six 黑壳虾 blocks, probe / path / writer fakes, fail closed on an
unscripted call) plus a scripted goals.enter (S2: one call per entry splits one level and, with his
words in the prompt, estimates from them):

- 「水质与环境」 → 水温 (atomic), 换水 (atomic), 水质检测 (not atomic);
- 「水质检测」 → 氨氮, pH (atomic);
- any other node → its covers as atomic parts (two at least).

With the estimate part in the prompt (「再写 known：」), every fragment of his words (split at
，。；、 and line breaks) that names a part gives one `known` entry with that fragment as the quote:
「没学过」「不会」 → unlearned, 「忘」 → beginner, else 「会」 → intermediate. Code then keeps only the
verbatim ones — this fake writes only verbatim fragments.

GET /test/enter: every goals.enter request in order — the entered node's name, whether the prompt
had the estimate part, his words it carried — plus the served count per purpose.

  uv run python -m tests.e2e.enter_backend --port 8806
"""

import re
from collections import Counter

from evowiki.goals.intake import ENTER_PURPOSE
from tests.e2e import blocks_backend as bb
from tests.e2e.wiki_backend import Unscripted
from tests.goals.fakes_intake import ESTIMATE_MARK

TREE: dict[str, list[tuple[str, bool, list[str]]]] = {
    "水质与环境": [("水温", True, ["term", "purpose"]), ("换水", True, ["procedure"]),
                ("水质检测", False, ["term", "procedure"])],
    "水质检测": [("氨氮", True, ["term", "interpret"]), ("pH", True, ["term", "procedure"])],
}
_NODE = re.compile(r"里的(?:一块|.+?下面的一部分)「([^」]+)」（id ")
_WORDS = re.compile(r"他自己说过的话（只有这些算他的话）：\n(.*?)\n再写 known：",
                    re.DOTALL)
_CUT = re.compile(r"[，。；、,;\n]")


def _level(fragment: str) -> str:
    if "没学过" in fragment or "不会" in fragment:
        return "unlearned"
    if "忘" in fragment:
        return "beginner"
    return "intermediate" if "会" in fragment else ""


def enter_reply(prompt: str) -> dict:
    """goals.enter for the node the prompt names (module doc); fails closed on any other prompt."""
    m = _NODE.search(prompt)
    if m is None:
        raise Unscripted(f"goals.enter: {prompt[:80]!r}")
    name = m.group(1)
    parts = TREE.get(name) or [(c, True, []) for c in bb.COVERS.get(name, [f"{name}（一）",
                                                                         f"{name}（二）"])]
    out: dict = {"parts": [{"name": n, "summary": f"{n}是怎么回事、要注意什么", "atomic": a,
                            **({"aspects": asp} if asp else {})} for n, a, asp in parts]}
    if ESTIMATE_MARK in prompt:
        said = _WORDS.search(prompt)
        frags = [f.strip() for f in _CUT.split(said.group(1) if said else "") if f.strip()]
        out["known"] = [{"part": n, "level": lv, "quote": f}
                        for n, _, _ in parts for f in frags
                        if n in f and (lv := _level(f))]
    return out


def build_script():
    s = _build()
    s.always(ENTER_PURPOSE, enter_reply)
    return s


def mount(app, script) -> None:
    _mount(app, script)

    @app.get("/test/enter")
    def _enter_calls() -> dict:
        rows = []
        for _, r in script._served:
            if r.purpose != ENTER_PURPOSE:
                continue
            m, said = _NODE.search(r.prompt), _WORDS.search(r.prompt)
            rows.append({"node": m.group(1) if m else "", "estimate": ESTIMATE_MARK in r.prompt,
                         "words": said.group(1).splitlines() if said else []})
        served = Counter(r.purpose for _, r in script._served)
        return {"enter": rows, "served": dict(served)}


_build, _mount = bb.build_script, bb.mount_script_status

if __name__ == "__main__":
    bb.build_script, bb.mount_script_status = build_script, mount   # blocks_backend's main, ours
    bb.main()
