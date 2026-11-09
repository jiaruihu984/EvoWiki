/**
 * The live status of the 学习路径 rows, split out of the 知识图谱 view.
 *
 * - Status: the shared background writes watcher (Task 13) polls GET /wiki/writes once for everybody; this reads its
 *   snapshot and paints only the rows' `.ew-w` spans (never the row, its menu or an open input). queued 排队中;
 *   planning 「正在规划… n 秒」 (the page counts the seconds itself from the first `planning` it saw - the backend sends no
 *   heartbeat; a 1 s timer runs only while something plans); writing `<i></i>生成中 n/m`; failed 「生成失败 · 重试」;
 *   done / undone / no run: nothing.
 * - End state: the watcher's snapshot holds live writes only, so `failed` / `undone` are never in it. When a page this
 *   view saw live leaves the list, its end state is read once with the free GET /wiki/writes?page_ids= (`lookup`):
 *   failed -> the row shows 生成失败 · 重试; done / undone / absent -> nothing.
 */
import { writeLine } from "../../assistant/card-state";
import type { WriteOut } from "../schema";
import { btn, el } from "../../ui/ew";
import { GRAPH_COPY } from "../graph-page/copy";
import type { GraphSource } from "../graph-page/store";

export interface LiveRowsHost {
  writes: GraphSource["writes"];
  /** The panel the rows live in (null: the view is closed). */
  panel(): HTMLElement | null;
  /** 重试 on a failed row. */
  retry(pageId: string): void;
  /** The end state of pages that left the live list (GET /wiki/writes?page_ids=, free). */
  lookup?(pageIds: string[]): Promise<readonly WriteOut[]>;
}

export class LiveRows {
  private writes = new Map<string, WriteOut>();
  private readonly planSince = new Map<string, number>();
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Pages seen live, whose end state is not known yet; the end states read (failed ones only matter). */
  private readonly seenLive = new Set<string>();
  private readonly looking = new Set<string>();
  private readonly ended = new Map<string, WriteOut["state"]>();
  private stopped = false;
  private off: (() => void) | null = null;

  constructor(private readonly host: LiveRowsHost) {}

  /** Subscribes to the watcher's snapshot (no source: no live rows). */
  start(): void {
    this.stopped = false;
    const w = this.host.writes;
    if (w === undefined) return;
    this.off = w.subscribe(() => { this.sync(); });
    this.sync();
  }

  stop(): void {
    this.stopped = true;
    this.off?.(); this.off = null;
    this.stopTimer();
  }

  /** The page's write is queued, planning or writing. */
  isLive(pageId: string): boolean {
    const s = this.writes.get(pageId)?.state;
    return s === "queued" || s === "planning" || s === "writing";
  }

  /** Re-reads the snapshot. */
  sync(): void {
    const snap = this.host.writes?.snapshot() ?? null;
    if (snap === null) return;
    this.writes = new Map(snap.map((w) => [w.page_id, w] as const));
    const now = performance.now();
    for (const [id, w] of this.writes) if (w.state === "planning" && !this.planSince.has(id)) this.planSince.set(id, now);
    for (const id of this.writes.keys()) if (this.isLive(id)) { this.seenLive.add(id); this.ended.delete(id); }
    this.read([...this.seenLive].filter((id) => !this.isLive(id)));
    for (const id of [...this.planSince.keys()]) if (this.writes.get(id)?.state !== "planning") this.planSince.delete(id);
    if (this.planSince.size > 0) this.startTimer(); else this.stopTimer();
    this.paintAll();
  }

  /** Paints every status span in the panel. */
  paintAll(): void {
    const panel = this.host.panel();
    if (panel === null) return;
    for (const e of panel.querySelectorAll<HTMLElement>(".ew-w[data-w]")) this.paint(e);
  }

  /** Paints one status span (`data-w` = the row's page id). */
  paint(e: HTMLElement): void {
    const id = e.dataset.w ?? "", line = this.line(id);
    e.hidden = line === null;
    const key = line === null ? "" : line.key;
    if (e.dataset.state === key) return;
    e.dataset.state = key;
    e.replaceChildren();
    if (line === null) return;
    if (line.pulse) e.append(el("i"));
    e.append(line.text);
    if (line.retry) {
      const b = btn(() => (GRAPH_COPY.rowRetry), "link", () => { this.host.retry(id); });
      b.dataset.k = "retry:" + id;
      e.append(b);
    }
  }

  private line(pageId: string): { key: string; text: string; pulse: boolean; retry: boolean } | null {
    const w = this.writes.get(pageId);
    if (w === undefined) return this.ended.get(pageId) === "failed" ? { key: "failed", text: GRAPH_COPY.rowFailed, pulse: false, retry: true } : null;
    if (w.state === "planning") {
      const since = this.planSince.get(pageId) ?? performance.now();
      const text = GRAPH_COPY.rowPlanning(Math.floor((performance.now() - since) / 1000));
      return { key: text, text, pulse: true, retry: false };
    }
    if (w.state === "failed") return { key: "failed", text: GRAPH_COPY.rowFailed, pulse: false, retry: true };
    if (w.state === "done" || w.state === "undone") return null;
    const l = writeLine(w);
    return l === null ? null : { key: l.key, text: l.text, pulse: l.pulse, retry: false };
  }

  /** Pages that left the live list: read how their writes ended, all in one free GET. */
  private read(pageIds: string[]): void {
    const ids = pageIds.filter((id) => !this.looking.has(id));
    if (this.host.lookup === undefined) { for (const id of ids) this.seenLive.delete(id); return; }
    if (ids.length === 0) return;
    for (const id of ids) this.looking.add(id);
    void this.host.lookup(ids).then((ws) => ws, () => [] as readonly WriteOut[]).then((ws) => {
      for (const id of ids) {
        this.looking.delete(id);
        if (this.stopped || this.isLive(id)) continue;
        this.seenLive.delete(id);
        const state = ws.find((x) => x.page_id === id)?.state;
        if (state === "queued" || state === "planning" || state === "writing") continue;   // live on the server, not (yet) in the snapshot
        if (state === undefined) this.ended.delete(id); else this.ended.set(id, state);
      }
      this.paintAll();
    });
  }

  private startTimer(): void {
    if (this.timer === null) this.timer = setInterval(() => { this.paintAll(); }, 1000);
  }
  private stopTimer(): void {
    if (this.timer !== null) { clearInterval(this.timer); this.timer = null; }
  }
}
