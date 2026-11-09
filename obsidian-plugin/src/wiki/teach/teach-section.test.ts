import { afterEach, describe, expect, it } from "vitest";
import { clearEwNotices, ewNotices } from "../../../test/ew-notices";
import { ApiError } from "../../api/errors";
import { SETTINGS_COPY } from "../../copy";
import { FakeConn, methods } from "../controller-test-kit";
import { FakeWikiApi } from "../fake";
import type { Exemplar, TeachSpecOut } from "../schema";
import { exemplarLine, exemplarMeta, mountExemplarSection } from "./teach-section";

const flush = () => new Promise((r) => setTimeout(r, 0));
afterEach(() => { clearEwNotices(); });

/** 样板页 as the settings builder makes it: `section` > h2, the section appends to it. */
function mount(o: { api?: FakeWikiApi; configured?: boolean; demo?: boolean } = {}) {
  const api = o.api ?? new FakeWikiApi({ delayMs: 0 });
  const ex = document.createElement("section");
  ex.append(Object.assign(document.createElement("h2"), { textContent: "样板页" }));
  const handle = mountExemplarSection(ex, { api, conn: new FakeConn(), configured: () => o.configured ?? true, demo: o.demo });
  const btn = (a: string) => ex.querySelector<HTMLButtonElement>(`[data-action=${a}]`);
  return { api, ex, sec: handle, btn };
}
const exemplar = (id: string, over: Partial<Exemplar> = {}): Exemplar =>
  ({ exemplar_id: id, kind: "vault_note", ref: `Notes/${id}.md`, title: `样板 ${id}`, text: "正文", ...over });
/** A FakeWikiApi whose GET returns `spec` (rendering cases the fake does not reach). */
function withSpec(spec: TeachSpecOut): FakeWikiApi {
  const api = new FakeWikiApi({ delayMs: 0 });
  api.teachSpec = () => Promise.resolve(structuredClone(spec));
  return api;
}
const SPEC = (exemplars: Exemplar[]): TeachSpecOut => ({
  version: 3, stored: true, items: [], exemplars, evidence: {}, decisions: [], max_items: 30, max_exemplars: 3,
  params: { recap_max_per_page: 1, analogy_level: 1, fading_level: 1, interactive_bias: 0 } });

describe("mountExemplarSection: 样板页", () => {
  it("exemplarMeta / exemplarLine: Wiki 页面 / 库里的笔记：<path>", () => {
    expect(exemplarMeta(exemplar("a", { kind: "wiki_page", ref: "page_pca" }))).toBe("Wiki 页面");
    expect(exemplarMeta(exemplar("b"))).toBe("库里的笔记：Notes/b.md");
    expect(exemplarLine(exemplar("a", { kind: "wiki_page", ref: "page_pca", title: "pca（2/4）" }))).toBe("Wiki 页面：pca（2/4）");
    expect(exemplarLine(exemplar("b"))).toBe("库里的笔记：Notes/b.md");
  });

  it("the demo wiki is open: the lines only, nothing read or written", async () => {
    const m = mount({ demo: true });
    await flush();
    expect(m.ex.textContent).toContain(SETTINGS_COPY.demoNoEdit);
    expect(m.ex.querySelectorAll("button")).toHaveLength(0);
    expect(m.api.calls).toEqual([]);
  });

  it("one .ew-rule per exemplar (its line + 删除 link), never the snapshot; nothing to add here", async () => {
    const api = withSpec(SPEC([exemplar("e1", { kind: "wiki_page", ref: "page_pca", title: "主成分分析", text: "SNAPSHOT" }), exemplar("e2")]));
    const m = mount({ api });
    await flush();
    const rows = [...m.ex.querySelectorAll<HTMLElement>(".ew-rule")];
    expect(rows.map((r) => [r.firstElementChild?.textContent, r.lastElementChild?.textContent, r.lastElementChild?.className])).toEqual([
      ["Wiki 页面：主成分分析", "删除", "ew-btn ew-link"], ["库里的笔记：Notes/e2.md", "删除", "ew-btn ew-link"]]);
    expect(m.ex.textContent).not.toContain("SNAPSHOT");
    expect(m.btn("add-exemplar")).toBeNull();
    expect(m.ex.textContent).not.toContain("用当前打开的笔记");
  });

  it("删除: DELETE with the version shown, the list follows", async () => {
    const m = mount();
    await m.api.addExemplar({ kind: "vault_note", ref: "Notes/写法.md", title: "写法", text: "先举例。" });
    m.api.calls.length = 0;
    m.sec.dispose();
    const n = mount({ api: m.api });
    await flush();
    expect(n.ex.querySelector(".ew-rule")?.firstElementChild?.textContent).toBe("库里的笔记：Notes/写法.md");
    n.btn("delete-exemplar")!.click();
    await flush();
    expect(n.api.calls[n.api.calls.length - 1]).toEqual({ method: "deleteExemplar", args: ["ex1", 2] });
    expect(n.ex.querySelector(".ew-rule")).toBeNull();
    expect(n.ex.textContent).toContain("还没有样板页。");
  });

  it("unconfigured: needsSettings, nothing sent, no retry button, no notice", async () => {
    const m = mount({ configured: false });
    await flush();
    expect(m.ex.textContent).toContain(SETTINGS_COPY.needsSettings);
    expect(methods(m.api)).toEqual([]);
    expect(m.btn("retry-teach")).toBeNull();
    expect(ewNotices()).toEqual([]);
  });

  it("a failed read: a plain .ew-muted sentence + notice, and 再试一次 (reads again)", async () => {
    const api = new FakeWikiApi({ delayMs: 0 });
    api.failNext("teachSpec", new ApiError(500, "secret detail"));
    const m = mount({ api });
    await flush();
    expect(m.ex.querySelector(".ew-row > .ew-muted")?.textContent).toBe("没能完成，请再试一次。");
    expect(ewNotices()).toEqual(["没能完成，请再试一次。"]);
    expect(m.ex.textContent).not.toContain("secret detail");
    m.btn("retry-teach")!.click();
    await flush();
    expect(methods(api)).toEqual(["teachSpec", "teachSpec"]);
  });

  it("dispose stops redraws", async () => {
    const m = mount();
    m.sec.dispose();
    await flush();
    expect(m.ex.textContent).toContain("正在读取…");
  });
});
