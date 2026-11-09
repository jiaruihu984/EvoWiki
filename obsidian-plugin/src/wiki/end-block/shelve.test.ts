import { describe, expect, it } from "vitest";
import { WIKI_COPY } from "../copy";
import { methods } from "../controller-test-kit";
import { CONCEPTS } from "../fixtures";
import { createEndBlock } from "./end-block";
import { harness } from "./test-kit";

const shelveLink = (el: HTMLElement): HTMLButtonElement | null => el.querySelector<HTMLButtonElement>(".ew-act > button.ew-lk");
const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 20));

describe("收为参考卡 (the end block's action row)", () => {
  it("offered only while PageOut.can_shelve; a click is one POST collapse, then the page is a reference card", async () => {
    const h = await harness();
    const b = createEndBlock("p1", h.hub);
    h.c.subscribe(() => b.update());
    const open = () => (h.c.state.page.kind === "open" ? h.c.state.page.page : null);
    expect(open()?.can_shelve).toBe(false);
    expect(shelveLink(b.el)).toBeNull();
    // every cell of the page's goal becomes 确定会: the server now offers 收为参考卡
    for (const a of CONCEPTS.c1.aspects) await h.api.declare(`c1.${a}`, "know");
    await h.c.followPage("p1");
    expect(open()?.can_shelve).toBe(true);
    const link = shelveLink(b.el);
    expect(link?.textContent).toBe(WIKI_COPY.shelvePage);
    expect(link?.nextElementSibling?.classList.contains("ew-primary")).toBe(true);   // before 继续学习
    const n = h.api.calls.length;
    link!.click();
    await settle();
    expect(methods(h.api).slice(n, n + 2)).toEqual(["collapse", "openPage"]);
    expect(h.api.calls[n]?.args).toEqual(["p1"]);
    expect(open()?.collapsed).toBe(true);
    expect(h.c.state.page.kind === "open" && h.c.state.page.status?.text.startsWith(WIKI_COPY.shelveDone)).toBe(true);
    expect(shelveLink(b.el)).toBeNull();                 // a reference card offers 恢复成完整页 instead (the top line)
  });

  it("the controller sends nothing for a page the server does not offer it for", async () => {
    const h = await harness();
    const n = h.api.calls.length;
    await h.c.collapse();
    expect(h.api.calls.length).toBe(n);
  });
});
