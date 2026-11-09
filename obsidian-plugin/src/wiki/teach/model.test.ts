import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/errors";
import { SETTINGS_COPY, TEACH_COPY } from "../../copy";
import { FakeConn, methods } from "../controller-test-kit";
import { WIKI_COPY } from "../copy";
import { FakeWikiApi } from "../fake";
import type { TeachSpecOut } from "../schema";
import { TeachModel } from "./model";

function rig(o: { configured?: boolean; api?: FakeWikiApi } = {}) {
  const api = o.api ?? new FakeWikiApi({ delayMs: 0 });
  const conn = new FakeConn();
  let configured = o.configured ?? true;
  const m = new TeachModel({ api, conn, configured: () => configured });
  return { api, conn, m, setConfigured: (v: boolean) => { configured = v; } };
}
const spec = (m: TeachModel): TeachSpecOut => {
  const s = m.state.spec;
  if (s.kind !== "ready") throw new Error(`spec is ${s.kind}`);
  return s.spec;
};
const last = (api: FakeWikiApi) => api.calls[api.calls.length - 1];
/** An exemplar set elsewhere (the assistant's 照这一页的写法写), before the section reads the spec. */
const withExemplar = async (api: FakeWikiApi): Promise<void> => {
  await api.addExemplar({ kind: "vault_note", ref: "Notes/写法.md", title: "写法", text: "先举例，再讲公式。" });
  api.calls.length = 0;
};

describe("TeachModel: reading", () => {
  it("unconfigured: needsSettings, nothing sent", async () => {
    const { api, m } = rig({ configured: false });
    await m.open();
    expect(m.state.spec).toEqual({ kind: "error", message: SETTINGS_COPY.needsSettings });
    expect(methods(api)).toEqual([]);
  });

  it("open: one GET of the spec (loading → ready); a failure is a plain sentence, never the detail", async () => {
    const { api, m } = rig();
    const p = m.open();
    expect(m.state.spec.kind).toBe("loading");
    await p;
    expect(methods(api)).toEqual(["teachSpec"]);
    expect(spec(m).exemplars).toEqual([]);
    api.failNext("teachSpec", new ApiError(500, "Traceback secret"));
    await m.open();
    expect(m.state.spec).toEqual({ kind: "error", message: WIKI_COPY.failed });
  });

  it("dispose: a GET still out lands nowhere and nothing is notified", async () => {
    const { m } = rig();
    let n = 0;
    m.subscribe(() => n++);
    const p = m.open();
    const before = n;
    m.dispose();
    await p;
    expect(m.state.spec.kind).toBe("loading");
    expect(n).toBe(before);
  });
});

describe("TeachModel: 样板页 删除", () => {
  it("DELETE with base_version, the returned spec shown (no GET); 404 → exemplarGone and a re-read", async () => {
    const { api, m } = rig();
    await withExemplar(api);
    await m.open();
    const id = spec(m).exemplars[0]!.exemplar_id;
    const v = spec(m).version;
    await m.deleteExemplar(id);
    expect(last(api)).toEqual({ method: "deleteExemplar", args: [id, v] });
    expect(spec(m).exemplars).toEqual([]);
    expect(m.state.note).toBeNull();
    api.failNext("deleteExemplar", new ApiError(404, "没有这个范例：ex9"));
    await m.deleteExemplar("ex9");
    expect(m.state.note).toEqual({ text: TEACH_COPY.exemplarGone, tone: "error" });
    expect(methods(api).slice(-2)).toEqual(["deleteExemplar", "teachSpec"]);
  });

  it("409: re-read the spec and say so", async () => {
    const { api, m } = rig();
    await withExemplar(api);
    await m.open();
    api.failNext("deleteExemplar", new ApiError(409, "version"));
    await m.deleteExemplar(spec(m).exemplars[0]!.exemplar_id);
    expect(m.state.note).toEqual({ text: TEACH_COPY.conflict, tone: "error" });
    expect(methods(api).slice(-2)).toEqual(["deleteExemplar", "teachSpec"]);
  });

  it("refused (422) → its own sentence; never the detail", async () => {
    const { api, m } = rig();
    await withExemplar(api);
    await m.open();
    api.failNext("deleteExemplar", new ApiError(422, "server detail"));
    await m.deleteExemplar(spec(m).exemplars[0]!.exemplar_id);
    expect(m.state.note).toEqual({ text: "没能删除这篇样板页，请再试一次。", tone: "error" });
    expect(JSON.stringify(m.state)).not.toContain("server detail");
  });

  it("offline / unconfigured / no spec shown: nothing sent, a plain sentence", async () => {
    const { api, conn, m, setConfigured } = rig();
    await m.deleteExemplar("ex1");
    expect(m.state.note).toEqual({ text: TEACH_COPY.notReady, tone: "error" });
    await withExemplar(api);
    await m.open();
    api.calls.length = 0;
    conn.emit("offline");
    await m.deleteExemplar("ex1");
    expect(m.state.note).toEqual({ text: WIKI_COPY.offline, tone: "error" });
    conn.emit("online");
    setConfigured(false);
    await m.deleteExemplar("ex1");
    expect(m.state.note).toEqual({ text: SETTINGS_COPY.needsSettings, tone: "error" });
    expect(methods(api)).toEqual([]);
  });
});
