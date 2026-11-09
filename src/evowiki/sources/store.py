"""Materials store (migration 0059). Plain functions over a connection; the caller owns the
transaction. Every query carries user_id; another user's source is simply not found."""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from evowiki.retrieval.embed import Embedder
from evowiki.sources.document import SourceDocument
from evowiki.sources.models import ChunkRow, LinkRow, PageRead, SectionRow, SourceRow, Use
from evowiki.store.artifacts import (
    Artifact,
    Dependency,
    Lineage,
    current_run_id,
    put_lineage,
    text_key,
)
from evowiki.store.db import fetch_all, fetch_one

_SOURCE_COLS = ("source_id, user_id, kind, title, sha256, bytes, pages, status, error, "
                "unreadable_pages, toc_mode, progress, origin, created_at, updated_at, deleted_at, "
                "page_offset, glossary, run_gen, glossary_coverage, text_first, imprecise_pages, "
                "toc_edited, image_pages, title_source, earlier_titles")
_SECTION_COLS = ("section_id, source_id, parent_id, ord, depth, title, page_from, page_to, "
                 "text, tokens, summary, read_by")


_RETITLED = ("earlier_titles = case when title = %s or title = any(earlier_titles) "
             "then earlier_titles else earlier_titles || title end, title = %s")


def _row(cls: type, r: dict) -> Any:
    return cls(**{k: r[k] for k in cls.__dataclass_fields__})


def _source(r: dict) -> SourceRow:
    return _row(SourceRow, {**r, "unreadable_pages": tuple(r["unreadable_pages"]),
                            "glossary": tuple(r["glossary"]),
                            "imprecise_pages": tuple(r["imprecise_pages"]),
                            "image_pages": tuple(r["image_pages"]),
                            "earlier_titles": tuple(r["earlier_titles"])})


def _vec(v: list[float]) -> np.ndarray:
    # No length check here: a vector that disagrees with the declared dim is refused by the
    # table's vector_dims(embedding) = dim check.
    return np.asarray(v, dtype=np.float32)


def _require_owner(conn: psycopg.Connection, user_id: str, source_id: str) -> None:
    """Writes need a live source of this user; unknown, foreign and deleted ones fail closed.
    `for no key update` holds the row until the writing transaction ends, so a concurrent `soft_delete`
    (an UPDATE of the row) either commits first (the re-check sees deleted_at) or waits; unlike
    `for share` it does not deadlock two writers that both then UPDATE the row."""
    if fetch_one(conn, "select 1 as x from source where source_id = %s and user_id = %s "
                       "and deleted_at is null for no key update", (source_id, user_id)) is None:
        raise LookupError(f"source {source_id} not found for user {user_id}")


# --- source ---------------------------------------------------------------------------------

def insert_source(conn: psycopg.Connection, row: SourceRow, data: bytes, mime: str) -> None:
    conn.execute(
        f"insert into source ({_SOURCE_COLS}) values "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s)",
        (row.source_id, row.user_id, row.kind, row.title, row.sha256, row.bytes, row.pages,
         row.status, row.error, list(row.unreadable_pages), row.toc_mode, Jsonb(row.progress),
         row.origin, row.created_at, row.updated_at, row.deleted_at, row.page_offset,
         Jsonb(list(row.glossary)), row.run_gen,
         None if row.glossary_coverage is None else Jsonb(row.glossary_coverage),
         row.text_first, list(row.imprecise_pages), row.toc_edited,
         list(row.image_pages), row.title_source, list(row.earlier_titles)))
    conn.execute("insert into source_file (source_id, mime, data) values (%s, %s, %s)",
                 (row.source_id, mime, data))


def find_by_sha(conn: psycopg.Connection, user_id: str, sha256: str) -> SourceRow | None:
    r = fetch_one(
        conn, f"select {_SOURCE_COLS} from source where user_id = %s and sha256 = %s "
              "and deleted_at is null and status <> 'failed' order by created_at limit 1",
        (user_id, sha256))
    return None if r is None else _source(r)


def get_source(conn: psycopg.Connection, user_id: str, source_id: str) -> SourceRow | None:
    """Deleted sources are returned too (a citation shows them as deleted)."""
    r = fetch_one(conn, f"select {_SOURCE_COLS} from source where user_id = %s and source_id = %s",
                  (user_id, source_id))
    return None if r is None else _source(r)


def list_sources(conn: psycopg.Connection, user_id: str) -> list[SourceRow]:
    return [_source(r) for r in fetch_all(
        conn, f"select {_SOURCE_COLS} from source where user_id = %s and deleted_at is null "
              "order by created_at desc, source_id", (user_id,))]


def set_status(conn: psycopg.Connection, user_id: str, source_id: str, *, status: str,
               error: str | None = None, pages: int | None = None,
               unreadable_pages: list[int] | None = None, toc_mode: str | None = None) -> None:
    """Sets status and error; pages / unreadable_pages / toc_mode only when given."""
    _require_owner(conn, user_id, source_id)
    conn.execute(
        "update source set status = %(status)s, error = %(error)s, "
        "pages = coalesce(%(pages)s, pages), "
        "unreadable_pages = coalesce(%(unr)s, unreadable_pages), "
        "toc_mode = coalesce(%(toc)s, toc_mode), updated_at = now() "
        "where source_id = %(sid)s and user_id = %(uid)s",
        {"status": status, "error": error, "pages": pages, "unr": unreadable_pages,
         "toc": toc_mode, "sid": source_id, "uid": user_id})


def set_text_first(conn: psycopg.Connection, user_id: str, source_id: str, *, text_first: bool,
                   imprecise_pages: list[int], image_pages: list[int]) -> None:
    """Whether the sections are the PDF's text layer, its unreliable pages, and the pages a chat
    turn reads only from their image (`sources.importer`, module doc)."""
    _require_owner(conn, user_id, source_id)
    conn.execute("update source set text_first = %s, imprecise_pages = %s, image_pages = %s, "
                 "updated_at = now() where source_id = %s and user_id = %s",
                 (text_first, imprecise_pages, image_pages, source_id, user_id))


def has_links(conn: psycopg.Connection, user_id: str, source_id: str) -> bool:
    """A wiki node links to one of the source's sections."""
    return fetch_one(conn, "select 1 as x from source_link l join source_section t "
                           "using (section_id) where t.source_id = %s and l.user_id = %s limit 1",
                     (source_id, user_id)) is not None


def set_progress(conn: psycopg.Connection, user_id: str, source_id: str, progress: dict) -> None:
    _require_owner(conn, user_id, source_id)
    conn.execute("update source set progress = %s, updated_at = now() "
                 "where source_id = %s and user_id = %s", (Jsonb(progress), source_id, user_id))


def set_page_offset(conn: psycopg.Connection, user_id: str, source_id: str,
                    offset: int | None) -> None:
    _require_owner(conn, user_id, source_id)
    conn.execute("update source set page_offset = %s, updated_at = now() "
                 "where source_id = %s and user_id = %s", (offset, source_id, user_id))


def set_glossary(conn: psycopg.Connection, user_id: str, source_id: str,
                 entries: list[dict], coverage: dict | None = None) -> None:
    """The glossary and which text it was extracted from (`glossary.GlossaryInput.coverage`,
    0101; None: not known)."""
    _require_owner(conn, user_id, source_id)
    conn.execute("update source set glossary = %s, glossary_coverage = %s, updated_at = now() "
                 "where source_id = %s and user_id = %s",
                 (Jsonb(entries), None if coverage is None else Jsonb(coverage), source_id,
                  user_id))
    run, now = current_run_id(), datetime.now(UTC)          # the saved glossary (0091)
    put_lineage(conn, Lineage((Artifact(
        user_id, "glossary", source_id,
        text_key("glossary", json.dumps(entries, sort_keys=True, ensure_ascii=False)), run, now),)))


def get_file(conn: psycopg.Connection, user_id: str, source_id: str) -> tuple[bytes, str] | None:
    r = fetch_one(
        conn, "select f.data, f.mime from source_file f join source s using (source_id) "
              "where f.source_id = %s and s.user_id = %s", (source_id, user_id))
    return None if r is None else (bytes(r["data"]), r["mime"])


# --- sections and chunks --------------------------------------------------------------------

def replace_sections(conn: psycopg.Connection, user_id: str, source_id: str,
                     sections: list[SectionRow]) -> None:
    """Drop the old sections (chunks and links cascade) and write the new ones."""
    _require_owner(conn, user_id, source_id)
    conn.execute("delete from source_section where source_id = %s", (source_id,))
    for s in sections:
        if s.source_id != source_id:
            raise ValueError(f"section {s.section_id} belongs to {s.source_id}, not {source_id}")
        conn.execute(
            f"insert into source_section ({_SECTION_COLS}) values "
            "(%s, %s, null, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (s.section_id, source_id, s.ord, s.depth, s.title, s.page_from, s.page_to, s.text,
             s.tokens, s.summary, s.read_by))
    for s in sections:  # parents are linked after all rows exist, so input order is free
        if s.parent_id is not None:
            conn.execute("update source_section set parent_id = %s where section_id = %s",
                         (s.parent_id, s.section_id))


def set_section_embeddings(conn: psycopg.Connection, user_id: str, source_id: str,
                           rows: list[tuple[str, list[float]]], embedder: Embedder) -> None:
    _require_owner(conn, user_id, source_id)
    for section_id, v in rows:
        cur = conn.execute(
            "update source_section set embedder_id = %s, model_version = %s, dim = %s, "
            "embedding = %s where section_id = %s and source_id = %s",
            (embedder.id, embedder.model_version, embedder.dim, _vec(v), section_id, source_id))
        if cur.rowcount != 1:
            raise LookupError(f"section {section_id} not found in source {source_id}")


def section_vectors(conn: psycopg.Connection, user_id: str, source_id: str,
                    embedder_tag: tuple[str, str, int]) -> dict[str, list[float]]:
    """section_id -> vector of a live source's sections embedded under this same
    (embedder_id, model_version, dim); sections without one (or another tag's) are left out."""
    eid, ver, dim = embedder_tag
    rows = fetch_all(
        conn, "select t.section_id, t.embedding from source_section t join source s "
              "using (source_id) where t.source_id = %s and s.user_id = %s "
              "and s.deleted_at is null and t.embedding is not null and t.embedder_id = %s "
              "and t.model_version = %s and t.dim = %s order by t.ord, t.section_id",
        (source_id, user_id, eid, ver, dim))
    return {r["section_id"]: [float(x) for x in r["embedding"].to_list()] for r in rows}


def replace_chunks(conn: psycopg.Connection, user_id: str, source_id: str,
                   chunks: list[ChunkRow], vectors: list[list[float]],
                   embedder: Embedder) -> None:
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    _require_owner(conn, user_id, source_id)
    conn.execute("delete from source_chunk where source_id = %s", (source_id,))
    for c, v in zip(chunks, vectors, strict=True):
        if c.source_id != source_id:
            raise ValueError(f"chunk {c.chunk_id} belongs to {c.source_id}, not {source_id}")
        conn.execute(
            "insert into source_chunk (chunk_id, section_id, source_id, ord, page, text, "
            "embedder_id, model_version, dim, embedding) values "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (c.chunk_id, c.section_id, source_id, c.ord, c.page, c.text, embedder.id,
             embedder.model_version, embedder.dim, _vec(v)))


def set_summaries(conn: psycopg.Connection, user_id: str, source_id: str,
                  rows: list[tuple[str, str, str]]) -> None:
    """rows are (section_id, summary, text_hash)."""
    _require_owner(conn, user_id, source_id)
    for section_id, summary, text_hash in rows:
        cur = conn.execute("update source_section set summary = %s, summary_hash = %s "
                           "where section_id = %s and source_id = %s",
                           (summary, text_hash, section_id, source_id))
        if cur.rowcount != 1:
            raise LookupError(f"section {section_id} not found in source {source_id}")
    # the saved summaries (0091), each made from its section's text (by the hash it was made of)
    run, now = current_run_id(), datetime.now(UTC)
    arts: list[Artifact] = []
    deps: list[Dependency] = []
    for section_id, summary, text_hash in rows:
        ref = f"{source_id}/{section_id}"
        made = Artifact(user_id, "source_summary", ref, text_key("summary", summary), run, now)
        text = Artifact(user_id, "source_section", ref, text_hash, None, now)
        arts += [made, text]
        deps.append(Dependency(made.artifact_id, text.artifact_id, "source_section"))
    put_lineage(conn, Lineage(tuple(arts), tuple(deps)))


def summaries_by_hash(conn: psycopg.Connection, user_id: str,
                      hashes: list[str]) -> dict[str, str]:
    """Summary cache keyed by text hash, limited to this user's own sources."""
    if not hashes:
        return {}
    rows = fetch_all(
        conn, "select distinct on (t.summary_hash) t.summary_hash as h, t.summary as s "
              "from source_section t join source s using (source_id) "
              "where s.user_id = %s and s.deleted_at is null and t.summary_hash = any(%s) and t.summary is not null "
              "order by t.summary_hash, t.section_id", (user_id, hashes))
    return {r["h"]: r["s"] for r in rows}


def own_summaries(conn: psycopg.Connection, user_id: str, source_id: str) -> dict[str, str]:
    """This source's section summaries by text hash (a new run's toc keeps them: `replace_sections`
    drops the rows that hold them, so `summaries_by_hash` would not find them any more)."""
    return {r["h"]: r["s"] for r in fetch_all(
        conn, "select t.summary_hash as h, t.summary as s from source_section t "
              "join source s using (source_id) where t.source_id = %s and s.user_id = %s "
              "and t.summary is not null and t.summary_hash is not null", (source_id, user_id))}


def sections(conn: psycopg.Connection, user_id: str, source_id: str) -> list[SectionRow]:
    cols = ", ".join(f"t.{c.strip()}" for c in _SECTION_COLS.split(","))
    return [_row(SectionRow, r) for r in fetch_all(
        conn, f"select {cols} from source_section t join source s using (source_id) "
              "where t.source_id = %s and s.user_id = %s and s.deleted_at is null "
              "order by t.ord, t.section_id",
        (source_id, user_id))]


def chunks_of(conn: psycopg.Connection, user_id: str, source_ids: list[str]) -> list[ChunkRow]:
    return [_row(ChunkRow, r) for r in fetch_all(
        conn, "select c.chunk_id, c.section_id, c.source_id, c.ord, c.page, c.text "
              "from source_chunk c join source s using (source_id) "
              "where c.source_id = any(%s) and s.user_id = %s and s.deleted_at is null "
              "order by c.source_id, c.ord, c.chunk_id", (source_ids, user_id))]


# --- uses and deletion ----------------------------------------------------------------------

def add_use(conn: psycopg.Connection, user_id: str, source_id: str, use: Use, *,
            goal_id: str | None = None, page_id: str | None = None,
            session_id: str | None = None, now: datetime) -> str:
    _require_owner(conn, user_id, source_id)
    use_id = uuid.uuid4().hex
    conn.execute(
        "insert into source_use (use_id, source_id, user_id, use, goal_id, page_id, session_id, "
        "created_at) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (use_id, source_id, user_id, use, goal_id, page_id, session_id, now))
    return use_id


def delete_use(conn: psycopg.Connection, user_id: str, use_id: str) -> bool:
    """Removes one use of the user's (the assistant's undo of mark_source_use); False when it is
    not there (already removed, or not this user's)."""
    cur = conn.execute("delete from source_use where use_id = %s and user_id = %s",
                       (use_id, user_id))
    return cur.rowcount == 1


def uses_of(conn: psycopg.Connection, user_id: str, source_id: str) -> list[dict]:
    return fetch_all(
        conn, "select use_id, source_id, use, goal_id, page_id, session_id, created_at "
              "from source_use where source_id = %s and user_id = %s "
              "order by created_at, use_id", (source_id, user_id))


def set_origin(conn: psycopg.Connection, user_id: str, source_id: str, origin: str) -> None:
    """Where the original of a live source lies in the vault; unknown, foreign and deleted ones
    raise LookupError."""
    _require_owner(conn, user_id, source_id)
    conn.execute("update source set origin = %s, updated_at = now() "
                 "where source_id = %s and user_id = %s", (origin, source_id, user_id))


def rename(conn: psycopg.Connection, user_id: str, source_id: str, title: str) -> None:
    """The user's own name for a live source: no automatic title replaces it. Unknown, foreign
    and deleted ones raise LookupError."""
    _require_owner(conn, user_id, source_id)
    conn.execute(f"update source set {_RETITLED}, title_source = 'given', updated_at = now() "
                 "where source_id = %s and user_id = %s", (title, title, source_id, user_id))


def auto_title(conn: psycopg.Connection, user_id: str, source_id: str, title: str,
               source: str) -> str | None:
    """A title the material itself gave, over a file name or url (never over the user's name, also
    when he renamed it a moment ago). The title it replaced; None when nothing changed."""
    r = fetch_one(conn, f"update source set {_RETITLED}, title_source = %s, updated_at = now() "
                        "from (select title as was from source where source_id = %s) old "
                        "where source_id = %s and user_id = %s and title_source = 'fallback' "
                        "and deleted_at is null and title <> %s returning old.was",
                  (title, title, source, source_id, source_id, user_id, title))
    return None if r is None else r["was"]


def soft_delete(conn: psycopg.Connection, user_id: str, source_id: str, now: datetime) -> bool:
    """Delete the original, sections and chunks; the source row and its title stay."""
    cur = conn.execute(
        "update source set deleted_at = %s, updated_at = %s "
        "where source_id = %s and user_id = %s and deleted_at is null",
        (now, now, source_id, user_id))
    if cur.rowcount == 0:
        return False
    _drop_content(conn, source_id)
    return True


def _drop_content(conn: psycopg.Connection, source_id: str) -> None:
    conn.execute("delete from source_file where source_id = %s", (source_id,))
    conn.execute("delete from source_section where source_id = %s", (source_id,))  # chunks cascade
    conn.execute("delete from source_page_read where source_id = %s", (source_id,))
    conn.execute("delete from source_document where source_id = %s", (source_id,))


def replace_links(conn: psycopg.Connection, user_id: str, node_ids: Sequence[str],
                  links: Sequence[LinkRow], *, by: str, now: datetime) -> None:
    """Replace this user's `by` links of `node_ids` with `links`. Every section must belong to a
    live source of this user (LookupError otherwise) and carry the same `by` (ValueError
    otherwise); the caller owns the transaction."""
    for link in links:
        if link.by != by:
            raise ValueError(f"link {link.node_id}/{link.section_id} is by {link.by}, not {by}")
    conn.execute("delete from source_link where user_id = %s and node_id = any(%s) and by = %s",
                 (user_id, list(node_ids), by))
    for link in links:
        r = fetch_one(conn, "select t.source_id from source_section t join source s using (source_id) "
                            "where t.section_id = %s and s.user_id = %s and s.deleted_at is null",
                      (link.section_id, user_id))
        if r is None or r["source_id"] != link.source_id:
            raise LookupError(f"section {link.section_id} not found for user {user_id}")
        conn.execute(
            "insert into source_link (user_id, node_id, section_id, role, by, created_at) "
            "values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
            (user_id, link.node_id, link.section_id, link.role, by, now))


def replace_source_links(conn: psycopg.Connection, user_id: str, source_id: str,
                         links: Sequence[LinkRow], *, by: str, now: datetime) -> None:
    """Replace this user's `by` links into the sections of `source_id` with `links` (each into
    that source). Links into any other source are kept. The checks
    and the transaction are `replace_links`' (LookupError: a section not of this live source)."""
    for link in links:
        if link.source_id != source_id:
            raise ValueError(f"link {link.node_id}/{link.section_id} is not into {source_id}")
    conn.execute("delete from source_link l using source_section t where l.user_id = %s "
                 "and l.by = %s and t.section_id = l.section_id and t.source_id = %s",
                 (user_id, by, source_id))
    replace_links(conn, user_id, [], links, by=by, now=now)


_LINK_SELECT = ("select l.node_id, l.section_id, t.source_id, l.role, l.by "
                "from source_link l join source_section t on t.section_id = l.section_id "
                "join source s on s.source_id = t.source_id ")
_LINK_ORDER = " order by s.created_at, t.ord, l.node_id"


def links_for_nodes(conn: psycopg.Connection, user_id: str,
                    node_ids: Sequence[str]) -> list[LinkRow]:
    rows = fetch_all(conn, _LINK_SELECT + "where l.user_id = %s and s.user_id = %s "
                     "and s.deleted_at is null and l.node_id = any(%s)" + _LINK_ORDER,
                     (user_id, user_id, list(node_ids)))
    return [_row(LinkRow, r) for r in rows]


def links_for_sources(conn: psycopg.Connection, user_id: str,
                      source_ids: Sequence[str]) -> list[LinkRow]:
    rows = fetch_all(conn, _LINK_SELECT + "where l.user_id = %s and s.user_id = %s "
                     "and s.deleted_at is null and s.source_id = any(%s)" + _LINK_ORDER,
                     (user_id, user_id, list(source_ids)))
    return [_row(LinkRow, r) for r in rows]


def edit_sections(conn: psycopg.Connection, user_id: str, source_id: str,
                  edits: Sequence[tuple[str, str | None, int | None]]) -> None:
    """Retitle / relevel sections of a live source; edits are (section_id, title, depth), None
    keeps the old value. Every parent_id is then recomputed from the depths in ord order (the
    nearest earlier section one level up, or none). Text, chunks and links stay. An unknown
    section raises KeyError; unknown, foreign and deleted sources raise LookupError."""
    _require_owner(conn, user_id, source_id)
    rows = fetch_all(conn, "select section_id, title, depth, parent_id from source_section "
                           "where source_id = %s order by ord, section_id", (source_id,))
    by_id = {r["section_id"]: r for r in rows}
    for section_id, title, depth in edits:
        if section_id not in by_id:
            raise KeyError(section_id)
        if title is not None:
            by_id[section_id]["title"] = title
        if depth is not None:
            by_id[section_id]["depth"] = depth
    stack: list[dict] = []          # the open ancestors, shallowest first
    for r in rows:
        while stack and stack[-1]["depth"] >= r["depth"]:
            stack.pop()
        r["parent"] = stack[-1]["section_id"] if stack else None
        stack.append(r)
    for r in rows:
        conn.execute("update source_section set title = %s, depth = %s, parent_id = %s "
                     "where section_id = %s and source_id = %s",
                     (r["title"], r["depth"], r["parent"], r["section_id"], source_id))
    conn.execute("update source set toc_edited = true where source_id = %s", (source_id,))


# --- page reads, run generations, vision quote, attachments (migration 0062) -----------------

def put_page_reads(conn: psycopg.Connection, user_id: str, source_id: str,
                   reads: list[PageRead], now: datetime) -> None:
    """Upsert by (page, by): writing the same page by the same reader again overwrites it."""
    _require_owner(conn, user_id, source_id)
    for r in reads:
        conn.execute(
            "insert into source_page_read (source_id, user_id, page, text, by, figure, created_at) "
            "values (%s, %s, %s, %s, %s, %s, %s) "
            "on conflict (source_id, page, by) do update set text = excluded.text, "
            "figure = excluded.figure, created_at = excluded.created_at",
            (source_id, user_id, r.page, r.text, r.by, r.figure, now))


def page_reads(conn: psycopg.Connection, user_id: str, source_id: str) -> dict[int, PageRead]:
    """One entry per page; when both readers read it, the claude read wins."""
    out: dict[int, PageRead] = {}
    for r in fetch_all(
            conn, "select page, text, by, figure from source_page_read "
                  "where source_id = %s and user_id = %s order by page, by", (source_id, user_id)):
        # `by` sorts claude before local; keep the first row of each page
        out.setdefault(r["page"], PageRead(r["page"], r["text"], r["by"], r["figure"]))
    return out


def bump_run(conn: psycopg.Connection, user_id: str, source_id: str) -> int:
    """run_gen += 1 and return the new value."""
    _require_owner(conn, user_id, source_id)
    r = fetch_one(conn, "update source set run_gen = run_gen + 1, updated_at = now() "
                        "where source_id = %s and user_id = %s returning run_gen",
                  (source_id, user_id))
    assert r is not None
    return r["run_gen"]



def stale_ask_only(conn: psycopg.Connection, *, before: datetime,
                   limit: int = 100) -> list[tuple[str, str]]:
    """(user_id, source_id) of live sources whose newest use is older than `before` (a file
    attached again is young again), whose every use is `ask` and none of whose sections a
    knowledge point links to. Crosses users: for the system-side cleanup only."""
    return [(r["user_id"], r["source_id"]) for r in fetch_all(
        conn, f"select s.user_id, s.source_id from source s where {_STALE_ASK} "
              "order by s.created_at, s.source_id limit %s",
        (before, limit))]


def soft_delete_stale_ask(conn: psycopg.Connection, user_id: str, source_id: str, *,
                          before: datetime, now: datetime) -> bool:
    """`soft_delete`, only if the source still is what `stale_ask_only` found, judged now in the
    caller's transaction: a use or link added since the scan keeps it (D18). The row is locked
    first, so a use being recorded (`add_use` locks the row too) has committed and the
    conditional update - a new statement, a new snapshot - sees it, or waits for this delete
    and then finds the source gone."""
    if fetch_one(conn, "select 1 as x from source where source_id = %s and user_id = %s "
                       "for update", (source_id, user_id)) is None:
        return False
    cur = conn.execute(
        f"update source s set deleted_at = %s, updated_at = %s "
        f"where s.source_id = %s and s.user_id = %s and {_STALE_ASK}",
        (now, now, source_id, user_id, before))
    if cur.rowcount == 0:
        return False
    _drop_content(conn, source_id)
    return True


# the cleanup's rule over `source s` (one parameter: the cutoff)
_STALE_ASK = ("s.deleted_at is null "
              "and (select max(u.created_at) from source_use u "
              "where u.source_id = s.source_id) < %s "
              "and not exists (select 1 from source_use u where u.source_id = s.source_id "
              "and u.use <> 'ask') "
              "and not exists (select 1 from source_link l join source_section t "
              "using (section_id) where t.source_id = s.source_id)")


def unfinished_runs(conn: psycopg.Connection) -> list[SourceRow]:
    """Live sources whose newest run has no end and that are not failed: the run is in progress
    or was asked for and never started. Crosses users: for the system-side start-up check."""
    return [_source(r) for r in fetch_all(
        conn, f"select {_SOURCE_COLS} from source where deleted_at is null "
              "and status <> 'failed' and not (progress ? 'end') "
              "order by created_at, source_id")]


def session_attachments(conn: psycopg.Connection, user_id: str, session_id: str) -> list[str]:
    """source_ids attached to this chat session (use = ask), oldest first. Deleted ones stay
    listed: the session shows them as cleaned up."""
    return [r["source_id"] for r in fetch_all(
        conn, "select source_id from source_use where user_id = %s and session_id = %s "
              "and use = 'ask' group by source_id order by min(created_at), source_id",
        (user_id, session_id))]


# --- the layout parser's document (migration 0102) -----------------------------------------

def put_document(conn: psycopg.Connection, user_id: str, source_id: str, doc: SourceDocument,
                 now: datetime) -> None:
    """Upsert this source's document (one per source); unknown, foreign and deleted sources
    raise LookupError."""
    _require_owner(conn, user_id, source_id)
    conn.execute(
        "insert into source_document (source_id, user_id, backend, version, doc, created_at) "
        "values (%s, %s, %s, %s, %s, %s) on conflict (source_id) do update set "
        "backend = excluded.backend, version = excluded.version, doc = excluded.doc, "
        "created_at = excluded.created_at",
        (source_id, user_id, doc.backend, doc.version, Jsonb(doc.to_json()), now))


def get_document(conn: psycopg.Connection, user_id: str,
                 source_id: str) -> SourceDocument | None:
    """The live source's stored document; None when there is none (or the source is not this
    user's, or deleted)."""
    r = fetch_one(conn, "select d.doc from source_document d join source s using (source_id) "
                        "where d.source_id = %s and s.user_id = %s and d.user_id = %s "
                        "and s.deleted_at is null", (source_id, user_id, user_id))
    return None if r is None else SourceDocument.from_json(r["doc"])


# --- page segments read after the source became usable (the import's ocr stage) -------------

def update_section_text(conn: psycopg.Connection, user_id: str, source_id: str,
                        section_id: str, *, text: str, tokens: int, read_by: str) -> None:
    """New text for one section; its summary and vector are dropped (they described the old
    text: the summary step and the re-embed after this fill them again)."""
    _require_owner(conn, user_id, source_id)
    cur = conn.execute(
        "update source_section set text = %s, tokens = %s, read_by = %s, summary = null, "
        "summary_hash = null, embedder_id = null, model_version = null, dim = null, "
        "embedding = null where section_id = %s and source_id = %s",
        (text, tokens, read_by, section_id, source_id))
    if cur.rowcount != 1:
        raise LookupError(f"section {section_id} not found in source {source_id}")


# --- chunks first, vectors later (the source is usable before its vectors are in) -----------

def put_chunks(conn: psycopg.Connection, user_id: str, source_id: str, chunks: list[ChunkRow], *,
               section_ids: list[str] | None = None) -> None:
    """Replace the chunks (of `section_ids` only, when given) with these, without vectors: BM25
    finds them at once, `set_chunk_embeddings` adds the vectors."""
    _require_owner(conn, user_id, source_id)
    if section_ids is None:
        conn.execute("delete from source_chunk where source_id = %s", (source_id,))
    else:
        conn.execute("delete from source_chunk where source_id = %s and section_id = any(%s)",
                     (source_id, section_ids))
    for c in chunks:
        if c.source_id != source_id or (section_ids is not None
                                        and c.section_id not in section_ids):
            raise ValueError(f"chunk {c.chunk_id} is not in the replaced part of {source_id}")
        conn.execute(
            "insert into source_chunk (chunk_id, section_id, source_id, ord, page, text) "
            "values (%s, %s, %s, %s, %s, %s)",
            (c.chunk_id, c.section_id, source_id, c.ord, c.page, c.text))


def _stale(alias: str, embedder: Embedder | None) -> tuple[str, tuple]:
    """SQL + params: this row has no vector, or (with `embedder`) one from another vector space."""
    if embedder is None:
        return f"{alias}.embedding is null", ()
    sql = (f"({alias}.embedding is null or ({alias}.embedder_id, {alias}.model_version, "
           f"{alias}.dim) is distinct from (%s, %s, %s))")
    return sql, (embedder.id, embedder.model_version, embedder.dim)


def unembedded_chunks(conn: psycopg.Connection, user_id: str, source_id: str,
                      embedder: Embedder | None = None) -> list[ChunkRow]:
    """This source's chunks that have no vector yet, in order. With `embedder`, also those whose
    vector is tagged with another (embedder_id, model_version, dim): they are re-embedded, and
    search ignores them (BM25 only) until then."""
    cond, params = _stale("c", embedder)
    return [_row(ChunkRow, r) for r in fetch_all(
        conn, "select c.chunk_id, c.section_id, c.source_id, c.ord, c.page, c.text "
              "from source_chunk c join source s using (source_id) "
              "where c.source_id = %s and s.user_id = %s and s.deleted_at is null "
              f"and {cond} order by c.section_id, c.ord, c.chunk_id",
        (source_id, user_id, *params))]


def set_chunk_embeddings(conn: psycopg.Connection, user_id: str, source_id: str,
                         rows: list[tuple[str, list[float]]], embedder: Embedder) -> None:
    _require_owner(conn, user_id, source_id)
    for chunk_id, v in rows:
        cur = conn.execute(
            "update source_chunk set embedder_id = %s, model_version = %s, dim = %s, "
            "embedding = %s where chunk_id = %s and source_id = %s",
            (embedder.id, embedder.model_version, embedder.dim, _vec(v), chunk_id, source_id))
        if cur.rowcount != 1:
            raise LookupError(f"chunk {chunk_id} not found in source {source_id}")


def unembedded_sections(conn: psycopg.Connection, user_id: str, source_id: str,
                        embedder: Embedder | None = None) -> list[str]:
    """section_ids of this source without a vector yet (with `embedder`: or with another vector
    space's), in order."""
    cond, params = _stale("t", embedder)
    return [r["section_id"] for r in fetch_all(
        conn, "select t.section_id from source_section t join source s using (source_id) "
              "where t.source_id = %s and s.user_id = %s and s.deleted_at is null "
              f"and {cond} order by t.ord, t.section_id", (source_id, user_id, *params))]


def sources_with_stale_vectors(conn: psycopg.Connection, embedder: Embedder,
                               limit: int = 1000) -> list[tuple[str, str]]:
    """(user_id, source_id) of live, not-being-imported sources that have a vector tagged with
    another vector space than `embedder`'s. Rows with no vector at all are the importer's job
    (`status = 'reading'`) or already BM25-only after a failure and are left alone here."""
    tag = (embedder.id, embedder.model_version, embedder.dim)
    return [(r["user_id"], r["source_id"]) for r in fetch_all(
        conn, "select s.user_id, s.source_id from source s where s.deleted_at is null "
              "and s.status <> 'reading' and (exists (select 1 from source_chunk c "
              "where c.source_id = s.source_id and c.embedding is not null "
              "and (c.embedder_id, c.model_version, c.dim) is distinct from (%s, %s, %s)) "
              "or exists (select 1 from source_section t where t.source_id = s.source_id "
              "and t.embedding is not null "
              "and (t.embedder_id, t.model_version, t.dim) is distinct from (%s, %s, %s))) "
              "order by s.created_at, s.source_id limit %s", (*tag, *tag, limit))]
