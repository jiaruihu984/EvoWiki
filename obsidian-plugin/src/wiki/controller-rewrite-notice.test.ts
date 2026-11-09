/**
 * A page an RSI change left out of date is rewritten in the background after the user's open (`PageOut.rewriting`).
 * When it lands (the page's writer stamp changes) the page is re-read into the view and the learner is told once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { firstPage, setup, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import { WIKI_COPY } from "./copy";
import type { PageOut } from "./schema";

/** Open reads answer `rewriting`; plain reads show the new stamp once `landed` is set. */
class RewriteApi extends FakeWikiApi {
  landed = false;
  startRewrite = false;
  override async openPage(pageId: string, opts: { rewrite?: boolean } = {}): Promise<PageOut> {
    const p = await super.openPage(pageId, opts);
    const w = p.writer;
    return {
      ...p,
      rewriting: !!opts.rewrite && this.startRewrite,
      writer: w && this.landed ? { ...w, at: "2099-01-01T00:00:00Z" } : w,
    };
  }
  override async getPage(pageId: string): Promise<PageOut> {
    const p = await super.getPage(pageId);
    const w = p.writer;
    return { ...p, writer: w && this.landed ? { ...w, at: "2099-01-01T00:00:00Z" } : w };
  }
}

describe("a lazy RSI rewrite lands", () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); });
  afterEach(() => { vi.useRealTimers(); });

  it("tells the learner once and shows the new page", async () => {
    const api = new RewriteApi({ delayMs: 0 });
    const s = setup({ api });
    await firstPage(s.c);
    api.startRewrite = true;
    await s.c.openWikiPage("p1");
    const before = view(s.c).page.writer?.at;
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.notices).toEqual([]);                     // not landed yet: silent
    api.landed = true;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(s.notices.filter((n) => n === WIKI_COPY.pageRewritten)).toHaveLength(1);
    expect(view(s.c).page.writer?.at).not.toBe(before);
    // the background poll only peeks: after the open itself no openPage is sent
    expect(api.calls.filter((x) => x.method === "openPage")).toHaveLength(2);       // firstPage + the user's open
    await vi.advanceTimersByTimeAsync(300_000);
    expect(s.notices.filter((n) => n === WIKI_COPY.pageRewritten)).toHaveLength(1);
  });

  it("an open that started no rewrite watches nothing", async () => {
    const api = new RewriteApi({ delayMs: 0 });
    const s = setup({ api });
    await firstPage(s.c);
    await s.c.openWikiPage("p1");
    const n = api.calls.length;
    api.landed = true;
    await vi.advanceTimersByTimeAsync(300_000);
    expect(api.calls.length).toBe(n);
    expect(s.notices).not.toContain(WIKI_COPY.pageRewritten);
  });

  it("gives up silently when it never lands", async () => {
    const api = new RewriteApi({ delayMs: 0 });
    const s = setup({ api });
    await firstPage(s.c);
    api.startRewrite = true;
    await s.c.openWikiPage("p1");
    await vi.advanceTimersByTimeAsync(400_000);
    expect(s.notices).not.toContain(WIKI_COPY.pageRewritten);
  });
});
