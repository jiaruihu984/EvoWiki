/**
 * Reading time (B5 spec §1.6): counts the seconds a wiki page is actively read — its note is the active view, the
 * document is visible, the window has focus and the user did something (click, key, wheel, mouse move) in the last
 * IDLE_MS — and reports them in batches (FLUSH_MS, and on leaving the page / losing focus / closing), never per second.
 * Seconds are kept per page and local day; each entry is stamped with the last moment counted on that day, so the
 * server files it under the right day. A batch holds at most MAX_ENTRIES entries.
 * A batch that fails to send (network, 5xx, 408 / 429, or no answer within SEND_TIMEOUT_MS) is kept and resent with the
 * same batch id (the server counts a batch once); any other 4xx means the server will never take it: dropped. A batch
 * older than MAX_AGE_MS is dropped (the server would not count it). Nothing is sent while unconfigured, offline or in
 * the demo (the host's `canSend`).
 */
import { ApiError } from "../api/errors";
import type { ReadingEntry } from "./bot/bot-schema";
import { localDayKey } from "../ui/dates";

export const TICK_MS = 5_000;
export const FLUSH_MS = 60_000;
export const IDLE_MS = 90_000;
export const MAX_ENTRY_S = 600;
export const MAX_ENTRIES = 200;
export const SEND_TIMEOUT_MS = 30_000;
export const MAX_AGE_MS = 2 * 24 * 3600_000 - 3600_000;

export interface ReadingEnv {
  now(): number;
  /** The wiki page whose note is the active view, or null. */
  currentPage(): string | null;
  /** Document visible and the window focused. */
  attentive(): boolean;
  canSend(): boolean;
  send(batchId: string, entries: ReadingEntry[]): Promise<unknown>;
  newBatchId(): string;
}

interface Batch { id: string; entries: ReadingEntry[]; madeAt: number }
interface Counted { page: string; ms: number; last: number }

/** The counting key's day (in memory only: never stored or sent). */
const localDay = (t: number): string => localDayKey(new Date(t));
/** The server will never accept it: a 4xx other than 408 (timeout) / 429 (rate limit). */
const rejected = (e: unknown): boolean => e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;

export class ReadingReporter {
  private lastInput = -Infinity;
  private lastTick: number | null = null;
  private lastFlush: number;
  private readonly acc = new Map<string, Counted>();     // `${page}|${local day}` -> ms not reported yet
  private queue: Batch[] = [];
  private sending = false;

  constructor(private readonly env: ReadingEnv) { this.lastFlush = env.now(); }

  /** A click / key / wheel / mouse move. */
  activity(): void { this.lastInput = this.env.now(); }

  /** Every TICK_MS: count the time since the last tick for the page being read (at most two ticks' worth). */
  tick(): void {
    const now = this.env.now();
    const prev = this.lastTick;
    this.lastTick = now;
    if (prev !== null) {
      const page = this.env.currentPage();
      if (page !== null && this.env.attentive() && now - this.lastInput <= IDLE_MS) {
        const dt = Math.min(now - prev, 2 * TICK_MS);
        if (dt > 0) {
          const key = `${page}|${localDay(now)}`;
          const c = this.acc.get(key) ?? { page, ms: 0, last: now };
          this.acc.set(key, { page, ms: c.ms + dt, last: now });
        }
      }
    }
    if (now - this.lastFlush >= FLUSH_MS) void this.flush();
  }

  /** Turns what was counted into batches (whole seconds; the rest stays counted). */
  private batch(now: number): void {
    const entries: ReadingEntry[] = [];
    for (const [key, c] of this.acc) {
      let s = Math.floor(c.ms / 1000);
      const at = new Date(c.last).toISOString();
      while (s > 0) { const part = Math.min(s, MAX_ENTRY_S); entries.push({ page_id: c.page, seconds: part, at }); s -= part; }
      const rest = c.ms % 1000;
      if (rest > 0) this.acc.set(key, { ...c, ms: rest }); else this.acc.delete(key);
    }
    for (let i = 0; i < entries.length; i += MAX_ENTRIES) {
      this.queue.push({ id: this.env.newBatchId(), entries: entries.slice(i, i + MAX_ENTRIES), madeAt: now });
    }
  }

  /** Sends what was counted (the batches that failed first). Safe to call any time; one send at a time. */
  async flush(): Promise<void> {
    const now = this.env.now();
    this.lastFlush = now;
    if (this.sending || !this.env.canSend()) return;
    this.queue = this.queue.filter((b) => now - b.madeAt <= MAX_AGE_MS);
    this.batch(now);
    this.sending = true;
    try {
      while (this.queue.length > 0) {
        const b = this.queue[0]!;
        try {
          await this.sendOnce(b);
        } catch (e) {
          if (!rejected(e)) return;           // kept: the next flush resends the same batch id
        }
        this.queue.shift();                   // sent, or refused for good
      }
    } finally {
      this.sending = false;
    }
  }

  private sendOnce(b: Batch): Promise<unknown> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), SEND_TIMEOUT_MS); });
    return Promise.race([this.env.send(b.id, b.entries), timeout]).finally(() => clearTimeout(timer));
  }

  /** For tests: what waits to be sent. */
  get unsent(): { queue: Batch[]; counted: Map<string, number> } {
    const counted = new Map<string, number>();
    for (const c of this.acc.values()) counted.set(c.page, (counted.get(c.page) ?? 0) + c.ms);
    return { queue: this.queue.map((b) => ({ ...b })), counted };
  }
}
