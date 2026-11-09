/**
 * FakeWikiApi's 资料 (/wiki/sources) in memory: what the backend's routes_sources.py answers, with the import pretended
 * step by step (FakeWikiApi.followSource emits `steps`, then `finish`). Every value has exactly the pydantic field set
 * of source_schemas.py / sources/importer.py (schema-parity.test.ts checks this). Returns live records: the caller copies.
 */
import { ApiError } from "../api/errors";
import type {
  ExamSummaryOut,
  SectionEditIn, SourceDetail, SourceDone, SourceEvent, SourceKind, SourceOut, SourceProgress, SourceSectionOut, SourceUploadIn, SourceUseIn,
  SourceUseOut,
} from "./schema";

/** EVOWIKI_SOURCE_MAX_MB's default (sources/config.py). */
const MAX_BYTES = 50 * 1024 * 1024;
/** sources/parse.py SUPPORTED. */
const KIND_BY_EXT: Record<string, SourceKind> = {
  ".pdf": "pdf", ".pptx": "pptx", ".docx": "docx", ".xlsx": "xlsx", ".md": "md", ".markdown": "md", ".txt": "text",
  ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image" };
const PAGED: ReadonlySet<SourceKind> = new Set(["pdf", "pptx"]);
// the backend's user-facing details (routes_sources.py, sources/parse.py, sources/importer.py)
const MSG_NOT_FOUND = "找不到这份资料";
const MSG_ONE_INPUT = "文件、网址、文字三选一";
const MSG_TOO_BIG = "文件太大了，上限 50 MB";
const MSG_UNSUPPORTED = `不支持这种文件，目前支持：${Object.keys(KIND_BY_EXT).join("、")}`;
const MSG_URL = "打不开这个网址，可以把正文贴进来";
/** sources/parse_other.py MSG_EMPTY: pasted text with nothing readable in it (whitespace only). */
const MSG_EMPTY = "文件里没有可读的文字";
const MSG_EMPTY_TITLE = "名字不能为空";
const MSG_LONG_TITLE = "名字太长了";
// PATCH …/sections (routes_sources.py, source_schemas.py SectionEditIn)
const MSG_STILL_READING = "资料还在读，读完再改目录";
const MSG_NO_SECTION = "目录里没有这一节";
const MSG_SECTION_TITLE = "标题不能为空";
const MSG_LONG_SECTION = "标题太长了";
const MSG_DEPTH = "层级只能是 0 到 2";
const MAX_DEPTH = 2;
const UNTITLED = "未命名资料";
const FAKE_SUMMARY = "（示例摘要）这一节讲了什么，导入时由模型写。";

interface Rec {
  out: SourceOut; key: string; data: ArrayBuffer; sections: SourceSectionOut[]; uses: SourceUseOut[];
  /** The sections the import will write (shown once it is done). */ planned: SourceSectionOut[];
  /** This import ends with this error (__failSourceImport). */ fail: string | null;
}

const enc = new TextEncoder();
const err = (status: number, detail: string): ApiError => new ApiError(status, detail);
const stem = (name: string): string => {
  const base = name.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  return (dot > 0 ? base.slice(0, dot) : base).trim() || UNTITLED;
};
/** FNV-1a over the bytes: the fake's stand-in for the backend's sha256 dedup. */
function digest(data: ArrayBuffer): string {
  let h = 0x811c9dc5;
  for (const b of new Uint8Array(data)) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return `${data.byteLength}:${h.toString(16)}`;
}

export class FakeSourceStore {
  private readonly recs = new Map<string, Rec>();
  private readonly exams = new Map<string, ExamSummaryOut>();
  private seq = 0;
  private failNext: string | null = null;

  constructor(private readonly now: () => Date) {}

  /** Test hook: the next new source's import ends with `error` {detail} and the source is failed. */
  failNextImport(detail: string): void { this.failNext = detail; }

  upload(input: SourceUploadIn): SourceOut {
    const file = input.file;
    const url = input.url?.trim() ?? "";
    const text = input.text ?? "";
    if ((file !== undefined ? 1 : 0) + (url ? 1 : 0) + (text ? 1 : 0) !== 1) throw err(422, MSG_ONE_INPUT);
    const title = input.title?.trim() || null;
    let kind: SourceKind;
    let data: ArrayBuffer;
    let name: string;
    let origin = input.origin || null;
    if (file !== undefined) {
      if (file.data.byteLength > MAX_BYTES) throw err(413, MSG_TOO_BIG);
      const dot = file.name.lastIndexOf(".");
      const k = KIND_BY_EXT[dot >= 0 ? file.name.slice(dot).toLowerCase() : ""];
      if (k === undefined) throw err(415, MSG_UNSUPPORTED);
      kind = k;
      data = file.data.slice(0);
      name = title ?? stem(file.name);
    } else if (url) {
      if (!/^https?:\/\/[^/\s]+/.test(url)) throw err(422, MSG_URL);
      kind = "url";
      data = enc.encode(`（示例网页）${url}`).buffer;
      name = title ?? (/^https?:\/\/([^/\s]+)/.exec(url)?.[1] ?? UNTITLED);
      origin = url;
    } else {
      if (!text.trim()) throw err(422, MSG_EMPTY);
      data = enc.encode(text).buffer;
      if (data.byteLength > MAX_BYTES) throw err(413, MSG_TOO_BIG);
      kind = "text";
      name = title ?? (text.trim().split("\n")[0]!.trim().slice(0, 30) || UNTITLED);
    }
    const key = digest(data);
    // store.find_by_sha: the same content again is that source, unless it was deleted or its import failed (a retry)
    let rec = [...this.recs.values()].find((r) => !r.out.deleted && r.out.status !== "failed" && r.key === key);
    if (rec === undefined) {
      const id = `src${++this.seq}`;
      const pages = PAGED.has(kind) ? 3 : null;
      const usesList: SourceUseOut[] = []; // shared by the record and its SourceOut (the list carries the uses)
      rec = {
        key, data, sections: [], uses: usesList, fail: this.failNext,
        planned: (pages === null ? [name.slice(0, 200)] : ["第 1 节", "第 2 节", "第 3 节"]).map((t, i) => ({
          id: `${id}-s${i + 1}`, parent_id: null, title: t, depth: 1, page_from: pages === null ? null : i + 1,
          page_to: pages === null ? null : i + 1, tokens: 400 + 100 * i, read_by: "text", summary: FAKE_SUMMARY })),
        out: { id, title: name.slice(0, 200), kind, pages: null, status: "reading", error: null, error_code: null, unreadable_pages: [],
          progress: null, note: null, origin,
          created_at: this.now().toISOString(), deleted: false, uses: usesList, can_retry: false },
      };
      rec.out.pages = pages;
      this.failNext = null;
      this.recs.set(id, rec);
    }
    if (input.use !== undefined) {
      this.addUse(rec.out.id, { use: input.use, goal_id: input.goal_id ?? null, page_id: input.page_id ?? null,
        session_id: input.session_id ?? null });
    }
    return rec.out;
  }

  /** Newest first (created_at desc, then id); a chat attachment used only to ask is not listed (routes_sources.list_sources). */
  list(): SourceOut[] {
    const askOnly = (r: Rec): boolean => r.uses.length > 0 && r.uses.every((u) => u.use === "ask");
    return [...this.recs.values()].filter((r) => !r.out.deleted && !askOnly(r)).map((r) => r.out)
      .sort((a, b) => (a.created_at === b.created_at ? (a.id < b.id ? -1 : 1) : a.created_at < b.created_at ? 1 : -1));
  }

  detail(id: string): SourceDetail {
    const r = this.live(id);
    return { ...r.out, sections: r.sections, page_offset: null, glossary: [], glossary_coverage: null,
      exam: this.exams.get(id) ?? null };
  }

  /** Test hook: the 真题 summary GET /wiki/sources/{id} answers for a source (null: none). */
  setExam(id: string, exam: ExamSummaryOut | null): void {
    if (exam === null) this.exams.delete(id); else this.exams.set(id, structuredClone(exam));
  }

  retry(id: string): SourceOut {
    const r = this.live(id);
    Object.assign(r.out, { status: "reading", error: null, error_code: null, can_retry: false });
    return r.out;
  }

  rename(id: string, title: string): SourceOut {
    const r = this.live(id);
    const t = title.trim();
    if (!t) throw err(422, MSG_EMPTY_TITLE);
    if (t.length > 200) throw err(422, MSG_LONG_TITLE);
    r.out.title = t;
    return r.out;
  }

  setOrigin(id: string, origin: string): SourceOut {
    const r = this.live(id);
    r.out.origin = origin;
    return r.out;
  }

  /** The backend's edit_sections: titles / levels change, each parent is the nearest earlier section one level up. */
  editSections(id: string, edits: SectionEditIn[]): SourceDetail {
    const r = this.live(id);
    if (r.out.status !== "ready" && r.out.status !== "partial") throw err(409, MSG_STILL_READING);
    const rows = r.sections.map((s) => ({ ...s }));
    for (const e of edits) {
      const s = rows.find((x) => x.id === e.id);
      if (s === undefined) throw err(422, MSG_NO_SECTION);
      if (e.title !== undefined && e.title !== null) {
        const t = e.title.trim();
        if (!t) throw err(422, MSG_SECTION_TITLE);
        if (t.length > 200) throw err(422, MSG_LONG_SECTION);
        s.title = t;
      }
      if (e.depth !== undefined && e.depth !== null) {
        if (!Number.isInteger(e.depth) || e.depth < 0 || e.depth > MAX_DEPTH) throw err(422, MSG_DEPTH);
        s.depth = e.depth;
      }
    }
    const open: SourceSectionOut[] = [];
    for (const s of rows) {
      while (open.length > 0 && open[open.length - 1]!.depth >= s.depth) open.pop();
      s.parent_id = open.length > 0 ? open[open.length - 1]!.id : null;
      open.push(s);
    }
    r.sections = rows;
    return this.detail(id);
  }

  addUse(id: string, u: SourceUseIn): SourceUseOut {
    const r = this.live(id);
    // `auto`: the server records the use it guesses from the material; the fake always guesses learn.
    const out: SourceUseOut = { use: u.use === "auto" ? "learn" : u.use, goal_id: u.goal_id ?? null, page_id: u.page_id ?? null, session_id: u.session_id ?? null,
      id: `use${++this.seq}`, created_at: this.now().toISOString() };
    r.uses.push(out);
    return out;
  }

  remove(id: string): void { this.live(id).out.deleted = true; }

  file(id: string): ArrayBuffer { return this.live(id).data.slice(0); }

  /** The progress items a follow of a reading source emits, in order; [] when its import has ended. */
  steps(id: string): SourceProgress[] {
    const r = this.live(id);
    if (r.out.status !== "reading") return [];
    if (r.fail !== null) return [{ stage: "parse", done: 0, total: 1, message: "正在解析" }];
    const n = r.planned.length;
    return [
      { stage: "parse", done: 0, total: 1, message: "正在解析" },
      { stage: "toc", done: 0, total: 1, message: "正在整理目录" },
      ...r.planned.map((_, i): SourceProgress => ({ stage: "embed", done: i + 1, total: n, message: `正在读 ${i + 1}/${n} 节` })),
      { stage: "summary", done: n, total: n, message: `正在读 ${n}/${n} 节` },
    ];
  }

  /** Records one progress item on the source (the list shows it without following the stream). */
  record(id: string, p: SourceProgress): void { this.live(id).out.progress = p; }

  /** Ends the import (once): done (ready, with its sections) or error (failed). An ended source answers its end again. */
  finish(id: string): SourceEvent {
    const r = this.live(id);
    if (r.out.status === "reading") {
      if (r.fail !== null) {
        Object.assign(r.out, { status: "failed", error: r.fail, progress: { stage: "parse", done: 0, total: 1, message: r.fail } });
      } else {
        r.sections = r.planned;
        const n = r.sections.length;
        Object.assign(r.out, { status: "ready", progress: { stage: "summary", done: n, total: n, message: `读完了，共 ${n} 节` } });
      }
    }
    if (r.out.status === "failed") return { event: "error", data: { code: r.out.error_code, detail: r.out.error ?? MSG_NOT_FOUND } };
    const done: SourceDone = { title: r.out.title, status: r.out.status, pages: r.out.pages, sections: r.sections.length,
      unreadable_pages: [...r.out.unreadable_pages], note: r.out.note };
    return { event: "done", data: done };
  }

  /** 404 for an unknown or deleted source. */
  private live(id: string): Rec {
    const r = this.recs.get(id);
    if (r === undefined || r.out.deleted) throw err(404, MSG_NOT_FOUND);
    return r;
  }
}
