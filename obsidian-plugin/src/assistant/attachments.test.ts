import { describe, expect, it } from "vitest";
import { FakeWikiApi } from "../wiki/fake";
import type { SourceOut } from "../wiki/schema";
import {
  applyProgress, applyUploaded, blocksSend, canSend, expiresInDays, fromDraft, fromOut, guessKind, learning, mergeOut, newDraft,
  sourcesPort, type AttachProgress, type DraftAttachment,
} from "./attachments";
import { parseEvent, parseSessionOut } from "./parse";
import type { AttachmentOut } from "./types";

const OUT = (over: Partial<SourceOut> = {}): SourceOut => ({
  id: "src_1", title: "讲义.pdf", kind: "pdf", pages: 12, status: "reading", error: null, error_code: null, unreadable_pages: [],
  progress: { stage: "parse", done: 1, total: 12, message: "正在读 1/12 页" }, note: null, origin: null,
  created_at: "2026-10-21T00:00:00Z", deleted: false, uses: [], can_retry: false, ...over,
});
const draft = (): DraftAttachment => newDraft("l1", { name: "讲义.pdf", type: "application/pdf", size: 2048 });

describe("draft state", () => {
  it("a new draft is uploading, with the kind guessed from the MIME type or extension", () => {
    expect(draft()).toEqual({ localId: "l1", name: "讲义.pdf", kind: "pdf", bytes: 2048, pages: null, status: "uploading" });
    expect(guessKind("a.PNG", "image/png")).toBe("image");
    expect(guessKind("notes.md", "")).toBe("md");
    expect(guessKind("README", "")).toBe("");
    expect(newDraft("l2", { name: "x.png", type: "image/png", size: 1 }, "blob:x").thumb).toBe("blob:x");
  });
  it("upload → reading with progress → usable (progress gone) → later items change nothing", () => {
    const d = draft();
    applyUploaded(d, OUT());
    expect(d).toMatchObject({ sourceId: "src_1", status: "reading", pages: 12, progress: { done: 1, total: 12 } });
    expect(d.progress).toEqual({ done: 1, total: 12 });           // the server's message (counts) is not kept
    applyProgress(d, { kind: "progress", done: 5, total: 12 });
    expect(d.progress).toEqual({ done: 5, total: 12 });
    applyProgress(d, { kind: "usable", status: "partial" });
    expect(d.status).toBe("partial");
    expect(d.progress).toBeUndefined();
    applyProgress(d, { kind: "error", detail: "后台补读失败" });
    expect(d.status).toBe("partial");
  });
  it("a same-content upload (200, already ready) is usable at once; a failed one carries the reason", () => {
    const a = draft(); applyUploaded(a, OUT({ status: "ready", progress: null }));
    expect(a.status).toBe("ready");
    const b = draft(); applyUploaded(b, OUT({ status: "failed", error: "读不出这份 PDF", progress: null }));
    expect(b).toMatchObject({ status: "failed", error: "读不出这份 PDF" });
    const c = draft(); applyUploaded(c, OUT()); applyProgress(c, { kind: "error", detail: "读不出来" });
    expect(c).toMatchObject({ status: "failed", error: "读不出来" });
    const d = draft(); applyUploaded(d, OUT()); applyProgress(d, { kind: "done", status: "ready" });
    expect(d.status).toBe("ready");
  });
  it("uploading and reading block the send; ready, partial and failed do not", () => {
    for (const s of ["uploading", "reading"] as const) expect(blocksSend({ ...draft(), status: s })).toBe(true);
    for (const s of ["ready", "partial", "failed"] as const) expect(blocksSend({ ...draft(), status: s })).toBe(false);
  });
});

describe("canSend", () => {
  const base = { busy: false, draft: "", quote: null, attachments: [] as DraftAttachment[], restore: "none" };
  const ready = { ...draft(), status: "ready" as const };
  it("needs text, a quote or an attachment", () => {
    expect(canSend(base)).toBe(false);
    expect(canSend({ ...base, draft: "  " })).toBe(false);
    expect(canSend({ ...base, draft: "问" })).toBe(true);
    expect(canSend({ ...base, quote: { text: "q" } })).toBe(true);
    expect(canSend({ ...base, attachments: [ready] })).toBe(true);
  });
  it("a failed attachment alone sends nothing; with words the words go", () => {
    const failed = { ...draft(), status: "failed" as const };
    expect(canSend({ ...base, attachments: [failed] })).toBe(false);
    expect(canSend({ ...base, draft: "问", attachments: [failed] })).toBe(true);
  });
  it("is off while busy, restoring, over length, or with an attachment uploading / reading", () => {
    expect(canSend({ ...base, draft: "问", busy: true })).toBe(false);
    expect(canSend({ ...base, draft: "问", restore: "loading" })).toBe(false);
    expect(canSend({ ...base, draft: "长".repeat(4001) })).toBe(false);
    for (const s of ["uploading", "reading"] as const) {
      expect(canSend({ ...base, draft: "问", attachments: [ready, { ...draft(), status: s }] })).toBe(false);
    }
  });
});

describe("turn attachments", () => {
  const A: AttachmentOut = { source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask"],
    bytes: 2048, pages: 12, expires_at: "2026-11-20T00:00:00Z", read: { mode: "toc", sections: 4, pages_total: null, pages_read: null, tokens: null, unseen_pages: [] }, unreadable_pages: [], chars: null, importing: false };
  it("fromOut: the server's record, no click state; the read mode is not kept; learning = a use beyond ask", () => {
    expect(fromOut(A)).toEqual({ sourceId: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask"],
      bytes: 2048, pages: 12, expiresAt: "2026-11-20T00:00:00Z", unreadable: [], chars: null, importing: false, progress: null, error: "" });
    expect(fromOut({ ...A, status: "partial", unreadable_pages: [3, 4], chars: 12_345 })).toMatchObject(
      { unreadable: [3, 4], chars: 12_345 });
    expect(learning(["ask"])).toBe(false);
    expect(learning(["ask", "exam"])).toBe(true);
    expect(fromOut({ source_id: "s", title: "t", kind: "image", status: "ready", deleted: true, uses: [], bytes: 0, pages: null, expires_at: null, unreadable_pages: [], chars: null, read: null, importing: false })).toMatchObject(
      { uses: [], bytes: 0, pages: null, expiresAt: null, deleted: true });
  });
  it("fromDraft keeps the thumbnail; mergeOut takes the server's record and keeps thumbnails and a running import's bar", () => {
    const d = { ...newDraft("l1", { name: "a.png", type: "image/png", size: 5 }, "blob:a"), sourceId: "src_1", status: "ready" as const };
    const sent = fromDraft(d);
    expect(sent).toMatchObject({ sourceId: "src_1", title: "a.png", thumb: "blob:a", uses: ["ask"], progress: null, expiresAt: null });
    sent.progress = { done: 1, total: 4 };
    const [m] = mergeOut([sent], [{ ...A, uses: ["ask", "exam"] }]);
    expect(m).toMatchObject({ title: "讲义.pdf", thumb: "blob:a", uses: ["ask", "exam"], progress: { done: 1, total: 4 },
      expiresAt: "2026-11-20T00:00:00Z" });
  });
  it("expiresInDays: within 7 days only, rounded up", () => {
    const now = Date.parse("2026-10-21T00:00:00Z");
    expect(expiresInDays("2026-10-24T12:00:00Z", now)).toBe(4);
    expect(expiresInDays("2026-11-20T00:00:00Z", now)).toBeNull();
    expect(expiresInDays(null, now)).toBeNull();
    expect(expiresInDays("nope", now)).toBeNull();
  });
});

describe("parsing (fail closed)", () => {
  const A = { source_id: "s", title: "t", kind: "pdf", status: "ready", deleted: false, uses: ["ask"], bytes: 3, pages: null,
    expires_at: null, read: { mode: "full", pages_total: 3, pages_read: 3, sections: null, tokens: 120, unseen_pages: [] } };
  it("done and user messages carry attachments", () => {
    expect(parseEvent("done", { type: "done", stopped: false, message_id: "m", tool_calls: 0, llm_calls: 1, attachments: [A] })).toMatchObject(
      { attachments: [A] });
    const s = parseSessionOut({ session_id: "x", page_id: null, messages: [{ message_id: "m", role: "user", text: "", attachments: [A] }] });
    expect(s.messages[0]!.attachments).toEqual([A]);
  });
  it("an unknown field, a bad read mode or a missing flag is a ContractError", () => {
    const done = (a: unknown) => () => parseEvent("done", { type: "done", stopped: false, message_id: "m", tool_calls: 0, llm_calls: 1, attachments: [a] });
    expect(done({ ...A, note: "x" })).toThrow(/unknown field note/);
    expect(done({ ...A, read: { mode: "glance" } })).toThrow(/AttachmentRead.mode/);
    const noDeleted: Record<string, unknown> = { ...A };
    delete noDeleted.deleted;
    expect(done(noDeleted)).toThrow(/AttachmentOut.deleted/);
  });
  it("needs_vision is a known error code", () => {
    expect(parseEvent("error", { type: "error", code: "needs_vision", retryable: false, detail: "这张图片现在看不了" })).toMatchObject(
      { code: "needs_vision" });
  });
});

describe("sourcesPort (over the /wiki/sources client)", () => {
  it("uploads with use ask and the session id, follows progress to done (no use is added from the card)", async () => {
    const api = new FakeWikiApi();
    const port = sourcesPort(api);
    const out = await port.upload({ name: "讲义.pdf", data: new TextEncoder().encode("%PDF").buffer }, "ses_1");
    expect(out.uses.map((u) => [u.use, u.session_id])).toEqual([["ask", "ses_1"]]);
    const seen: AttachProgress[] = [];
    await port.follow(out.id, (p) => seen.push(p), new AbortController().signal);
    expect(seen[seen.length - 1]).toMatchObject({ kind: "done", status: "ready" });
    expect(seen.slice(0, -1).every((p) => p.kind === "progress")).toBe(true);
    expect(api.calls.some((c) => c.method === "addSourceUse")).toBe(false);
  });
  it("maps usable and error events", async () => {
    const port = sourcesPort({
      uploadSource: () => Promise.reject(new Error("unused")),
      followSource: (_id, on) => {
        on({ event: "usable", data: { title: null, status: "partial", pages: 3, sections: 2, unreadable_pages: [2], note: null } });
        on({ event: "error", data: { code: null, detail: "读不出来" } });
        return Promise.resolve();
      },
    });
    const seen: AttachProgress[] = [];
    await port.follow("s", (p) => seen.push(p), new AbortController().signal);
    expect(seen).toEqual([{ kind: "usable", status: "partial", unreadable: [2] }, { kind: "error", detail: "读不出来" }]);
  });
});
