/** The two-tab sidebar's controller side: tab landing, the section jump, and the reads 今天 needs. */
import { describe, expect, it } from "vitest";
import { OfflineError } from "../api/errors";
import { firstPage, flush, methods, setup, view } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { renderWikiNote } from "./note/format";
import { toWikiNoteInput } from "./note-input";
import { LEGACY_TABS, TABS, landTab } from "./state";

describe("tabs", () => {
  it("landTab: 助手 and 参数 as themselves; every former tab value and anything unknown lands on 今天", () => {
    expect(landTab("today")).toBe("today");
    expect(landTab("assistant")).toBe("assistant");
    expect(landTab("engine")).toBe("engine");
    for (const t of LEGACY_TABS) expect(landTab(t), t).toBe("today");
    expect(landTab("toc")).toBe("today");
    expect(landTab("")).toBe("today");
    expect(TABS).toEqual(["today", "assistant", "engine"]);
  });

  it("setTab only changes the tab and reads nothing; old values land on 今天", async () => {
    const { api, c } = setup();
    for (const t of LEGACY_TABS) {
      c.setTab(t);
      expect(c.state.tab, t).toBe("today");
    }
    c.setTab("assistant");
    expect(c.state.tab).toBe("assistant");
    await flush();
    expect(methods(api)).toEqual([]);                 // leaving reads nothing
    c.setTab("today");
    expect(c.state.tab).toBe("today");
    await flush();
    // Coming back re-reads only what 今天 shows (a long-lived controller is never stale): the review list, the catalog and the changes
    // 需要你看的 / 最近改了 draw from (the Bot reads: tasks, actions, and the settings when not loaded yet).
    // (+ files: the vault has no note yet, so the first reconcile writes every page's note once)
    expect([...methods(api)].sort()).toEqual(["botActions", "botReviewReminder", "botSettings", "botSuggestions", "catalog", "files", "reviewToday", "rsiChanges"]);
  });

  it("opening a page reads K once (for the concept bars); following a note does too", async () => {
    const { api, c } = setup();
    await firstPage(c);           // the new-page chain loads K
    expect(methods(api).filter((m) => m === "kGrid")).toHaveLength(1);
    const s2 = setup();
    const d = s2.api.__goal("g");
    await s2.api.answerDraft(d.draft_id ?? "", { answers: null, skip: true });
    const n = s2.api.calls.length;
    await s2.c.followPage("p1");
    expect(methods(s2.api).slice(n)).toEqual(["openPage", "kGrid"]);
    await s2.c.openWikiPage("p1");
    expect(methods(s2.api).slice(n)).toEqual(["openPage", "kGrid", "openPage"]);
  });

  it("init reads the catalog, K, today's review list and the changes in parallel, each failing on its own", async () => {
    const ok = setup();
    await ok.c.init();
    expect([...methods(ok.api)].sort()).toEqual(["catalog", "kGrid", "memoryConflicts", "reviewToday", "rsiChanges", "teachSpec"]);
    expect([ok.c.state.catalog.kind, ok.c.state.k.kind, ok.c.state.review.today.kind, ok.c.state.rsi.kind])
      .toEqual(["ready", "ready", "ready", "ready"]);
    const bad = setup();
    bad.api.failNext("kGrid", new OfflineError());
    await bad.c.init();
    expect([bad.c.state.catalog.kind, bad.c.state.k.kind, bad.c.state.review.today.kind, bad.c.state.rsi.kind])
      .toEqual(["ready", "error", "ready", "ready"]);
  });

  it("init when unconfigured sends nothing and every read says to fill the settings", async () => {
    const { api, c } = setup({ configured: false });
    await c.init();
    expect(methods(api)).toEqual([]);
    for (const r of [c.state.catalog, c.state.k, c.state.review.today, c.state.rsi]) {
      expect(r).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    }
  });

  it("retryConnection re-reads only when something errored, and skips what already loaded", async () => {
    const { api, c } = setup();
    api.failNext("kGrid", new OfflineError());
    await c.init();
    api.calls.length = 0;
    await c.retryConnection();
    expect([...methods(api)].sort()).toEqual(["catalog", "kGrid"]);
    expect(c.state.k.kind).toBe("ready");
    api.calls.length = 0;
    await c.retryConnection();
    expect(methods(api)).toEqual([]);
  });
});

describe("jumpToSection", () => {
  it("opens the page's note with a locator that finds the section's line", async () => {
    const { c, sync, opened, locators } = setup();
    await firstPage(c);
    const page = view(c).page;
    const n = opened.length;
    c.jumpToSection("s4");
    await flush();
    expect(sync.finds[sync.finds.length - 1]).toBe("p1");
    expect(opened.slice(n)).toEqual(["Wiki/p1.md"]);
    const at = locators[locators.length - 1];
    if (at === undefined) throw new Error("no locator");
    const text = renderWikiNote(toWikiNoteInput(page, c.names)).markdown;
    const line = at(text);
    expect(line).not.toBeNull();
    expect(text.split("\n")[line!]).toBe(`## ${page.sections.find((x) => x.id === "s4")!.heading}`);
  });

  it("nothing for an unknown section, no open page, or the demo (no notes)", async () => {
    const { c, opened } = setup();
    c.jumpToSection("s1");
    await firstPage(c);
    const n = opened.length;
    c.jumpToSection("nope");
    await flush();
    expect(opened.length).toBe(n);
    const demo = setup({ demo: true });
    await firstPage(demo.c);
    demo.c.jumpToSection("s1");
    await flush();
    expect(demo.opened).toEqual([]);
  });

  it("works while a write is in flight (a local read)", async () => {
    const { c, opened } = setup({ delayMs: 5 });
    await firstPage(c);
    const n = opened.length;
    const done = c.done();
    expect(c.state.busy).not.toBeNull();
    c.jumpToSection("s2");
    await done;
    await flush();
    expect(opened.length).toBe(n + 1);
  });
});
