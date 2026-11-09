import { setUiLanguage } from "../../i18n";
import { describe, expect, it, vi } from "vitest";
import { renderWikiNote } from "../note/format";
import { watchReviseBar } from "../revise/overlay";
import { applyHeader, buildHeader, editorSlot, ensureHeader, headerInfo, readingSlot, watchHeader } from "./header";

const FM = { 目标: "能说出特征向量是什么", 更新于: "2026-10-10 15:12", evowiki_title: "pca（2/4）", evowiki_page_id: "p2", cssclasses: ["evowiki-page"] };

describe("headerInfo", () => {
  it("a wiki note's title (evowiki_title, else the base name), 目标 and 更新于; null for any other note", () => {
    expect(headerInfo(FM, "file")).toEqual({ pageId: "p2", title: "pca（2/4）", goal: "能说出特征向量是什么", updated: "2026-10-10 15:12" });
    expect(headerInfo({ ...FM, evowiki_title: undefined }, "pca 2")).toEqual({ pageId: "p2", title: "pca 2", goal: "能说出特征向量是什么", updated: "2026-10-10 15:12" });
    expect(headerInfo({ ...FM, cssclasses: "evowiki-page wide" }, "f")?.title).toBe("pca（2/4）");
    expect(headerInfo({ ...FM, cssclasses: ["wide"] }, "f")).toBeNull();
    expect(headerInfo({ ...FM, evowiki_page_id: undefined }, "f")).toBeNull();
    expect(headerInfo(undefined, "f")).toBeNull();
    expect(headerInfo("x", "f")).toBeNull();
  });

  it("reads what the note writer writes", () => {
    const md = renderWikiNote({ pageId: "p2", title: "pca（2/4）", goal: "能说出特征向量是什么", diffText: null,       collapsed: false, sections: [] }, new Date(2026, 9, 10, 15, 12)).markdown;
    expect(md).toContain("evowiki_title: \"pca（2/4）\"");
    expect(md).toContain("更新于: \"2026-10-10 15:12\"");
  });
});

describe("buildHeader / ensureHeader", () => {
  const info = { pageId: "p2", title: "pca（2/4）", goal: "能说出特征向量是什么", updated: "2026-10-10 15:12" };

  it("evowiki-status: writing -> 「生成中」 at the top of the page (the fallback when the file tree has no mark)", () => {
    const fm = { evowiki_page_id: "p1", cssclasses: ["evowiki-page"], "evowiki-status": "writing" };
    const i = headerInfo(fm, "x")!;
    expect(i.writing).toBe(true);
    expect(buildHeader(i).querySelector(".ew-ws")?.textContent).toBe("生成中");
    expect(buildHeader({ ...i, writing: false }).querySelector(".ew-ws")).toBeNull();
    expect(headerInfo({ ...fm, "evowiki-status": undefined }, "x")!.writing).toBeUndefined();
  });

  it("a write this plugin streams: 生成中 + 停止 (calls stop); after 停止: 已停止 + 重新写 (restarts that page)", () => {
    const hooks = { stop: vi.fn(), restart: vi.fn() };
    const w = buildHeader({ ...info, writing: true, run: "writing" }, hooks).querySelector(".ew-ws")!;
    expect(w.textContent).toBe("生成中停止");
    expect(w.querySelector("i")).not.toBeNull();
    (w.querySelector("[data-action=stop]") as HTMLButtonElement).click();
    expect(hooks.stop).toHaveBeenCalledOnce();
    const st = buildHeader({ ...info, run: "stopped" }, hooks).querySelector(".ew-ws")!;
    expect(st.textContent).toBe("已停止重新写");
    (st.querySelector("[data-action=restart]") as HTMLButtonElement).click();
    expect(hooks.restart).toHaveBeenCalledWith("p2");
    // a write it does not stream (the Bot's): plain 生成中, no button
    expect(buildHeader({ ...info, writing: true }, hooks).querySelector(".ew-ws button")).toBeNull();
  });

  it("the header is redrawn when the write's state changes", () => {
    const parent = document.createElement("div");
    const hooks = { stop: vi.fn(), restart: vi.fn() };
    ensureHeader(parent, { ...info, run: "writing" }, null, hooks);
    expect(ensureHeader(parent, { ...info, run: "stopped" }, null, hooks)).toBe(true);
    expect(parent.querySelector(".ew-ws")?.textContent).toBe("已停止重新写");
  });

  it("app.js renderNote's DOM inside a .ew.ew-inline wrapper: h1, then dl.ew-meta 目标 / 更新于", () => {
    const h = buildHeader(info);
    expect(h.className).toBe("ew ew-inline evowiki-note-header");
    expect([...h.children].map((x) => x.tagName.toLowerCase() + (x.className ? "." + x.className : ""))).toEqual(["h1", "dl.ew-meta"]);
    expect(h.querySelector("h1")?.textContent).toBe("pca（2/4）");
    expect([...h.querySelectorAll("dl > *")].map((x) => [x.tagName.toLowerCase(), x.textContent])).toEqual([
      ["dt", "目标"], ["dd", "能说出特征向量是什么"], ["dt", "更新于"], ["dd", "2026-10-10 15:12"]]);
    expect(buildHeader({ ...info, updated: "" }).querySelectorAll("dt").length).toBe(1);
  });

  it("idempotent: never two headers; a changed value replaces it; null removes it", () => {
    const parent = document.createElement("div");
    const first = document.createElement("div");
    parent.append(first);
    expect(ensureHeader(parent, info, first)).toBe(true);
    const h = parent.firstElementChild;
    expect(ensureHeader(parent, info, first)).toBe(false);
    expect(parent.firstElementChild).toBe(h);
    expect(parent.querySelectorAll(".evowiki-note-header").length).toBe(1);
    expect(ensureHeader(parent, { ...info, title: "新标题" }, first)).toBe(true);
    expect(parent.querySelectorAll(".evowiki-note-header").length).toBe(1);
    expect(parent.querySelector("h1")?.textContent).toBe("新标题");
    expect(ensureHeader(parent, null)).toBe(true);
    expect(parent.querySelector(".evowiki-note-header")).toBeNull();
  });
});

/** Obsidian's Markdown view: the reading sizer (pusher + .mod-header + blocks) and the editor sizer. */
function markdownView(): HTMLElement {
  const d = (cls: string, ...kids: HTMLElement[]): HTMLElement => { const e = document.createElement("div"); e.className = cls; e.append(...kids); return e; };
  return d("", d("markdown-reading-view", d("markdown-preview-view evowiki-page", d("markdown-preview-sizer",
    d("markdown-preview-pusher"), d("mod-header", d("inline-title"), d("metadata-container")), d("el-p")))),
  d("markdown-source-view evowiki-page", d("cm-editor", d("cm-scroller", d("cm-sizer", d("inline-title"), d("cm-contentContainer"))))));
}

describe("where the header goes", () => {
  const info = { pageId: "p", title: "T", goal: "g", updated: "u" };
  it("Reading view: at the end of .mod-header; Live Preview: before .cm-contentContainer; a sizer without .mod-header: after the pusher", () => {
    const v = markdownView();
    expect(readingSlot(v.querySelector(".markdown-preview-view")!)?.parent.className).toBe("mod-header");
    expect((editorSlot(v.querySelector(".markdown-source-view")!)?.before as HTMLElement).className).toBe("cm-contentContainer");
    applyHeader(v, info);
    expect(v.querySelector(".mod-header > .evowiki-note-header:last-child")).not.toBeNull();
    expect(v.querySelector(".cm-sizer > .evowiki-note-header + .cm-contentContainer")).not.toBeNull();
    applyHeader(v, info);
    expect(v.querySelectorAll(".evowiki-note-header").length).toBe(2);
    v.querySelector(".mod-header")!.remove();
    applyHeader(v, info);
    expect(v.querySelector(".markdown-preview-pusher + .evowiki-note-header")).not.toBeNull();
    applyHeader(v, info);
    expect(v.querySelectorAll(".markdown-preview-sizer > .evowiki-note-header").length).toBe(1);
    applyHeader(v, null);
    expect(v.querySelectorAll(".evowiki-note-header").length).toBe(0);
  });

  it("watchHeader puts it back when Obsidian rebuilds .mod-header; the stop function stops watching", async () => {
    const v = markdownView();
    let info: { pageId: string; title: string; goal: string; updated: string } | null = { pageId: "p", title: "T", goal: "g", updated: "u" };
    const stop = watchHeader(v, () => info);
    expect(v.querySelectorAll(".evowiki-note-header").length).toBe(2);
    const sizer = v.querySelector(".markdown-preview-sizer")!;
    const fresh = document.createElement("div");
    fresh.className = "mod-header";
    sizer.querySelector(".mod-header")!.replaceWith(fresh);
    await new Promise((r) => setTimeout(r, 0));
    expect(v.querySelector(".mod-header > .evowiki-note-header")).not.toBeNull();
    info = null;
    stop();
    sizer.querySelector(".mod-header")!.replaceWith(document.createElement("div"));
    await new Promise((r) => setTimeout(r, 0));
    expect(v.querySelectorAll(".evowiki-note-header").length).toBe(1); // the editor's; no longer re-applied
  });
});

describe("the header and the 用资料改这一页 bar together (live check 2026-10-21: Obsidian froze)", () => {
  const d = (cls: string, ...kids: HTMLElement[]): HTMLElement => { const e = document.createElement("div"); e.className = cls; e.append(...kids); return e; };
  const hub = { current: null, subscribe: () => () => undefined };
  const tick = async (n = 30): Promise<void> => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
  const names = (p: Element): string[] => [...p.children].map((c) => c.className);

  /** Both watchers on one view; a third observer counts what they do and stops them past a bound (the old code looped forever). */
  async function settle(v: HTMLElement): Promise<{ count: () => number; stop: () => void }> {
    document.body.append(v);
    let n = 0;
    const stops: (() => void)[] = [];
    const stop = (): void => { for (const s of stops.splice(0)) s(); };
    const counter = new MutationObserver((rs) => { n += rs.length; if (n > 400) stop(); });
    counter.observe(v, { childList: true, subtree: true });
    stops.push(watchHeader(v, () => ({ pageId: "p", title: "T", goal: "g", updated: "u" })));
    stops.push(watchReviseBar(v, () => "p1", hub, () => "x"));
    stops.push(() => counter.disconnect());
    await tick();
    return { count: () => n, stop };
  }

  it("a reading sizer without .mod-header (Live Preview's reading sizer): the DOM settles, header then bar", async () => {
    const v = d("", d("markdown-reading-view", d("markdown-preview-view", d("markdown-preview-sizer", d("markdown-preview-pusher"), d("el-p"), d("el-p2")))),
      d("markdown-source-view", d("cm-sizer", d("inline-title"), d("cm-contentContainer"))));
    const w = await settle(v);
    expect(w.count()).toBeLessThan(20);
    const sizer = v.querySelector(".markdown-preview-sizer")!;
    expect(names(sizer)).toEqual(["markdown-preview-pusher", "ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "el-p", "el-p2"]);
    expect(names(v.querySelector(".cm-sizer")!)).toEqual(["inline-title", "ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "cm-contentContainer"]);
    // quiet: nothing more happens on its own
    const before = w.count();
    await tick();
    expect(w.count()).toBe(before);
    // Obsidian re-renders the blocks: both come back in the same order, then it is quiet again
    sizer.replaceChildren(d("markdown-preview-pusher"), d("el-q"));
    await tick();
    expect(names(sizer)).toEqual(["markdown-preview-pusher", "ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "el-q"]);
    const after = w.count();
    expect(after - before).toBeLessThan(20);
    await tick();
    expect(w.count()).toBe(after);
    w.stop();
  });

  it("with .mod-header, and when the bar got there first: still settles, the header always above the bar", async () => {
    const v = d("", d("markdown-reading-view", d("markdown-preview-view", d("markdown-preview-sizer", d("markdown-preview-pusher"), d("mod-header"), d("el-p")))));
    const w = await settle(v);
    expect(w.count()).toBeLessThan(20);
    expect(names(v.querySelector(".markdown-preview-sizer")!)).toEqual(["markdown-preview-pusher", "mod-header", "ew ew-inline evowiki-revbar-host", "el-p"]);
    w.stop();
    // the bar first, then the header: the header goes above it and neither moves again
    const v2 = d("", d("markdown-reading-view", d("markdown-preview-view", d("markdown-preview-sizer", d("markdown-preview-pusher"), d("el-p")))));
    document.body.append(v2);
    let n = 0;
    const c = new MutationObserver((rs) => { n += rs.length; });
    c.observe(v2, { childList: true, subtree: true });
    const sb = watchReviseBar(v2, () => "p1", hub, () => "x");
    const sh = watchHeader(v2, () => ({ pageId: "p", title: "T", goal: "", updated: "" }));
    await tick();
    expect(names(v2.querySelector(".markdown-preview-sizer")!)).toEqual(["markdown-preview-pusher", "ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "el-p"]);
    expect(n).toBeLessThan(10);
    sb(); sh(); c.disconnect();
  });

  it("Obsidian re-rendering away only our bar / header (nothing else moved): both are put back", async () => {
    const v = d("", d("markdown-reading-view", d("markdown-preview-view", d("markdown-preview-sizer", d("markdown-preview-pusher"), d("el-p")))));
    const w = await settle(v);
    const sizer = v.querySelector(".markdown-preview-sizer")!;
    sizer.querySelector(".evowiki-revbar-host")!.remove();
    await tick();
    sizer.querySelector(".evowiki-note-header")!.remove();
    await tick();
    expect(names(sizer)).toEqual(["markdown-preview-pusher", "ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "el-p"]);
    expect(w.count()).toBeLessThan(40);
    w.stop();
  });
});

it("managed metadata labels switch in place while title, goal and date values remain original", () => {
  const node = buildHeader({ pageId: "qa", title: "User 中文 title", goal: "My 原始 goal", updated: "2026-11-09 19:30" });
  const title = node.querySelector("h1"); const values = [...node.querySelectorAll("dd")].map((dd) => dd.textContent);
  setUiLanguage("en"); expect([...node.querySelectorAll("dt")].map((dt) => dt.textContent)).toEqual(["Goal", "Updated"]);
  expect(node.querySelector("h1")).toBe(title); expect(title?.textContent).toBe("User 中文 title");
  expect([...node.querySelectorAll("dd")].map((dd) => dd.textContent)).toEqual(values); setUiLanguage("zh");
});
