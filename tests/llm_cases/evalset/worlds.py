"""A world is one user's event streams, exported once from a lane database and cloned into a fresh
user per trial, so every repetition of a case starts from the same state and nothing a trial does
reaches another (a trial owns its user id).

World files live outside the repo (`WORLDS`): their pages come from the audit lanes' data, and the
repo keeps no user data. `file_hash` goes into every run record, so a changed world shows up as a
changed case set."""

import hashlib
import json
from pathlib import Path

import psycopg

from tests.llm_cases.evalset.spec import DATA, TEST_PORT

WORLDS = DATA / "worlds"
TEST_CLUSTER = f"postgresql://postgres@127.0.0.1:{TEST_PORT}/"


def path(name: str) -> Path:
    return WORLDS / f"{name}.jsonl"


def file_hash(name: str) -> str:
    return hashlib.sha256(path(name).read_bytes()).hexdigest()


def export(name: str, db: str, user: str, *, upto_seq: int | None = None) -> dict[str, int]:
    """One line per event of `user` in lane database `db`, wiki events up to `upto_seq`. Read only;
    only the test cluster."""
    WORLDS.mkdir(parents=True, exist_ok=True)
    counts = {"wiki": 0, "aspect": 0}
    with psycopg.connect(TEST_CLUSTER + db, autocommit=True) as c, path(name).open(
            "w", encoding="utf-8", newline="\n") as f:
        bg, cond, lang = c.execute(
            "select background, condition, lang from app_user where user_id=%s", (user,)).fetchone()
        f.write(json.dumps({"from_db": db, "from_user": user, "upto_seq": upto_seq,
                            "user": {"background": bg, "condition": cond, "lang": lang}},
                           ensure_ascii=False) + "\n")
        wiki = c.execute("select seq, event_id, occurred_at, type, payload from wiki_event "
                         "where user_id=%s and (%s::bigint is null or seq<=%s) order by seq",
                         (user, upto_seq, upto_seq)).fetchall()
        for seq, eid, at, typ, p in wiki:
            f.write(json.dumps({"t": "wiki", "seq": seq, "event_id": eid, "at": at.isoformat(),
                                "type": typ, "payload": p}, ensure_ascii=False) + "\n")
            counts["wiki"] += 1
        last = wiki[-1][2] if wiki else None
        cols = _aspect_cols(c)
        rows = c.execute(f"select {', '.join(cols)} from aspect_event where user_id=%s "
                         "and (%s::timestamptz is null or occurred_at<=%s) order by seq",
                         (user, last, last)).fetchall()
        for r in rows:
            f.write(json.dumps({"t": "aspect", **dict(zip(cols, r, strict=True))},
                               ensure_ascii=False, default=str) + "\n")
            counts["aspect"] += 1
    return counts


def _aspect_cols(c: psycopg.Connection) -> list[str]:
    return [r[0] for r in c.execute(
        "select column_name from information_schema.columns where table_schema='public' and "
        "table_name='aspect_event' and column_name not in ('user_id', 'ingested_at') "
        "order by ordinal_position")]


def load(name: str) -> tuple[dict, list[dict]]:
    lines = path(name).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:]]


def clone(conn: psycopg.Connection, name: str, user_id: str) -> None:
    """A fresh user `user_id` holding world `name`. Event ids are globally unique, so each gets the
    user as a prefix; nothing else in an event names its user."""
    head, events = load(name)
    u = head["user"]
    conn.execute("insert into app_user (user_id, background, condition, lang, created_at) "
                 "values (%s, %s, %s, %s, now())",
                 (user_id, u["background"], u["condition"], u["lang"]))
    with conn.cursor() as cur:
        wiki = [(user_id, e["seq"], f"{user_id}:{e['event_id']}", e["at"], e["type"],
                 json.dumps(e["payload"], ensure_ascii=False)) for e in events if e["t"] == "wiki"]
        if wiki:
            cur.executemany("insert into wiki_event (user_id, seq, event_id, occurred_at, type, "
                            "payload) values (%s, %s, %s, %s, %s, %s)", wiki)
        aspect = [e for e in events if e["t"] == "aspect"]
        if aspect:
            cols = [k for k in aspect[0] if k != "t"]
            sql = (f"insert into aspect_event (user_id, {', '.join(cols)}) "
                   f"values (%s, {', '.join(['%s'] * len(cols))})")
            cur.executemany(sql, [(user_id, *[_cell(c, e[c], user_id) for c in cols])
                                  for e in aspect])


def _cell(col: str, v: object, user_id: str) -> object:
    if col == "event_id":
        return f"{user_id}:{v}"
    return json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v
