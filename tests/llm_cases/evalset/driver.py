"""Runs one trial of one case against the rig backend and returns its record: what the user did, what
the product answered, the tool ledger, and the state before, between and after. Graders read only
this record, so a run can be graded again without any model.

Steps a case can take:
  say: text         one chat turn. Options: page (title: the note open as he sends), selection
                    ({text, heading}), attach (a file: see `_attachment`)
  accept: true      accept everything waiting in this trial (page-edit proposals, draft action cards)
  reject: true      reject everything waiting
  undo: true        撤销 the newest executed action
  hand_edit: {page, section, append}    a hand edit in the note: the section's body plus `append`
  organize: title   write that page (the page run behind 开始生成)
  new_session: true the next `say` opens a new conversation (its first turn starts the old one's wrap-up)
  settle: true      wait until nothing of this user's moves any more (wrap-up, consolidation done)
  answer: {page, correct}  answer that page's own check question right or wrong, as the note's 小测 does,
                    and record the K cell before and after and whether the card is in 考考我这页
  delete_session: n delete the n-th conversation (1 = the first) as the 删除对话 dialog does, wait for it
"""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg

from evowiki.goals.path_edit import apply_edits, visible
from evowiki.harness.diff import diff_from_json
from evowiki.store import harness as pack_store
from evowiki.teach import spec_ops
from evowiki.wikiapp.spec_store import RepoSpecRegistry
from tests.llm_cases.evalset.spec import Case
from tests.llm_cases.evalset.worlds import clone

CANDIDATE_ENV = "EVOWIKI_EVALSET_CANDIDATE"

TURN_TIMEOUT_S = 400
ACTION_WAIT_S = 400
PAGE_WAIT_S = 600
RIG_RETRIES = 3
FILES = Path(__file__).resolve().parent / "files"


class Harness(RuntimeError):
    """The harness could not run the step: the trial has no verdict (never a product failure)."""


def user_for(run_id: str, case_id: str, trial: int) -> str:
    h = hashlib.sha1(case_id.encode()).hexdigest()[:10]
    return f"ev-{run_id}-{h}-{trial}"


def _sha(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
                          .encode()).hexdigest()[:16]


def state_summary(repo, conn: psycopg.Connection, user: str) -> dict[str, Any]:
    """What the graders compare: every page's sections, the graph, goals, paths, settings, rules,
    hidden cards. Layout coordinates and open-page marks are not content."""
    snap = repo.load(user)
    st = snap.state
    pages = {}
    for p in st.pages.values():
        heads = {s.id: s.heading for s in p.page.outline.sections} if p.page else {}
        order = [s.id for s in p.page.outline.sections] if p.page else []
        texts = dict(p.page.texts) if p.page else {}
        pages[p.page_id] = {
            "title": p.title, "goal": p.goal, "has_body": p.has_body,
            "sections": [{"id": sid, "heading": heads.get(sid, ""),
                          "cue": texts[sid].cue if sid in texts else "",
                          "body": texts[sid].body if sid in texts else ""} for sid in order]}
    g = st.graph
    dump = g.model_dump(mode="json")
    graph = {"nodes": sorted((n.id, n.name) for n in g.nodes),
             "rest": {k: v for k, v in dump.items() if k != "nodes"}}
    paths = {k: {"goal": v.path.goal, "order": [i.title for i in visible(apply_edits(v.path.pages, v.edits))]}
             for k, v in st.paths.items()}
    maps = {k: {"goal": m.goal, "nodes": [(n.id, n.name, n.parent) for n in m.nodes],
                "limits": list(getattr(m, "limits", ()) or ())} for k, m in st.maps.items()}
    settings = {k: v.model_dump(mode="json") for k, v in st.goal_settings.items()}
    rows = conn.execute("select count(*) from memory_item where user_id=%s", (user,)).fetchone()
    memory = [{"kind": k, "status": st, "content": c, "provenance": pv} for k, st, c, pv in conn.execute(
        "select kind, status, content, provenance from memory_item where user_id=%s order by created_at",
        (user,))]
    other = {"guidelines": [g.model_dump(mode="json") for g in st.guidelines],
             "hidden_cards": sorted(str(x) for x in st.hidden_cards),
             "review_postponed": sorted(str(x) for x in st.review_postponed),
             "memory_items": rows[0], "memory": memory, "wiki_seq": snap.wiki_seq}
    profile = conn.execute("select body->'owned' from learner_profile where user_id=%s", (user,)).fetchone()
    other["profile"] = profile[0] if profile else {}
    other["complaints"] = conn.execute("select count(*) from wiki_event where user_id=%s "
        "and type='signal_recorded' and payload->'signal'->>'kind'='user_complaint'", (user,)).fetchone()[0]
    other["rsi_cycles"] = conn.execute("select count(*) from rsi_cycle where user_id=%s", (user,)).fetchone()[0]
    out = {"pages": pages, "graph": graph, "paths": paths, "maps": maps, "settings": settings,
           "other": other}
    out["hash"] = {"pages": {k: _sha(v) for k, v in pages.items()}, "graph": _sha(graph),
                   "paths": _sha(paths), "maps": _sha(maps), "settings": _sha(settings),
                   "hidden_cards": _sha(other["hidden_cards"]),
                   "review_postponed": _sha(other["review_postponed"]),
                   "guidelines": _sha(other["guidelines"])}
    return out


def _sse(resp: httpx.Response) -> list[tuple[str, dict]]:
    frames, buf = [], ""
    for chunk in resp.iter_text():
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            f = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln)
            if "data" in f:
                try:
                    frames.append((f.get("event", ""), json.loads(f["data"])))
                except json.JSONDecodeError:
                    frames.append((f.get("event", ""), {"raw": f["data"]}))
    return frames


def _answer_of(frames: list[tuple[str, dict]]) -> str:
    """The answer as the client builds it (contracts.TextDelta): keep `answer[:at]`, append `text`."""
    out = ""
    for e, d in frames:
        if e == "text_delta":
            at = d.get("at")
            out = (out if at is None else out[:at]) + d.get("text", "")
    return out


class Trial:
    def __init__(self, base_url: str, repo, conn: psycopg.Connection, faults_dir: Path, case: Case,
                 run_id: str, n: int):
        self.case, self.repo, self.conn = case, repo, conn
        self.user = user_for(run_id, case.id, n)
        self.http = httpx.Client(base_url=base_url, headers={"X-EvoWiki-User": self.user},
                                 timeout=httpx.Timeout(TURN_TIMEOUT_S),
                                 # uvicorn closes idle connections after 5 s: a reused one races it
                                 limits=httpx.Limits(max_keepalive_connections=0))
        self.faults_dir = faults_dir
        self.session: str | None = None
        self.sessions: list[str] = []
        self.before: dict[str, Any] = {}
        self.seq_read = 0
        self.rec: dict[str, Any] = {"case": case.id, "trial": n, "user": self.user, "turns": [],
                                    "decisions": [], "steps": [], "fault": case.fault}

    # ---- steps
    def page_id(self, title: str) -> str:
        """`title`, or `title@goal words` when two goals have a page of that title."""
        name, _, goal = title.partition("@")
        for pid, p in self.before["pages"].items():
            if p["title"] == name and goal in p["goal"]:
                return pid
        raise Harness(f"{self.case.id}: no page {title!r} in world {self.case.world}")

    def _attachment(self, spec: str) -> str:
        """`spec`: a file under this case set's files/ (synthetic, in the repo) or an absolute path
        under the configured evaluation data directory (a local lecture). Uploaded as 问答用 material
        of this session, as the chat's paperclip does; waits until it can be attached."""
        p = Path(spec) if Path(spec).is_absolute() else FILES / spec
        if not p.exists():
            raise Harness(f"{self.case.id}: attachment {p} is missing on this machine")
        with p.open("rb") as fh:
            r = self.http.post("/wiki/sources", files={"file": (p.name, fh)},
                               data={"use": "ask", "session_id": self._session()})
        if r.status_code >= 400:
            raise Harness(f"upload {p.name}: {r.status_code} {r.text[:300]}")
        sid = r.json().get("source_id") or r.json()["id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < 300:
            d = self.http.get(f"/wiki/sources/{sid}").json()
            status = d.get("status") or d.get("source", {}).get("status")
            if status in ("ready", "partial"):
                self.rec["steps"].append({"attached": p.name, "source_id": sid, "status": status,
                                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
                return sid
            if status == "failed":
                raise Harness(f"source {p.name} failed: {json.dumps(d, ensure_ascii=False)[:300]}")
            time.sleep(1)
        raise Harness(f"source {p.name} not ready after 300 s")

    def _session(self) -> str:
        if self.session is None:
            r = self.http.post("/assistant/sessions", json={})
            if r.status_code >= 400:
                raise Harness(f"session: {r.status_code} {r.text[:300]}")
            self.session = r.json()["session_id"]
            self.sessions.append(self.session)
        return self.session

    def settle(self) -> None:
        """Until nothing of this user moved for 20 s and no run of his is open."""
        last, quiet, t0 = None, 0, time.monotonic()
        while time.monotonic() - t0 < 600:
            cur = self.conn.execute("select (select count(*) from llm_call where user_id=%s), "
                                    "(select count(*) from agent_run where user_id=%s and finished_at is null), "
                                    "(select count(*) from memory_item where user_id=%s)",
                                    (self.user, self.user, self.user)).fetchone()
            quiet = quiet + 5 if cur == last and not cur[1] else 0
            if quiet >= 20:
                return
            last = cur
            time.sleep(5)
        raise Harness("settle: the user's runs did not stop in 600 s")

    def _k_cells(self) -> dict[str, float]:
        r = self.http.get("/wiki/k")
        if r.status_code >= 400:
            raise Harness(f"k: {r.status_code} {r.text[:200]}")
        return {c["cell"]: c["m"] for con in r.json()["concepts"] for c in con["cells"]}

    def answer(self, s: dict[str, Any]) -> None:
        pid = self.page_id(s["page"])
        page = self.repo.load(self.user).state.pages[pid].page
        sec = next((x for x in page.outline.sections if x.check), None)
        if sec is None:
            raise Harness(f"{self.case.id}: page {s['page']} has no check question")
        chk, cell = sec.check, sec.check.cell
        rv = self.http.get(f"/wiki/review/page/{pid}")
        if rv.status_code >= 400:
            raise Harness(f"review page: {rv.status_code} {rv.text[:200]}")
        items = rv.json()["items"]
        in_review = any(i["cell"] == cell or i["section_id"] == sec.id for i in items)
        wrong = next(i for i in range(len(chk.options)) if i != chk.answer)
        choice = chk.answer if s["correct"] else wrong
        before = self._k_cells()
        r = self.http.post(f"/wiki/pages/{pid}/checks/{sec.id}", json={"choice": choice})
        if r.status_code >= 400:
            raise Harness(f"check answer: {r.status_code} {r.text[:300]}")
        after = self._k_cells()
        self.rec.setdefault("answers", []).append({
            "page": s["page"], "cell": cell, "asked_correct": bool(s["correct"]), "correct": r.json().get("correct"),
            "m_before": before.get(cell), "m_after": after.get(cell), "in_review": in_review, "review_cards": len(items)})

    def delete_session(self, n: int) -> None:
        sid = self.sessions[n - 1]
        r = self.http.request("DELETE", f"/assistant/sessions/{sid}", json={})
        self.rec["decisions"].append({"id": sid, "what": "delete_session", "http": r.status_code, "out": _json(r)})
        if r.status_code >= 400:
            raise Harness(f"delete session: {r.status_code} {r.text[:300]}")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 180:
            if self.http.get(f"/assistant/sessions/{sid}").status_code == 404:
                return
            time.sleep(3)
        raise Harness("delete session: still there after 180 s")

    def say(self, s: dict[str, Any]) -> None:
        body: dict[str, Any] = {"text": s["say"]}
        if s.get("page"):
            body["page_id"] = self.page_id(s["page"])
        if s.get("selection"):
            body["selection"] = s["selection"]
        if s.get("attach"):
            body["attachments"] = [self._attachment(s["attach"])]
        sess, t0 = self._session(), time.monotonic()
        last_seq = self.seq_read
        frames = self._send(sess, body)
        streamed = _answer_of(frames)
        errors = [d for e, d in frames if e == "error"]
        entries = self._entries(last_seq)
        tools = []
        for p in entries:
            if p["type"] == "tool":
                q = p["payload"]
                a = q.get("action") or q.get("quiet_action") or {}
                tools.append({"name": q.get("name"), "ok": q.get("ok"), "effect": q.get("effect"),
                              "status": a.get("status"), "action_id": a.get("action_id"),
                              "confirm": a.get("confirm"), "title": a.get("title"),
                              "args": q.get("args"), "result": q.get("content", "")})
        props = [p["payload"] for p in entries if p["type"] == "proposal"]
        stored = "".join(p["payload"].get("text", "") for p in entries if p["type"] == "assistant")
        self.rec["turns"].append({
            "say": s["say"], "answer": stored or streamed, "streamed": streamed, "tools": tools,
            "proposals": props, "errors": errors, "seconds": round(time.monotonic() - t0, 1),
            "events": [e for e, _ in frames if e != "text_delta"]})
        self.rec["after_turn_hash"] = state_summary(self.repo, self.conn, self.user)["hash"]

    def _send(self, sess: str, body: dict) -> list[tuple[str, dict]]:
        """A 5xx before the stream opens is the rig (a connection timeout on the shared test cluster),
        no model call was made: try again, and say so in the record."""
        for attempt in range(RIG_RETRIES + 1):
            with self.http.stream("POST", f"/assistant/sessions/{sess}/messages", json=body) as r:
                if r.status_code < 400:
                    return _sse(r)
                r.read()
                if r.status_code < 500 or attempt == RIG_RETRIES:
                    raise Harness(f"turn: {r.status_code} {r.text[:400]}")
            self.rec.setdefault("rig_retries", []).append(r.status_code)
            time.sleep(10 * (attempt + 1))
        raise Harness("unreachable")

    def _entries(self, after: int) -> list[dict]:
        rows = self.conn.execute("select seq, type, payload from assistant_entry where session_id=%s "
                                 "and seq>%s order by seq", (self.session, after)).fetchall()
        if rows:
            self.seq_read = rows[-1][0]
        return [{"seq": s, "type": t, "payload": p} for s, t, p in rows]

    def _waiting(self) -> tuple[list[str], list[str]]:
        props, cards = [], []
        for t in self.rec["turns"]:
            props += [p.get("proposal_id") for p in t["proposals"] if p.get("proposal_id")]
            cards += [x["action_id"] for x in t["tools"] if x["action_id"] and x["status"] == "proposed"]
        done = {d["id"] for d in self.rec["decisions"]}
        return [p for p in props if p not in done], [c for c in cards if c not in done]

    def decide(self, accept: bool) -> None:
        props, cards = self._waiting()
        for pid in props:
            r = self.http.post(f"/assistant/proposals/{pid}/{'accept' if accept else 'reject'}")
            self.rec["decisions"].append({"id": pid, "what": "proposal", "accept": accept,
                                          "http": r.status_code, "out": _json(r)})
        for aid in cards:
            r = self.http.post(f"/wiki/bot/actions/{aid}/{'approve' if accept else 'reject'}", json={})
            out = self._action_settled(aid) if r.status_code < 400 else _json(r)
            self.rec["decisions"].append({"id": aid, "what": "action", "accept": accept,
                                          "http": r.status_code, "out": out})

    def _action_settled(self, aid: str) -> dict:
        t0 = time.monotonic()
        while time.monotonic() - t0 < ACTION_WAIT_S:
            rows = self.http.get("/wiki/bot/actions").json()
            row = next((a for a in rows if a.get("action_id", a.get("id")) == aid), None)
            if row is None:
                return {"gone": True}
            if row.get("status") not in ("approved", "proposed"):
                return row
            time.sleep(2)
        raise Harness(f"action {aid} did not settle in {ACTION_WAIT_S} s")

    def undo(self) -> None:
        rows = self.http.get("/wiki/bot/actions", params={"status": "executed"}).json()
        if not rows:
            self.rec["decisions"].append({"id": "", "what": "undo", "http": 0, "out": "nothing"})
            return
        aid = rows[0].get("action_id", rows[0].get("id"))
        r = self.http.post(f"/wiki/bot/actions/{aid}/undo")
        self.rec["decisions"].append({"id": aid, "what": "undo", "http": r.status_code, "out": _json(r)})

    def hand_edit(self, s: dict[str, Any]) -> None:
        st = state_summary(self.repo, self.conn, self.user)
        pid = self.page_id(s["page"])
        sec = st["pages"][pid]["sections"][s["section"]]
        body = sec["body"] + s["append"]
        r = self.http.post(f"/wiki/pages/{pid}/hand-edit", json={
            "sections": [{"section_id": sec["id"], "body": body, "note_body": body}],
            "summary": "手改"})
        if r.status_code >= 400:
            raise Harness(f"hand edit: {r.status_code} {r.text[:300]}")
        self.rec["steps"].append({"hand_edit": pid, "section": sec["id"], "append": s["append"]})

    def organize(self, title: str) -> None:
        pid = self.page_id(title)
        t0 = time.monotonic()
        with self.http.stream("POST", f"/wiki/pages/{pid}/organize",
                              timeout=httpx.Timeout(PAGE_WAIT_S)) as r:
            if r.status_code >= 400:
                r.read()
                raise Harness(f"organize: {r.status_code} {r.text[:400]}")
            frames = _sse(r)
        self.rec["steps"].append({"organize": pid, "seconds": round(time.monotonic() - t0, 1),
                                  "events": sorted({e for e, _ in frames}),
                                  "errors": [d for e, d in frames if e == "error"]})

    def _candidate(self) -> None:
        """`run --candidate`: this trial's user runs the harness diff (`diff_to_json`), as if applied."""
        path = os.environ.get(CANDIDATE_ENV)
        if not path:
            return
        diff = diff_from_json(json.loads(Path(path).read_text(encoding="utf-8")))[0]
        now = datetime.now(UTC)
        for o in diff.ops:
            pack_store.set_value(self.conn, self.user, o.name, o.value, source="improver", why="candidate", now=now)
        if diff.spec_ops:
            reg = RepoSpecRegistry(self.repo, lambda p: f"{p}_{uuid4().hex}")
            spec = reg.current(self.user).spec
            for op in diff.spec_ops:
                spec = spec_ops.apply(spec, op, patch_id="evalset:candidate")
            reg.commit(self.user, spec, patch_id="evalset:candidate", at=now, actor="rsi")

    def _idle_writing_off(self) -> None:
        """As the world's owner has it (decision list 2: 空闲写页 你已关). Left on, every cloned user with
        an unwritten page draws background writes the case did not ask for."""
        read = self.http.get("/wiki/bot/settings")
        if read.status_code >= 400:     # another lane's backend on this port answers 404 for this user
            raise Harness(f"bot settings: {read.status_code} {read.text[:300]}")
        got = read.json()
        got["kinds"] = {**got["kinds"], "idle_write": False}
        r = self.http.put("/wiki/bot/settings", json=got)
        if r.status_code >= 400:
            raise Harness(f"bot settings: {r.status_code} {r.text[:300]}")

    # ---- the whole trial
    def run(self) -> dict[str, Any]:
        c = self.case
        try:
            if c.world:
                clone(self.conn, c.world, self.user)
            else:
                self._require(self.http.post("/users", json={"user_id": self.user,
                    "survey": {"math": "none", "ml": "none", "dl": "none"}, "lang": c.language}), "register")
            self._require(self.http.put("/wiki/settings", json={"language": c.language}), "language")
            # Exercise the same explicit budget gate as setup, before any paid request.
            self._require(self.http.patch("/usage/settings", json={"daily_limit_usd": 5,
                "monthly_limit_usd": 30, "confirm_budget": True}), "budget")
            if model := os.environ.get("EVOWIKI_EVALSET_MODEL"):
                self._require(self.http.post("/assistant/model", json={"model": model}), "model selection")
            self.rec["language"] = c.language
            self._candidate()
            if c.fault:
                self.faults_dir.mkdir(parents=True, exist_ok=True)
                (self.faults_dir / f"{self.user}.json").write_text(json.dumps(c.fault), encoding="utf-8")
            self._idle_writing_off()
            before = self.before = state_summary(self.repo, self.conn, self.user)
            for s in c.steps:
                if "say" in s:
                    self.say(s)
                elif s.get("accept"):
                    self.decide(True)
                elif s.get("reject"):
                    self.decide(False)
                elif s.get("undo"):
                    self.undo()
                elif "hand_edit" in s:
                    self.hand_edit(s["hand_edit"])
                elif "organize" in s:
                    self.organize(s["organize"])
                elif s.get("new_session"):
                    self.session = None
                    self.seq_read = 0
                elif s.get("settle"):
                    self.settle()
                elif "answer" in s:
                    self.answer(s["answer"])
                elif "delete_session" in s:
                    self.delete_session(s["delete_session"])
                else:
                    raise Harness(f"{c.id}: unknown step {s}")
            after = state_summary(self.repo, self.conn, self.user)
            self.rec["before"], self.rec["after"] = _thin(before, after)
            self.rec["actions"] = _json(self.http.get("/wiki/bot/actions"))
        except (Harness, httpx.HTTPError, psycopg.Error) as exc:
            self.rec["harness_error"] = f"{type(exc).__name__}: {exc}"[:600]
        finally:
            self.http.close()
        return self.rec

    @staticmethod
    def _require(response: httpx.Response, step: str) -> None:
        if response.status_code >= 400:
            raise Harness(f"{step}: {response.status_code} {response.text[:300]}")


def _thin(before: dict, after: dict) -> tuple[dict, dict]:
    """Both sides whole (a grader may compare any page) minus nothing: the records go gzipped in the
    log, and a page that did not change is dropped from `after` only."""
    same = {k for k, h in before["hash"]["pages"].items() if after["hash"]["pages"].get(k) == h}
    thin = {**after, "pages": {k: v for k, v in after["pages"].items() if k not in same},
            "unchanged_pages": sorted(same)}
    return before, thin


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:2000]
