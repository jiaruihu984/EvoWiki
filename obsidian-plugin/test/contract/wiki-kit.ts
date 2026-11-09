/** Shared by the two personal-wiki contract tests (wiki.contract.test.ts, wiki-idk.contract.test.ts). */
import * as http from "node:http";
import { OfflineError } from "../../src/api/errors";
import type { WikiHttp } from "../../src/wiki/api";
import { SOURCE_EVENT_MODELS } from "../../src/wiki/schema";
import { claimsPage } from "../../src/wiki/note/format";
import type { WikiNoteVault } from "../../src/wiki/note/sync";

export const NOTES = "EvoWiki";
export const FOLDER = `${NOTES}/Wiki`;

export const ORGANIZE_MODELS: Record<string, string> = {
  planning: "PlanningEvent", outline: "OutlineEvent", writer: "WriterEvent", section_added: "SectionAddedEvent", section_delta: "SectionDeltaEvent", section_done: "SectionDoneEvent",
  done: "OrganizeDoneEvent", error: "ErrorEvent" };
export const REWRITE_MODELS: Record<string, string> = {
  writer: "WriterEvent", section_added: "SectionAddedEvent", section_delta: "SectionDeltaEvent", section_done: "SectionDoneEvent", change: "ChangeOut",
  done: "RewriteDoneEvent", error: "ErrorEvent" };
/** GET /wiki/sources/{id}/progress (SSE): event → pydantic model. */
export const SOURCE_MODELS: Record<string, string> = { ...SOURCE_EVENT_MODELS };
export const SEG = "[^/]+";
/** A route whose 2xx body is raw bytes (the original of a source), not JSON. */
export const BYTES = "<bytes>";
/**
 * /wiki/sources (资料) besides the progress stream (SOURCE_MODELS); all driven by wiki.contract.test.ts's sources test.
 * Same shape as ROUTES; BYTES = a non-empty non-JSON body.
 */
export const SOURCE_ROUTES: [string, RegExp, string | null][] = [
  ["POST", /^\/wiki\/sources$/, "SourceOut"],
  ["GET", /^\/wiki\/sources$/, "SourceOut[]"],
  ["GET", new RegExp(`^/wiki/sources/${SEG}$`), "SourceDetail"],
  ["PATCH", new RegExp(`^/wiki/sources/${SEG}$`), "SourceOut"],
  ["GET", new RegExp(`^/wiki/sources/${SEG}/file$`), BYTES],
  ["POST", new RegExp(`^/wiki/sources/${SEG}/uses$`), "SourceUseOut"],
  ["DELETE", new RegExp(`^/wiki/sources/${SEG}$`), null],
];
/** JSON routes → the pydantic model of a 2xx body (`Model[]` = a JSON list, each item checked); null = 204 (empty body). */
export const ROUTES: [string, RegExp, string | null][] = [
  ...SOURCE_ROUTES,
  ["POST", new RegExp(`^/wiki/goals/${SEG}/answers$`), "GoalDraftOut"],
  ["POST", new RegExp(`^/wiki/goals/${SEG}/stop$`), "GoalDraftOut"],
  ["GET", new RegExp(`^/wiki/maps/${SEG}$`), "GoalMapOut"],
  ["POST", new RegExp(`^/wiki/maps/${SEG}/regions/${SEG}/enter$`), "GoalDraftOut"],
  ["GET", new RegExp(`^/wiki/pages/${SEG}$`), "PageOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/open(\\?rewrite=1)?$`), "PageOut"],
  ["GET", /^\/wiki\/progress$/, "ProgressOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/sections/${SEG}/(fold|pin)$`), "SectionOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/sections/${SEG}/feedback$`), "FeedbackOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/checks/${SEG}$`), "CheckAnswerOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/done$`), "DoneOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/(expand|collapse)$`), "ShelveOut"],
  ["GET", /^\/wiki$/, "CatalogOut"],
  ["GET", /^\/wiki\/k$/, "KGridOut"],
  ["POST", new RegExp(`^/wiki/k/cells/${SEG}/declare$`), "DeclareOut"],
  ["GET", /^\/wiki\/settings$/, "SettingsIO"],
  ["PUT", /^\/wiki\/settings$/, "SettingsIO"],
  ["GET", /^\/wiki\/guidelines$/, "GuidelinesOut"],
  ["GET", /^\/wiki\/teach\/spec$/, "TeachSpecOut"],
  ["POST", /^\/wiki\/teach\/items(\?base_version=\d+)?$/, "TeachSpecOut"],
  ["DELETE", /^\/wiki\/teach\/items\/[^/?]+(\?base_version=\d+)?$/, "TeachSpecOut"],
  ["PATCH", /^\/wiki\/teach\/items\/[^/?]+(\?base_version=\d+)?$/, "TeachSpecOut"],
  ["POST", /^\/wiki\/teach\/exemplars(\?base_version=\d+)?$/, "TeachSpecOut"],
  ["DELETE", /^\/wiki\/teach\/exemplars\/[^/?]+(\?base_version=\d+)?$/, "TeachSpecOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/feedback$`), "PageFeedbackOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/feedback/${SEG}/undo$`), "FeedbackUndoOut"],
  ["GET", /^\/wiki\/components$/, "ComponentOut[]"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/hand-edit$`), "HandEditOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/hand-edit/${SEG}/undo$`), "HandEditOut"],
  ["POST", new RegExp(`^/wiki/pages/${SEG}/fit$`), "SignalCreatedOut"],
  ["GET", /^\/wiki\/bot\/recent(\?.*)?$/, "ActivityOut[]"],
  ["GET", /^\/wiki\/bot\/review-reminder$/, "ReviewReminderOut"],
  ["GET", /^\/wiki\/rsi\/changes$/, "RsiChangeOut[]"],
  ["POST", new RegExp(`^/wiki/rsi/patches/${SEG}/(accept|reject|undo)$`), "RsiChangeOut"],
  ["POST", /^\/wiki\/guidelines$/, "GuidelineOut"],
  ["DELETE", new RegExp(`^/wiki/guidelines/${SEG}$`), null],
  ["GET", /^\/wiki\/review\/today$/, "ReviewTodayOut"],
  ["POST", /^\/wiki\/review\/grade$/, "ReviewGradeOut"],
  ["GET", /^\/wiki\/review\/digest\?period=(day|week)$/, "DigestOut"],
  ["GET", new RegExp(`^/wiki/paths/${SEG}$`), "PathOut"],
  ["POST", new RegExp(`^/wiki/paths/${SEG}/next$`), "PathNextOut"],
  ["POST", new RegExp(`^/wiki/paths/${SEG}/pages/\\d+/done$`), "PathPageDoneOut"],
];

/**
 * WikiHttp over node:http (test/node-transports.ts types only GET / POST; the wiki needs PUT and DELETE too), with an
 * ArrayBuffer body (multipart) and the response bytes when `binary` is asked for (like transport.ts).
 */
export const nodeWikiHttp: WikiHttp = {
  request: (req) => new Promise((resolve, reject) => {
    const r = http.request(new URL(req.url), { method: req.method, headers: req.headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => { chunks.push(c); });
      res.on("end", () => {
        const all = Buffer.concat(chunks);
        const status = res.statusCode ?? 0;
        const text = all.toString("utf8");
        if (!req.binary || status < 200 || status >= 300) { resolve({ status, text }); return; }
        resolve({ status, text: "", bytes: all.buffer.slice(all.byteOffset, all.byteOffset + all.byteLength) });
      });
      res.on("error", (e) => reject(new OfflineError(e)));
    });
    r.on("error", (e) => reject(new OfflineError(e)));
    if (req.body !== undefined) r.write(typeof req.body === "string" ? req.body : Buffer.from(req.body));
    r.end();
  }),
};

/** E3a's WikiNoteVault in memory; `writes` keeps the order of every write. */
export class MemVault implements WikiNoteVault {
  readonly files = new Map<string, string>();
  readonly folders = new Set<string>();
  list(p: string) {
    const direct = (paths: Iterable<string>) => [...paths].filter((f) => f.startsWith(`${p}/`) && !f.slice(p.length + 1).includes("/"));
    return Promise.resolve({ files: direct(this.files.keys()), folders: direct(this.folders) });
  }
  removeFile(p: string) { this.files.delete(p); return Promise.resolve(); }
  removeEmptyDir(p: string) { this.folders.delete(p); return Promise.resolve(); }

  readonly writes: string[] = [];
  rename(from: string, to: string) { const t = this.files.get(from); if (t === undefined || this.files.has(to)) return Promise.reject(new Error("cannot rename")); this.files.delete(from); this.files.set(to, t); return Promise.resolve(); }
  trash(p: string) { this.files.delete(p); return Promise.resolve(); }
  occupied(p: string): Promise<boolean> {
    const l = p.toLowerCase();
    return Promise.resolve([...this.files.keys(), ...this.folders].some((x) => x.toLowerCase() === l));
  }
  read(p: string): Promise<string> {
    const t = this.files.get(p);
    return t === undefined ? Promise.reject(new Error(`missing ${p}`)) : Promise.resolve(t);
  }
  create(p: string, text: string): Promise<void> {
    this.writes.push(`create ${p}`);
    if (this.files.has(p)) return Promise.reject(new Error(`exists ${p}`));
    this.files.set(p, text);
    return Promise.resolve();
  }
  process(p: string, fn: (d: string) => string): Promise<string> {
    this.writes.push(`process ${p}`);
    const t = this.files.get(p);
    if (t === undefined) return Promise.reject(new Error(`missing ${p}`));
    const out = fn(t);
    this.files.set(p, out);
    return Promise.resolve(out);
  }
  ensureFolder(p: string): Promise<void> {
    const parts = p.split("/");
    for (let i = 1; i <= parts.length; i++) this.folders.add(parts.slice(0, i).join("/"));
    return Promise.resolve();
  }
  listMarkdown(folder: string): string[] {
    const pre = folder.toLowerCase() + "/";
    return [...this.files.keys()].filter((p) => p.toLowerCase().startsWith(pre) && p.endsWith(".md")).sort();
  }
  readHidden(p: string): Promise<string | null> { return Promise.resolve(this.files.get(p) ?? null); }
  claiming(pageId: string): string[] { return [...this.files].filter(([p, t]) => p.startsWith(`${FOLDER}/`) && claimsPage(t, pageId)).map(([p]) => p); }
}


/**
 * The note syncs of a new page's organize stream with a run of "updated" counted once: the first streamed write
 * "created", later streamed writes "updated", the landing full sync "unchanged". How many "updated" there are depends on
 * timing (at most one per landed section: a write that comes while a sync is running replaces the waiting one, notes.ts
 * enqueueSync), so compare this form plus an upper bound on the length.
 */
export function streamedSyncs(runs: readonly string[]): string[] {
  return runs.filter((r, i) => !(r === "updated" && runs[i - 1] === "updated"));
}
/**
 * A new page's note runs: "created", then "updated" for the streamed writes, ending with the landing full sync — "unchanged"
 * when the last streamed write already held the page, or "updated" when the full sync took the place of a streamed write
 * still waiting (notes.ts enqueueSync keeps one waiting input per page). Either way at most one per landed section + 1.
 */
export function isNewPageSyncs(runs: readonly string[], sections: number): boolean {
  const s = streamedSyncs(runs);
  const shapes = [["created", "updated", "unchanged"], ["created", "updated"]];
  return shapes.some((x) => x.join() === s.join()) && runs.length <= sections + 1;
}

export async function until(cond: () => boolean, ms = 15_000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error("timed out");
    await new Promise((r) => setTimeout(r, 10));
  }
}
