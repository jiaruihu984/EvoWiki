import { describe, expect, it } from "vitest";
import { firstPage, setup, until, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import type { PageOut } from "./schema";

/** openPage of any id answers p1's page under that id; ids in `held` wait for release(). */
class GatedApi extends FakeWikiApi {
  readonly held = new Set<string>();
  private waiting: (() => void)[] = [];
  release(): void { const w = this.waiting; this.waiting = []; for (const f of w) f(); }
  override async openPage(pageId: string): Promise<PageOut> {
    if (this.held.has(pageId)) await new Promise<void>((r) => this.waiting.push(r));
    const p = await super.openPage("p1");
    return { ...p, page_id: pageId };
  }
}

describe("followPage (the wiki follows the active note)", () => {
  it("loads the page like openWikiPage but keeps the tab and never opens a note", async () => {
    const { c, opened, sync } = setup();
    await firstPage(c);
    const before = opened.length;
    const finds = sync.finds.length;
    c.setTab("assistant");
    await c.followPage("p1");
    expect(view(c).page.page_id).toBe("p1");
    expect(c.state.tab).toBe("assistant");
    expect(opened.length).toBe(before);
    expect(sync.finds.length).toBe(finds); // no findAndOpen
  });

  it("the latest follow wins: an older GET that returns late does not replace the newer page", async () => {
    const api = new GatedApi();
    const { c } = setup({ api });
    await firstPage(c);
    api.held.add("pa");
    const older = c.followPage("pa");
    expect(c.state.page).toEqual({ kind: "loading", pageId: "pa" });
    await c.followPage("pb");
    expect(view(c).page.page_id).toBe("pb");
    api.release();
    await older;
    await until(() => true);
    expect(view(c).page.page_id).toBe("pb");
  });

  it("does nothing while a write is in flight", async () => {
    const { c } = setup();
    await firstPage(c);
    const id = view(c).page.page_id;
    const state = { ...c.state };
    (c as unknown as { _state: typeof state })._state = { ...state, busy: { what: "know", abortable: false } };
    await c.followPage("other");
    expect(c.state.page.kind === "open" && c.state.page.page.page_id).toBe(id);
  });
});
