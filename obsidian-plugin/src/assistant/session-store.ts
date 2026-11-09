/** Which note or page a conversation belongs to, and where its session id is remembered (ids only, never text). */
import { MAX_ASSISTANT_SESSIONS } from "../settings";

export type Target = { kind: "page"; pageId: string; title: string } | { kind: "note"; path: string; conceptId: string; title: string };
export type ActiveNote = { kind: "none" } | { kind: "unsupported"; title: string } | Target;
export interface Identity { server: string; user: string }
/** What a conversation belongs to: a note or page, or (no note shown) nothing: a session without a page. */
export type ConvTarget = Target | { kind: "free" };

export function sessionKey(id: Identity, t: ConvTarget): string {
  return JSON.stringify([id.server, id.user, t.kind === "page" ? "page:" + t.pageId : t.kind === "note" ? "note:" + t.path : "free"]);
}

export interface AssistantSessionStore {
  get(key: string): string | null; set(key: string, sessionId: string | null): Promise<void>;
  /** Every key, oldest first (the most recently set last). */
  keys?(): string[];
}

const own = (r: object, k: string): boolean => Object.prototype.hasOwnProperty.call(r, k);

/**
 * Over plugin data: own-property reads; a set re-appends the key (most recent last) and drops the oldest beyond `max`
 * (MAX_ASSISTANT_SESSIONS by default; goal runs and profile bases use the same store).
 */
export function recordStore(rec: () => Record<string, string>, save: () => Promise<void>, max = MAX_ASSISTANT_SESSIONS): AssistantSessionStore {
  return {
    keys: () => Object.keys(rec()),
    get(key) {
      const r = rec();
      if (!own(r, key)) return null;
      const v = r[key];
      return typeof v === "string" ? v : null;
    },
    async set(key, sessionId) {
      const r = rec();
      if (own(r, key)) delete r[key];
      if (sessionId !== null) {
        // defineProperty, not `[key] =`: a "__proto__" key would otherwise set the prototype.
        Object.defineProperty(r, key, { value: sessionId, writable: true, enumerable: true, configurable: true });
        const keys = Object.keys(r);
        for (const k of keys.slice(0, Math.max(0, keys.length - max))) delete r[k];
      }
      await save();
    },
  };
}
