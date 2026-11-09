import { ApiError, OfflineError } from "./api/errors";
import type { CreateUserIn, Lang, UserOut } from "./generated/service";
import type { ConnectionMonitor } from "./connection";
import { COPY } from "./copy";
import { type EvoWikiSettings, isValidUserId } from "./settings";

/**
 * The account check that the concept-era sidebar used to run (its `state.account`), kept on its own: GET /me for the
 * configured user. An unknown user id (a 404 from /me) is registered with POST /users, then /me is read again.
 * `ready` no longer carries the concept catalog (GET /concepts is gone).
 */
export type AccountState =
  | { kind: "needs-settings" } | { kind: "loading" } | { kind: "ready"; user: UserOut } | { kind: "error"; message: string };

export interface AccountDeps {
  me(): Promise<UserOut>;
  createUser(body: CreateUserIn): Promise<UserOut>;
  conn: Pick<ConnectionMonitor, "track" | "onChange">;
  settings: () => EvoWikiSettings;
  /** Obsidian's UI language (a locale such as "zh", "zh-TW", "en"); read when a user is registered. */
  uiLocale: () => string;
  /** A failed registration (the text to show), told once per attempt; not when the server is unreachable (the offline banner says that). */
  registerFailed: (text: string) => void;
  /** A new user now exists: what was read before that (404s) is read again. */
  registered?: () => void;
  prepareUser?: () => Promise<void>;
}

/** The survey a registered user starts from: every background answer "none" (POST /users, service/routes_users.py). */
export const DEFAULT_SURVEY = { math: "none", ml: "none", dl: "none" } as const;

/** Obsidian's UI locale → the backend's Lang: "zh" for any Chinese locale, else "en". */
export const langFromLocale = (locale: string): Lang => (locale.trim().toLowerCase().startsWith("zh") ? "zh" : "en");

function errorMessage(e: unknown): string {
  if (e instanceof OfflineError) return COPY.offline;
  if (e instanceof ApiError) return e.detail;
  return e instanceof Error ? e.message : String(e);
}

const isNotFound = (e: unknown): boolean => e instanceof ApiError && e.status === 404;

/**
 * init() runs on start (layout ready), after a new server / user id, and once more when the server comes back online
 * after a failed check. Each init() registers an unknown user at most once: a failed registration leaves the account in
 * `error` and is not retried until the next of those triggers (never a loop).
 */
export class AccountCheck {
  private _state: AccountState = { kind: "loading" };
  private token = 0;
  private readonly listeners = new Set<() => void>();
  private readonly unsubConn: () => void;

  constructor(private readonly d: AccountDeps) {
    this.unsubConn = d.conn.onChange((conn) => {
      if (conn === "online" && this._state.kind === "error") void this.init();
    });
  }

  get state(): AccountState { return this._state; }
  subscribe(fn: () => void): () => void { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; }

  async init(): Promise<void> {
    const t = ++this.token;
    const { userId } = this.d.settings();
    if (!isValidUserId(userId)) { this.set({ kind: "needs-settings" }); return; }
    this.set({ kind: "loading" });
    const live = (): boolean => t === this.token;
    let user: UserOut;
    let created = false;
    try {
      user = await this.d.conn.track(this.d.me());
    } catch (e) {
      if (!live()) return;
      if (!isNotFound(e)) { this.set({ kind: "error", message: errorMessage(e) }); return; }
      try {
        try {
          await this.d.conn.track(this.d.createUser({ user_id: userId, survey: DEFAULT_SURVEY, lang: langFromLocale(this.d.uiLocale()) }));
        } catch (e3) {
          // 409: the user exists already (made meanwhile elsewhere): fine, /me is read again below.
          if (!(e3 instanceof ApiError && e3.status === 409)) throw e3;
        }
        if (!live()) return;
        created = true;
        user = await this.d.conn.track(this.d.me());
      } catch (e2) {
        if (!live()) return;
        const message = errorMessage(e2);
        this.set({ kind: "error", message });
        if (!(e2 instanceof OfflineError)) this.d.registerFailed(COPY.registerFailed(userId, message));
        return;
      }
    }
    if (!live()) return;
    try { await this.d.prepareUser?.(); } catch (e) {
      if (live()) this.set({ kind: "error", message: errorMessage(e) });
      return;
    }
    if (!live()) return;
    this.set({ kind: "ready", user });
    if (created) this.d.registered?.();
  }

  dispose(): void { this.token++; this.unsubConn(); this.listeners.clear(); }

  private set(s: AccountState): void { this._state = s; for (const l of [...this.listeners]) l(); }
}
