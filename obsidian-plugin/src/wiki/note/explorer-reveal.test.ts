import { describe, expect, it, vi } from "vitest";
import type { App } from "obsidian";
import { RevealOnce, revealInExplorer } from "./explorer-reveal";

describe("the file explorer opens down to a page when its writing starts (explorer-reveal.ts)", () => {
  it("once per write: the first streamed sync reveals, the next sections do not, a new write does again", () => {
    const reveal = vi.fn();
    const r = new RevealOnce(reveal);
    r.started("pg1", "EvoWiki/目标/网络/度与度分布.md");
    r.started("pg1", "EvoWiki/目标/网络/度与度分布.md");            // section 2, 3 …
    r.started("pg2", "");                                           // no file yet: nothing
    expect(reveal.mock.calls).toEqual([["EvoWiki/目标/网络/度与度分布.md"]]);
    r.ended("pg1");
    r.started("pg1", "EvoWiki/目标/网络/度与度分布.md");
    expect(reveal).toHaveBeenCalledTimes(2);
  });

  function app(shown: boolean, items: Record<string, { collapsed?: boolean; setCollapsed?: (c: boolean) => void }> | undefined) {
    const scroll = vi.fn();
    const view = { containerEl: { isShown: () => shown }, fileItems: items, tree: { infinityScroll: { scrollIntoView: scroll } } };
    const setActiveLeaf = vi.fn(), revealLeaf = vi.fn();
    const a = { workspace: { getLeavesOfType: (t: string) => (t === "file-explorer" ? [{ view }] : []), setActiveLeaf, revealLeaf } } as unknown as App;
    return { a, scroll, setActiveLeaf, revealLeaf };
  }

  it("opens each collapsed folder down to the file and scrolls its row in; no focus, no tab switch", async () => {
    const folder = (collapsed: boolean) => { const f = { collapsed, setCollapsed: vi.fn((c: boolean) => { f.collapsed = c; }) }; return f; };
    const items = { EvoWiki: folder(true), "EvoWiki/目标": folder(true), "EvoWiki/目标/网络": folder(false), "EvoWiki/目标/网络/度.md": {} };
    const { a, scroll, setActiveLeaf, revealLeaf } = app(true, items);
    revealInExplorer(a, "EvoWiki/目标/网络/度.md");
    expect([items.EvoWiki.collapsed, items["EvoWiki/目标"].collapsed]).toEqual([false, false]);
    expect(items["EvoWiki/目标/网络"].setCollapsed).not.toHaveBeenCalled();
    await new Promise((r) => window.requestAnimationFrame(() => r(null)));
    expect(scroll).toHaveBeenCalledWith(items["EvoWiki/目标/网络/度.md"], 4);
    expect(setActiveLeaf).not.toHaveBeenCalled();
    expect(revealLeaf).not.toHaveBeenCalled();
  });

  it("an explorer not on screen (another tab, a collapsed sidebar) or of an unknown shape is left as it is", () => {
    const f = { collapsed: true, setCollapsed: vi.fn() };
    revealInExplorer(app(false, { EvoWiki: f }).a, "EvoWiki/x.md");
    expect(f.setCollapsed).not.toHaveBeenCalled();
    expect(() => revealInExplorer(app(true, undefined).a, "EvoWiki/x.md")).not.toThrow();
  });
});
