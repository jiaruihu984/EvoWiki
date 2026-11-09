import { describe, expect, it, vi } from "vitest";
import { AccountCheck, langFromLocale } from "./account";
import { ApiError, OfflineError } from "./api/errors";
import type { CreateUserIn, UserOut } from "./generated/service";
import type { ConnState } from "./connection";
import { COPY } from "./copy";
import { DEFAULT_SETTINGS } from "./settings";

const USER: UserOut = { user_id: "jerry", background: "none", condition: "S2", lang: "zh" };
const NOT_FOUND = () => Promise.reject(new ApiError(404, "unknown user jerry"));

function rig(o: { me: () => Promise<UserOut>; createUser?: (b: CreateUserIn) => Promise<UserOut>; userId?: string; locale?: string;
  prepareUser?: () => Promise<void>; registered?: () => void }) {
  let onConn: ((s: ConnState) => void) | null = null;
  let tracked = 0;
  const conn = { track: <T,>(p: Promise<T>): Promise<T> => { tracked++; return p; },
    onChange: (fn: (s: ConnState) => void) => { onConn = fn; return () => { onConn = null; }; } };
  const me = vi.fn(o.me);
  const createUser = vi.fn(o.createUser ?? (() => Promise.resolve(USER)));
  const registerFailed = vi.fn();
  const a = new AccountCheck({ me, createUser, conn, registerFailed, uiLocale: () => o.locale ?? "en",
    prepareUser: o.prepareUser, registered: o.registered,
    settings: () => ({ ...DEFAULT_SETTINGS, userId: o.userId ?? "jerry" }) });
  return { a, me, createUser, registerFailed, tracked: () => tracked, online: () => onConn?.("online"), connected: () => onConn !== null };
}

describe("AccountCheck", () => {
  it("ready: GET /me through the connection monitor preserves the account payload", async () => {
    const r = rig({ me: () => Promise.resolve(USER) });
    expect(r.a.state).toEqual({ kind: "loading" });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "ready", user: USER });
    expect(r.tracked()).toBe(1);
    expect(r.createUser).not.toHaveBeenCalled();
  });

  it("an invalid or empty user id: needs-settings, nothing sent", async () => {
    const r = rig({ me: () => Promise.resolve(USER), userId: "" });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "needs-settings" });
    expect(r.me).not.toHaveBeenCalled();
  });

  it("an unknown user (404 from /me): one POST /users with the none survey and Obsidian's language, then /me again → ready", async () => {
    let registered = false;
    const r = rig({ me: () => (registered ? Promise.resolve(USER) : NOT_FOUND()), locale: "zh-TW",
      createUser: () => { registered = true; return Promise.resolve(USER); } });
    await r.a.init();
    expect(r.createUser.mock.calls).toEqual([[{ user_id: "jerry", survey: { math: "none", ml: "none", dl: "none" }, lang: "zh" }]]);
    expect(r.me).toHaveBeenCalledTimes(2);
    expect(r.a.state).toEqual({ kind: "ready", user: USER });
    expect(r.registerFailed).not.toHaveBeenCalled();
  });

  it("a failed registration: error, told once; no second POST until the next trigger (settings change / reconnect / 重试)", async () => {
    const r = rig({ me: NOT_FOUND, createUser: () => Promise.reject(new ApiError(422, "bad id")) });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "error", message: "bad id" });
    expect(r.registerFailed.mock.calls).toEqual([[COPY.registerFailed("jerry", "bad id")]]);
    expect(r.createUser).toHaveBeenCalledOnce();
    await new Promise((res) => setTimeout(res, 0));
    expect(r.createUser).toHaveBeenCalledOnce();             // nothing retries on its own
    r.online();                                              // the server back online after the error: one more try
    await vi.waitFor(() => expect(r.createUser).toHaveBeenCalledTimes(2));
    await new Promise((res) => setTimeout(res, 0));
    expect(r.createUser).toHaveBeenCalledTimes(2);
    expect(r.registerFailed).toHaveBeenCalledTimes(2);
  });

  it("a 409 from POST /users (the user exists already) counts as registered: /me is read again → ready, no notice", async () => {
    let calls = 0;
    const r = rig({ me: () => (++calls === 1 ? NOT_FOUND() : Promise.resolve(USER)),
      createUser: () => Promise.reject(new ApiError(409, "user jerry exists")) });
    await r.a.init();
    expect(r.createUser).toHaveBeenCalledOnce();
    expect(r.a.state).toEqual({ kind: "ready", user: USER });
    expect(r.registerFailed).not.toHaveBeenCalled();
  });

  it("registration while the server is unreachable: error (offline text), no notice (the offline banner says it)", async () => {
    const r = rig({ me: NOT_FOUND, createUser: () => Promise.reject(new OfflineError()) });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "error", message: COPY.offline });
    expect(r.registerFailed).not.toHaveBeenCalled();
  });

  it("other /me failures are error and register nothing", async () => {
    const offline = rig({ me: () => Promise.reject(new OfflineError()) });
    await offline.a.init();
    expect(offline.a.state).toEqual({ kind: "error", message: COPY.offline });
    const bad = rig({ me: () => Promise.reject(new ApiError(500, "boom")) });
    await bad.a.init();
    expect(bad.a.state).toEqual({ kind: "error", message: "boom" });
    expect(offline.createUser).not.toHaveBeenCalled();
    expect(bad.createUser).not.toHaveBeenCalled();
  });

  it("after an error, the server coming back online runs the check once more", async () => {
    let fail = true;
    const r = rig({ me: () => (fail ? Promise.reject(new OfflineError()) : Promise.resolve(USER)) });
    await r.a.init();
    fail = false;
    r.online();
    await vi.waitFor(() => expect(r.a.state).toEqual({ kind: "ready", user: USER }));
    r.online();                                   // ready: no further check
    expect(r.me).toHaveBeenCalledTimes(2);
  });

  it("only the latest init lands; subscribers hear every change; dispose stops listening", async () => {
    let resolveFirst: (u: UserOut) => void = () => undefined;
    const calls: Promise<UserOut>[] = [new Promise((res) => { resolveFirst = res; }), Promise.resolve({ ...USER, lang: "en" })];
    const r = rig({ me: () => calls.shift()! });
    const heard = vi.fn();
    r.a.subscribe(heard);
    const first = r.a.init();
    await r.a.init();
    resolveFirst(USER);
    await first;
    expect(r.a.state).toEqual({ kind: "ready", user: { ...USER, lang: "en" } });
    expect(heard).toHaveBeenCalled();
    r.a.dispose();
    expect(r.connected()).toBe(false);
  });

  it("a newly registered account waits for budget confirmation before ready or registered callbacks", async () => {
    let created = false;
    let confirm = (): void => undefined;
    const registered = vi.fn();
    const prepareUser = vi.fn(() => new Promise<void>((resolve) => { confirm = resolve; }));
    const r = rig({ me: () => created ? Promise.resolve(USER) : NOT_FOUND(),
      createUser: () => { created = true; return Promise.resolve(USER); }, prepareUser, registered });
    const init = r.a.init();
    await vi.waitFor(() => expect(prepareUser).toHaveBeenCalledOnce());
    expect(r.a.state.kind).toBe("loading");
    expect(registered).not.toHaveBeenCalled();
    confirm();
    await init;
    expect(r.a.state).toEqual({ kind: "ready", user: USER });
    expect(registered).toHaveBeenCalledOnce();
  });

  it("cancelling budget confirmation keeps the account blocked and an explicit retry can confirm", async () => {
    const prepareUser = vi.fn().mockRejectedValueOnce(new ApiError(403, "请先确认预算", "budget_confirmation_required"))
      .mockResolvedValueOnce(undefined);
    const r = rig({ me: () => Promise.resolve(USER), prepareUser });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "error", message: "请先确认预算" });
    expect(r.registerFailed).not.toHaveBeenCalled();
    expect(prepareUser).toHaveBeenCalledOnce();
    await r.a.init();
    expect(r.a.state.kind).toBe("ready");
    expect(prepareUser).toHaveBeenCalledTimes(2);
    expect(r.createUser).not.toHaveBeenCalled();
  });

  it("a failed budget read never marks the account ready", async () => {
    const r = rig({ me: () => Promise.resolve(USER), prepareUser: () => Promise.reject(new OfflineError()) });
    await r.a.init();
    expect(r.a.state).toEqual({ kind: "error", message: COPY.offline });
  });

  it("an old confirmation cannot change the state after a newer account check", async () => {
    let release = (): void => undefined;
    const prepareUser = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }))
      .mockResolvedValueOnce(undefined);
    const r = rig({ me: () => Promise.resolve(USER), prepareUser });
    const first = r.a.init();
    await vi.waitFor(() => expect(prepareUser).toHaveBeenCalledOnce());
    await r.a.init();
    const latest = r.a.state;
    release();
    await first;
    expect(r.a.state).toBe(latest);
  });
});

it("Obsidian's locale maps to the backend Lang: zh* → zh, anything else → en", () => {
  for (const l of ["zh", "zh-TW", "ZH-cn"]) expect(langFromLocale(l)).toBe("zh");
  for (const l of ["en", "en-GB", "ja", ""]) expect(langFromLocale(l)).toBe("en");
});
