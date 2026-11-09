import { OfflineError } from "./api/errors";

export type ConnState = "unknown" | "online" | "offline";
/** Automatic re-pings after going offline; the last interval goes on until the server answers. */
export const BACKOFF_MS = [5_000, 15_000, 60_000, 300_000] as const;
export interface Timers { set(fn: () => void, ms: number): unknown; clear(handle: unknown): void }
const windowTimers: Timers = { set: (fn, ms) => window.setTimeout(fn, ms), clear: (h) => window.clearTimeout(h as number) };

export class ConnectionMonitor {
  private _state: ConnState = "unknown";
  private attempt = 0;
  private handle: unknown = null;
  private stopped = false;
  private readonly listeners = new Set<(s: ConnState) => void>();

  constructor(private readonly ping: () => Promise<void>, private readonly timers: Timers = windowTimers) {}

  get state(): ConnState { return this._state; }
  onChange(fn: (s: ConnState) => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }

  /** Wraps any server call: unreachable → offline (rethrown); any HTTP answer → online. */
  async track<T>(p: Promise<T>): Promise<T> {
    try { const v = await p; this.online(); return v; } catch (e) {
      if (e instanceof OfflineError) this.offline(); else if (!(e instanceof Error && e.name === "AbortedError")) this.online();
      throw e;
    }
  }

  async retryNow(): Promise<void> { this.cancel(); this.attempt = 0; await this.probe(); }
  stop(): void { this.stopped = true; this.cancel(); }

  private online(): void { this.cancel(); this.attempt = 0; this.set("online"); }
  private offline(): void { this.set("offline"); this.schedule(); }
  private set(s: ConnState): void { if (s === this._state) return; this._state = s; for (const l of this.listeners) l(s); }
  private cancel(): void { if (this.handle !== null) { this.timers.clear(this.handle); this.handle = null; } }
  private schedule(): void {
    if (this.stopped || this.handle !== null) return;
    const ms = BACKOFF_MS[Math.min(this.attempt++, BACKOFF_MS.length - 1)]!;
    this.handle = this.timers.set(() => { this.handle = null; void this.probe(); }, ms);
  }
  private async probe(): Promise<void> {
    if (this.stopped) return;
    try { await this.ping(); this.online(); } catch (e) { if (e instanceof OfflineError) this.offline(); else this.online(); }
  }
}
