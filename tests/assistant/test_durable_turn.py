"""Durable ports of one assistant turn (assistant/durable.py): inside a (virtual) workflow the
turn yields the same events and writes the same session log as the plain run, every read / write
of a port is a recorded step, and a retried append is idempotent."""

import json

import pytest

from evowiki.agent import durable
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.durable import (
    DurableAssistantData,
    DurableSessionStore,
    durable_turn_deps,
)
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import EntryConflict, MemorySessionStore, fold
from evowiki.llm.chat import ChatTurn
from tests.assistant.fakes import INFO, T0, Data
from tests.assistant.test_loop import call, setup

SCRIPT = [ChatTurn(tool_calls=(call("read_k", 1), call("propose_page_edit", 2))),
          ChatTurn(text="我建议加一段。" + "答" * 100)]


def dump(events):
    return [e.model_dump(mode="json") for e in events]


def log_of(store, sid="s1"):
    return [(e.seq, e.entry_id, e.type, e.payload, e.at) for e in store.entries(sid)]


# ---- (a) idempotent append ----

def test_memory_append_is_idempotent_and_refuses_a_different_entry():
    store = MemorySessionStore()
    store.create(INFO)
    payload = {"message_id": "m1", "text": "问", "selection": None}
    first = store.append("s1", "e1", "user", payload, T0)
    again = store.append("s1", "e1", "user", dict(payload), T0)
    assert again == first and len(store.entries("s1")) == 1
    with pytest.raises(EntryConflict):
        store.append("s1", "e1", "user", {**payload, "text": "别的"}, T0)
    with pytest.raises(EntryConflict):
        store.append("s1", "e1", "assistant", payload, T0)
    assert len(store.entries("s1")) == 1


# ---- (b) same events and same log as the plain run ----

def test_durable_turn_equals_the_plain_turn(tmp_path):
    plain, _, _ = setup(tmp_path / "plain", SCRIPT)
    live = dump(run_turn(plain, INFO, MessageIn(text="问")))
    base, _, _ = setup(tmp_path / "durable", SCRIPT)
    deps = durable_turn_deps(base)
    with durable.virtual_workflow("turn:s1:m1"):
        got = dump(run_turn(deps, INFO, MessageIn(text="问")))
    assert got == live
    assert log_of(base.store) == log_of(plain.store)
    assert fold(INFO, base.store.entries("s1")) == fold(INFO, plain.store.entries("s1"))


def test_durable_deps_outside_a_workflow_are_plain(tmp_path):
    plain, _, _ = setup(tmp_path / "plain", SCRIPT)
    live = dump(run_turn(plain, INFO, MessageIn(text="问")))
    base, _, _ = setup(tmp_path / "durable", SCRIPT)
    assert dump(run_turn(durable_turn_deps(base), INFO, MessageIn(text="问"))) == live
    assert log_of(base.store) == log_of(plain.store)


# ---- (c) every port access is a step ----

def in_step():
    assert not durable.in_workflow(), "port touched in the workflow body, outside a step"


class SpyStore(MemorySessionStore):
    def info(self, session_id):
        in_step()
        return super().info(session_id)

    def entries(self, session_id):
        in_step()
        return super().entries(session_id)

    def append(self, *a, **k):
        in_step()
        return super().append(*a, **k)


class SpyData(Data):
    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in ("page", "pages", "k_text", "guidelines", "language", "prereqs"):
            def wrapped(*a, **k):
                in_step()
                return attr(*a, **k)
            return wrapped
        return attr


def test_ports_are_steps_for_a_read_tool_and_a_propose_tool(tmp_path, monkeypatch):
    names: list[str] = []
    real = durable.step_ran

    def spy(name, fn):
        names.append(name)
        return real(name, fn)

    monkeypatch.setattr(durable, "step_ran", spy)
    base, _, ts = setup(tmp_path, SCRIPT)
    store = SpyStore()
    store.create(INFO)
    base.store, base.data = store, SpyData()
    for t in ts:                       # the loop must run the tools inside their steps too
        real_run = t.run

        def checked(a, c, real_run=real_run):
            in_step()
            return real_run(a, c)

        object.__setattr__(t, "run", checked)
    with durable.virtual_workflow("turn:s1:m1"):
        ev = dump(run_turn(durable_turn_deps(base), INFO, MessageIn(text="问")))
    assert ev[-1]["type"] == "done"
    assert any(e["type"] == "proposal" for e in ev)
    for want in ("assistant.entries", "assistant.append", "llm.converse:assistant.turn",
                 "tool:read_k", "tool:propose_page_edit", "new_id",
                 "clock", "assistant.data.language", "assistant.data.page",
                 "assistant.data.k_text", "assistant.data.guidelines", "assistant.data.pages",
                 "assistant.data.prereqs"):
        assert want in names, (want, names)
    assert names.count("llm.converse:assistant.turn") == 2


def test_data_and_store_wrappers_round_trip_json():
    d = DurableAssistantData(Data())
    with durable.virtual_workflow("w"):
        assert d.page("u1", "p1") == Data().page("u1", "p1") and d.page("u1", "zz") is None
        assert d.pages("u1") == Data().pages("u1")
        assert d.prereqs("u1", ["x"]) == ("mean",) and d.language("u1") == "zh"
        assert list(d.guidelines("u1")) == Data().guidelines("u1")
        store = DurableSessionStore(MemorySessionStore())
        store.create(INFO)
        e = store.append("s1", "e1", "user", {"message_id": "m", "text": "x"}, T0)
        assert store.entries("s1") == [e] and store.info("s1") == INFO
        with pytest.raises(LookupError):
            store.info("nope")
        with pytest.raises(EntryConflict):
            store.append("s1", "e1", "user", {"message_id": "m", "text": "y"}, T0)


# ---- (d) message_id ----

def test_message_id_is_used_for_the_user_entry(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(text="答")])
    ev = dump(run_turn(deps, INFO, MessageIn(text="问"), message_id="m-fixed"))
    users = [e for e in deps.store.entries("s1") if e.type == "user"]
    assert users[0].payload["message_id"] == "m-fixed"
    assert ev[-1]["type"] == "done"


# ---- B1d carry-over inside the durable turn ----

# ---- chat attachments (T6b review I2) ----

def _sha(a, data):
    import hashlib
    from dataclasses import replace as dc_replace
    return dc_replace(a, source=dc_replace(a.source, sha256=hashlib.sha256(data).hexdigest()))


def test_attachment_reads_and_routing_are_steps_and_file_bytes_never_recorded(tmp_path,
                                                                             monkeypatch):
    from evowiki.assistant.attachments import MemoryAttachmentReader
    from tests.assistant.attach_fakes import doc, image, png
    from tests.assistant.test_loop_attachments import setup as att_setup

    pic_bytes = png()
    pic = _sha(image("pic"), pic_bytes)
    deps, prov, _ = att_setup(tmp_path, [], route="anthropic", script=[ChatTurn(text="好")])

    class SpyReader(MemoryAttachmentReader):
        def load(self, *a, **k):
            in_step()
            return super().load(*a, **k)

    deps.attachments = SpyReader({"u1": {"pic": (pic, pic_bytes),
                                         "d": (doc("d", ["正文"]), None)}})
    seen = []
    deps.sees_images = lambda acct: (in_step(), seen.append(acct), True)[2]
    names, values = [], []
    real = durable.step_ran

    def spy(name, fn):
        names.append(name)
        out = real(name, fn)
        values.append(out[0])
        return out
    monkeypatch.setattr(durable, "step_ran", spy)
    with durable.virtual_workflow("turn:s1:m1"):
        ev = dump(run_turn(durable_turn_deps(deps), INFO,
                           MessageIn(text="看", attachments=("pic", "d"))))
    assert ev[-1]["type"] == "done"
    for want in ("assistant.attachments.load", "assistant.sees_images", "assistant.route"):
        assert want in names, (want, names)
    assert seen and [p.kind for p in prov.chat_calls[0].messages[-1].parts] == ["image", "doc"]
    import base64
    assert base64.b64encode(pic_bytes).decode()[:40] not in json.dumps(values, default=str)


def test_attachment_steps_record_only_what_the_turn_gives(tmp_path, monkeypatch):
    """A 300-section book is recorded as sizes (load) and its table of contents (texts), never
    its full text; a small material's full text is recorded once. The durable turn sends exactly
    what the plain turn sends."""
    from evowiki.assistant.attachments import TOC_MAX_TOKENS, MemoryAttachmentReader
    from evowiki.goals.context import estimate_tokens
    from evowiki.sources.config import full_text_tokens
    from tests.assistant.attach_fakes import doc
    from tests.assistant.test_loop_attachments import setup as att_setup

    book = doc("book", [f"第{i}节正文" + "长" * 2000 for i in range(300)])
    note = doc("note", ["短讲义的全文"])
    data = {"u1": {"book": (book, None), "note": (note, None)}}

    def run(durable_run):
        where = tmp_path / str(durable_run)        # a fresh call log: no cached answer
        where.mkdir()
        deps, prov, _ = att_setup(where, [], script=[ChatTurn(text="好")])
        deps.attachments = MemoryAttachmentReader(data)
        msg = MessageIn(text="看", attachments=("book", "note"))
        if not durable_run:
            dump(run_turn(deps, INFO, msg))
        else:
            with durable.virtual_workflow("turn:s1:m1"):
                dump(run_turn(durable_turn_deps(deps), INFO, msg))
        return prov.chat_calls[0].messages[-1]

    plain = run(False)
    values: dict[str, list] = {}
    real = durable.step_ran

    def spy(name, fn):
        out = real(name, fn)
        values.setdefault(name, []).append(out[0])
        return out
    monkeypatch.setattr(durable, "step_ran", spy)
    sent = run(True)
    assert sent == plain and "短讲义的全文" in sent.parts[1].text
    loaded = json.dumps(values["assistant.attachments.load"], ensure_ascii=False)
    assert len(loaded) < 10_000 and "长长长" not in loaded
    texts = values["assistant.attachments.texts"]
    assert len(texts) == 1 and set(texts[0]) == {"book", "note"}
    assert estimate_tokens(texts[0]["book"]) <= TOC_MAX_TOKENS + 100
    assert sum(estimate_tokens(t) for t in texts[0].values()) <= (
        full_text_tokens() + TOC_MAX_TOKENS + 100)
    assert "长" * 200 not in json.dumps(texts, ensure_ascii=False)


def test_durable_texts_fail_closed_when_the_material_changed():
    from evowiki.assistant.attachments import (
        AttachmentGone,
        MemoryAttachmentReader,
        Placement,
        plan,
    )
    from evowiki.assistant.durable import DurableAttachmentReader
    from tests.assistant.attach_fakes import doc
    data = {"u1": {"d": (doc("d", ["原来的正文"]), None)}}
    rd = DurableAttachmentReader(MemoryAttachmentReader(data))
    atts = {a.source_id: a for a in rd.load("u1", ["d"])}
    assert atts["d"].digest is not None and atts["d"].sections == ()
    data["u1"]["d"] = (doc("d", ["后来改过的、长一些的正文" * 20]), None)
    with pytest.raises(AttachmentGone):
        plan(Placement(turns=(("m1", ("d",)),)), atts, sees=False, budget_tokens=30000,
             limit_tokens=100000, user_id="u1", reader=rd, render=lambda b, p: [])


def test_attachment_json_round_trips():
    from dataclasses import replace

    from evowiki.assistant.attachments import Attachment, digested
    from evowiki.assistant.durable import attachment_from_json, attachment_json
    from tests.assistant.attach_fakes import doc, image
    for a in (doc("d", ["一", "二"], figures=(2,), unreadable=(3,)), image("i", ocr="字"),
              doc("g", ["x"], deleted=True),
              Attachment(doc("c", ["x"]).source, ("ask", "learn"), True, chars=12),
              replace(doc("l", ["x"]), last_used=doc("l", ["x"]).source.created_at),
              digested(doc("e", ["一", "二"], figures=(2,))),
              replace(doc("t", ["x"]), source=replace(
                  doc("t", ["x"]).source, page_offset=2,
                  glossary=({"term": "特征值", "means": "λ", "page": 3},)))):
        back = attachment_from_json(json.loads(json.dumps(attachment_json(a))))
        assert back == a


def test_durable_file_fails_closed_unless_the_original_hashes_as_loaded():
    from evowiki.assistant.attachments import AttachmentGone
    from evowiki.assistant.durable import DurableAttachmentReader
    from tests.assistant.attach_fakes import image, png, reader
    data = png()
    pic = _sha(image("pic"), data)
    rd = DurableAttachmentReader(reader((pic, data)))
    assert rd.file("u1", "pic", sha256=pic.source.sha256) == data
    with pytest.raises(AttachmentGone):
        rd.file("u1", "pic", sha256="0" * 64)
    with pytest.raises(AttachmentGone):
        rd.file("u1", "pic")
