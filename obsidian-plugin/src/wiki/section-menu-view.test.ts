import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CheckBlockHub } from "./check-block";
import { firstPage, setup } from "./controller-test-kit";
import { renderWikiNote } from "./note/format";
import { toWikiNoteInput } from "./note-input";
import { closeSectionMenu, openSectionMenu, sectionMenuPostProcessor } from "./section-menu-view";
import type { WikiController } from "./controller";
import { at, rect, settle, viewport } from "../ui/floating-test-kit";

async function opened() {
  const s = setup();
  await firstPage(s.c);
  if (s.c.state.page.kind !== "open") throw new Error("no page");
  const md = renderWikiNote(toWikiNoteInput(s.c.state.page.page, s.c.names)).markdown;
  return { ...s, md, line: md.split("\n").indexOf("## z = ±1 是什么意思") };
}

afterEach(() => { closeSectionMenu(); document.body.replaceChildren(); vi.restoreAllMocks(); });

describe("openSectionMenu (app.js sectionMenu)", () => {
  const open = async (pin = false) => {
    const o = await opened();
    if (pin) await o.c.togglePin("s4");
    const view = document.createElement("div");
    view.className = "view-content";
    const btn = document.createElement("button");
    btn.setAttribute("aria-expanded", "false");
    view.append(btn);
    document.body.append(view);
    viewport(1000, 800);
    at(btn, rect(480, 80, 20, 20));
    // the menu (made by openSectionMenu): 202 × 150 wherever floating-ui puts it
    vi.spyOn(HTMLDivElement.prototype, "offsetWidth", "get").mockReturnValue(202);
    vi.spyOn(HTMLDivElement.prototype, "offsetHeight", "get").mockReturnValue(150);
    const pageId = o.c.state.page.kind === "open" ? o.c.state.page.page.page_id : "";
    const ref = { pageId, heading: "z = ±1 是什么意思", ordinal: 0 };
    openSectionMenu(btn, o.c, ref);
    const menu = view.querySelector<HTMLElement>(".ew.ew-inline > div.ew-menu");
    return { ...o, btn, view, menu, ref };
  };
  const items = (m: HTMLElement) => [...m.children].map((x) => (x.tagName === "HR" ? "hr" : [x.textContent, (x as HTMLButtonElement).disabled, x.getAttribute("title")]));

  it("div.ew-menu role=menu in the view, entries in app.js's order with the two rules; positioned like app.js", async () => {
    const { btn, menu } = await open();
    expect(menu).not.toBeNull();
    expect(menu!.getAttribute("role")).toBe("menu");
    expect(items(menu!)).toEqual([["标记为已掌握", false, null], ["标记为不会", false, null], "hr",
      ["问助手", false, null], "hr", ["锁定这一节", false, null]]);
    expect([...menu!.querySelectorAll("button")].every((b) => b.type === "button" && b.getAttribute("role") === "menuitem")).toBe(true);
    // app.js: left = the button's right − 190, top = its bottom + 4 (fixed, by @floating-ui)
    await settle();
    expect(menu!.style.position).toBe("fixed");
    expect([menu!.style.left, menu!.style.top]).toEqual([`${500 - 190}px`, `${100 + 4}px`]);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(menu!.querySelector("button"));      // the focus is on the first item
  });

  it("keeps inside the window: at its right edge it shifts left, at the bottom it opens above", async () => {
    const { btn, c, ref } = await open();
    closeSectionMenu();
    at(btn, rect(990, 700, 20, 20));
    openSectionMenu(btn, c, ref);
    await settle();
    const m = document.querySelector<HTMLElement>(".ew-menu")!;
    expect(parseFloat(m.style.left) + 202).toBeLessThanOrEqual(1000 - 8);
    expect(m.style.top).toBe(`${700 - 4 - 150}px`);
  });

  it("arrow keys move between the enabled items; Esc in the menu closes it and puts the focus back on the button", async () => {
    const { btn, menu } = await open(true);
    await settle();
    const enabled = [...menu!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
    expect(document.activeElement).toBe(enabled[0]);              // 标记为已掌握 is disabled when pinned: 标记为不会
    enabled[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(enabled[1]);
    enabled[1]!.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
    expect(document.activeElement).toBe(enabled[enabled.length - 1]);
    enabled[enabled.length - 1]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector(".ew-menu")).toBeNull();
    expect(document.activeElement).toBe(btn);
  });

  it("pinned: 标记为已掌握 disabled with title 先解除锁定, the last entry is 解除锁定", async () => {
    const { menu } = await open(true);
    expect(items(menu!)).toEqual([["标记为已掌握", true, null], ["标记为不会", false, null], "hr",
      ["问助手", false, null], "hr", ["解除锁定", false, null]]);
  });

  it("a click on an entry closes the menu and runs the action; 问助手 hands over the quote", async () => {
    const { c, btn, view, menu, asked } = await open();
    const know = vi.spyOn(c, "know").mockResolvedValue(undefined);
    menu!.querySelector<HTMLButtonElement>("button")!.click();
    expect(know).toHaveBeenCalledWith("s4");
    expect(view.querySelector(".ew-menu")).toBeNull();
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    openSectionMenu(btn, c, { pageId: c.state.page.kind === "open" ? c.state.page.page.page_id : "", heading: "z = ±1 是什么意思", ordinal: 0 });
    [...view.querySelectorAll<HTMLButtonElement>(".ew-menu button")].find((b) => b.textContent === "问助手")!.click();
    expect(asked).toHaveLength(1);
    expect(asked[0]!.quote.heading).toBe("z = ±1 是什么意思");
  });

  it("closes on a click outside, on Esc, and on a second click of its button (one menu at a time)", async () => {
    const { c, btn, view, ref } = await open();
    document.body.click();
    expect(view.querySelector(".ew-menu")).toBeNull();
    openSectionMenu(btn, c, ref);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(view.querySelector(".ew-menu")).toBeNull();
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    openSectionMenu(btn, c, ref);
    openSectionMenu(btn, c, ref); // the button again: closes
    expect(view.querySelectorAll(".ew-menu").length).toBe(0);
    openSectionMenu(btn, c, ref);
    expect(view.querySelectorAll(".ew-menu").length).toBe(1);
  });
});

describe("sectionMenuPostProcessor (Reading view)", () => {
  const run = (hub: CheckBlockHub<WikiController>, text: string, lineStart: number, withH2 = true) => {
    const el = document.createElement("div");
    if (withH2) el.append(document.createElement("h2"));
    const addChild = vi.fn();
    sectionMenuPostProcessor(hub, vi.fn())(el, {
      getSectionInfo: () => ({ text, lineStart, lineEnd: lineStart }), addChild,
    } as unknown as Parameters<ReturnType<typeof sectionMenuPostProcessor>>[1]);
    return { el, addChild };
  };

  it("adds the parts to the h2 of a section heading line; nothing for other lines, no h2, or no section info", async () => {
    const { c, md, line } = await opened();
    const hub = new CheckBlockHub<WikiController>();
    hub.set(c);
    const ok = run(hub, md, line);
    expect(ok.el.querySelector("h2 > span.ew.ew-inline > button.ew-more")).not.toBeNull();
    expect(ok.addChild).toHaveBeenCalledOnce();
    expect(run(hub, md, line + 1).el.querySelector("button")).toBeNull();
    expect(run(hub, md, line, false).el.querySelector("button")).toBeNull();
    const el = document.createElement("div");
    el.append(document.createElement("h2"));
    sectionMenuPostProcessor(hub)(el, { getSectionInfo: () => null, addChild: vi.fn() } as unknown as
      Parameters<ReturnType<typeof sectionMenuPostProcessor>>[1]);
    expect(el.querySelector("button")).toBeNull();
  });

  it("only the one ⋯ button per heading, nothing else interactive", async () => {
    const { c, md, line } = await opened();
    const hub = new CheckBlockHub<WikiController>();
    hub.set(c);
    const { el } = run(hub, md, line);
    expect([...el.querySelectorAll("button")].map((b) => b.className)).toEqual(["ew-more"]);
    expect(el.querySelectorAll("a, input, [role=button]").length).toBe(0);
  });
});

describe("styles: .note h2 .more on Obsidian's DOM", () => {
  const css = readFileSync(join(process.cwd(), "styles.css"), "utf8");
  it("invisible until the section is hovered, focus-visible or its menu open; the demo's values", () => {
    expect(css).toContain(".evowiki-page h2 .ew-more,.evowiki-page .HyperMD-header-2 .ew-more{margin-left:auto;opacity:0;border:0;background:none;color:var(--tx-3);cursor:pointer;border-radius:5px;padding:0 6px;font-size:18px;line-height:1}");
    expect(css).toMatch(/\.evowiki-page h2 \.ew-more:focus-visible,\.evowiki-page h2 \.ew-more\[aria-expanded="true"\][^{]*\.el-h2:has\(~ div:hover\)[^{]*\{opacity:1\}/);
  });
});
