"""POST /wiki/assets/generated: whether EvoWiki stored a block with exactly this content, for this user."""

import json
from dataclasses import replace

import pytest

from evowiki.deep.evoui import block_fence, block_json, stored_form
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.store import PgAssetStore, sha256
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page, wiki  # noqa: F401 - fixture
from tests.wikiapp.test_block_results import stored

APP = {"type": "evoui", "title": "t", "question": "q", "fallback": "f", "state": {"n": 1.5, "u": "中"}, "derived": {},
       "body": {"kind": "app", "html": "<p>x</p><script>evowiki.ready()</script>"}}


def stored_text() -> str:
    return block_json(EvoUIBlock.model_validate(APP))


def test_the_stored_form_of_a_note_fence_and_of_an_asset_file_is_what_the_server_stored() -> None:
    text = stored_text()
    fence_body = block_fence(json.loads(text)).split("\n", 1)[1].rsplit("\n", 1)[0]
    assert stored_form(text) == text
    assert stored_form(fence_body) == text


@pytest.mark.parametrize("bad", ["", "{broken", "[1]", "3"])
def test_what_is_not_a_block_has_no_stored_form(bad: str) -> None:
    assert stored_form(bad) is None


def test_the_route_answers_for_this_user_and_this_content(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki)
    mem, aid = stored(pid, block=APP)
    with services.pool.connection() as conn, conn.transaction():
        PgAssetStore(services.pool).writer(list(mem.rows.values()))(conn)
    text = mem.rows[aid].html

    def ask(body: str, user: str = "u1") -> bool:
        r = client.post("/wiki/assets/generated", json={"text": body}, headers=H(user))
        assert r.status_code == 200, r.text
        return r.json()["generated"]

    assert ask(text)                                                     # the asset file
    assert ask(json.dumps(json.loads(text), ensure_ascii=False, indent=1))  # the same block as an inline fence
    assert not ask(text, "someone-else")                                 # another user's asset is not yours
    changed = json.loads(text)
    changed["body"]["html"] += "<script>evil()</script>"
    assert not ask(json.dumps(changed, ensure_ascii=False))              # one character of the program changed
    assert not ask(json.dumps({**json.loads(text), "generated": True, "signature": aid}))  # a marker proves nothing
    assert not ask("{broken")


def test_an_html_file_counts_only_byte_for_byte_for_this_user(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki)
    mem, aid = stored(pid, block=APP)
    html = "<!doctype html><p>generated</p><script>1</script>\n"
    row = mem.rows[aid]
    meta = row.meta.model_copy(update={"asset_id": "ia_html1", "kind": "html", "sha256": sha256(html)})
    with services.pool.connection() as conn, conn.transaction():
        PgAssetStore(services.pool).writer([replace(row, meta=meta, html=html)])(conn)

    def ask(body: str, user: str = "u1", kind: str = "html") -> bool:
        r = client.post("/wiki/assets/generated", json={"text": body, "kind": kind}, headers=H(user))
        assert r.status_code == 200, r.text
        return r.json()["generated"]

    assert ask(html)
    assert not ask(html + " ")
    assert not ask(html, "someone-else")
    assert not ask(html, kind="evoui")
