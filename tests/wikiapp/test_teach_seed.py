"""wikiapp.teach_seed: a user's style guide + exemplar into their own spec, one version, no LLM."""
import pytest

from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.teach_seed import SeedExemplar, exemplar_text, seed_style, style_items
from tests.wikiapp.test_goal import NOW, Ids

NOTE = """---
type: knowledge
aliases: [投影]
---

# 12-投影

误差垂直于线，见 [[11-正交向量与正交子空间]] 和 [[线性代数学习入口|目录]]。

```embedded-html
type: embedded-html
id: la12-line
src: Assets/Interactive/LinearAlgebra/la12-line.html
title: 把 b 投到一条线上
height: 710
```

![[Assets/Visuals/x.svg]]
"""


def _spec(repo):
    return tspec.effective_spec(repo.load("u1").state)


def test_style_items_one_rule_per_paragraph():
    assert style_items("- 第一条\n接着写。\n\n\n2. 第二条\n\n") == ["第一条接着写。", "第二条"]


def test_exemplar_text_drops_vault_syntax():
    t = exemplar_text(NOTE)
    assert t.startswith("# 12-投影")
    assert "（这里是一个交互：把 b 投到一条线上）" in t and "embedded-html" not in t
    assert "见 11-正交向量与正交子空间 和 目录。" in t and "[[" not in t
    assert "Assets/Visuals/x.svg" in t and "![[" not in t


def test_seed_writes_one_version_and_reruns_add_nothing():
    repo, ids = MemoryRepo(["u1"]), Ids()
    ex = SeedExemplar(ref="12-投影.md", title="投影", text=NOTE)
    v = seed_style(repo, "u1", items=["不要压缩信息。", "不用比喻代替解释。", "不要压缩信息。"],
                   origin="seed:style.md", exemplar=ex, analogy=0, now=NOW, new_id=ids)
    spec = _spec(repo)
    assert v == spec.version
    seeded = [i for i in spec.items if i.origin_ref == "seed:style.md"]
    assert [i.text for i in seeded] == ["不要压缩信息。", "不用比喻代替解释。"]
    # a script's style pack, not the user's words (rule provenance contract §7.1 #13)
    assert all((i.origin, i.authority) == ("seed", "rsi") for i in seeded)
    assert [e.ref for e in spec.exemplars] == ["12-投影.md"]
    assert spec.exemplars[0].kind == "vault_note" and "[[" not in spec.exemplars[0].text
    assert spec.params.analogy_level == 0
    assert seed_style(repo, "u1", items=["不要压缩信息。"], origin="seed:style.md", exemplar=ex,
                      analogy=0, now=NOW, new_id=ids) is None
    assert _spec(repo).version == v


def test_seed_fails_closed_and_writes_nothing():
    repo, ids = MemoryRepo(["u1"]), Ids()
    before = _spec(repo).version
    with pytest.raises(tspec.SpecInvalid):
        seed_style(repo, "u1", items=["x" * 501], origin="seed:a", exemplar=None, analogy=None,
                   now=NOW, new_id=ids)
    assert _spec(repo).version == before
