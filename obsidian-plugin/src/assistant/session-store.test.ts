import { describe, expect, it } from "vitest";
import { MAX_ASSISTANT_SESSIONS } from "../settings";
import { recordStore, sessionKey, type Target } from "./session-store";

const ID = { server: "http://127.0.0.1:8000", user: "jerry" };
const PAGE: Target = { kind: "page", pageId: "page_pca", title: "主成分分析" };
const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };

function rig(initial: Record<string, string> = {}) {
  const rec: Record<string, string> = { ...initial };
  let saves = 0;
  const store = recordStore(() => rec, () => { saves++; return Promise.resolve(); });
  return { rec, store, saves: () => saves };
}

describe("sessionKey", () => {
  it("is JSON [server, user, page:<id> | note:<path>]", () => {
    expect(sessionKey(ID, PAGE)).toBe(JSON.stringify(["http://127.0.0.1:8000", "jerry", "page:page_pca"]));
    expect(sessionKey(ID, NOTE)).toBe(JSON.stringify(["http://127.0.0.1:8000", "jerry", "note:ML/PCA.md"]));
  });
  it("page and note keys differ even with the same text, and identities differ", () => {
    const p: Target = { kind: "page", pageId: "x", title: "" };
    const n: Target = { kind: "note", path: "x", conceptId: "x", title: "" };
    expect(sessionKey(ID, p)).not.toBe(sessionKey(ID, n));
    expect(sessionKey({ ...ID, user: "other" }, p)).not.toBe(sessionKey(ID, p));
  });
});

describe("recordStore", () => {
  it("get reads own properties only", () => {
    const { store } = rig({ a: "ses_a" });
    expect(store.get("a")).toBe("ses_a");
    expect(store.get("toString")).toBeNull();
    expect(store.get("constructor")).toBeNull();
    expect(store.get("missing")).toBeNull();
  });
  it("set re-appends the key (most recent last) and saves once per set", async () => {
    const { rec, store, saves } = rig({ a: "1", b: "2", c: "3" });
    await store.set("a", "9");
    expect(Object.keys(rec)).toEqual(["b", "c", "a"]);
    expect(rec.a).toBe("9");
    expect(saves()).toBe(1);
    await store.set("d", "4");
    expect(Object.keys(rec)).toEqual(["b", "c", "a", "d"]);
    expect(saves()).toBe(2);
  });
  it("set(k, null) deletes", async () => {
    const { rec, store, saves } = rig({ a: "1", b: "2" });
    await store.set("a", null);
    expect(rec).toEqual({ b: "2" });
    expect(store.get("a")).toBeNull();
    expect(saves()).toBe(1);
  });
  it(`the ${MAX_ASSISTANT_SESSIONS + 1}th insert drops the oldest`, async () => {
    const init: Record<string, string> = {};
    for (let i = 0; i < MAX_ASSISTANT_SESSIONS; i++) init[`k${i}`] = `s${i}`;
    const { rec, store } = rig(init);
    await store.set("new", "s_new");
    const keys = Object.keys(rec);
    expect(keys).toHaveLength(MAX_ASSISTANT_SESSIONS);
    expect(keys[0]).toBe("k1");
    expect(keys[keys.length - 1]).toBe("new");
    expect(store.get("k0")).toBeNull();
  });
  it("a \"__proto__\" key round-trips as an own property", async () => {
    const { rec, store } = rig();
    await store.set("__proto__", "ses_p");
    expect(store.get("__proto__")).toBe("ses_p");
    expect(Object.keys(rec)).toEqual(["__proto__"]);
    expect(Object.getPrototypeOf(rec)).toBe(Object.prototype);
    await store.set("__proto__", null);
    expect(store.get("__proto__")).toBeNull();
    expect(Object.keys(rec)).toEqual([]);
  });
});
