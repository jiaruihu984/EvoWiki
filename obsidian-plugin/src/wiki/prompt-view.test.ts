import type { WorkspaceLeaf } from "obsidian";
import { describe, expect, it } from "vitest";
import { setup } from "./controller-test-kit";
import { PROMPT_STEPS } from "./fixtures";
import { PromptView } from "./prompt-view";

async function open(key: string, o: Parameters<typeof setup>[0] = {}) {
  const s = setup(o);
  await s.c.loadEngine();
  const view = new PromptView({} as WorkspaceLeaf, { controller: () => s.c });
  view.setStateKey(key);
  await view.onOpen();
  return { ...s, view, root: view.contentEl };
}
const q = <T extends HTMLElement>(root: HTMLElement, sel: string): T => {
  const e = root.querySelector<T>(sel);
  if (!e) throw new Error(`no ${sel}`);
  return e;
};

describe("PromptView", () => {
  it("a step: div.ew-scroll > div.ew-set > h1 name + textarea#prompt-edit (aria-label) + row 保存 / 恢复默认", async () => {
    const { root, view } = await open("write");
    const set = q(root, ".ew > .ew-scroll > .ew-set");
    expect(set.querySelector(":scope > h1")?.textContent).toBe("页面内容生成");
    expect(view.getDisplayText()).toBe("页面内容生成");
    const ta = q<HTMLTextAreaElement>(set, "textarea.ew-ped#prompt-edit");
    expect(ta.value).toBe(PROMPT_STEPS.find((p) => p.step === "write")?.text);
    expect(ta.getAttribute("aria-label")).toBe("页面内容生成的提示词");
    expect([...set.querySelectorAll(":scope > .ew-row > button")].map((b) => [b.className, b.textContent])).toEqual([
      ["ew-btn ew-primary", "保存"], ["ew-btn", "恢复默认"]]);
  });

  it("a step with a format_note says it in a .ew-muted line under its name; one without has none", async () => {
    const plain = await open("write");
    expect(plain.root.querySelector("[data-role=format-note]")).toBeNull();
    const s = setup();
    await s.c.loadEngine();
    const p = s.c.state.prompts;
    if (p.kind !== "ready") throw new Error(p.kind);
    p.value = p.value.map((x) => (x.step === "write" ? { ...x, format_note: "这一步的输出格式改了，你以前的写法可能不适用" } : x));
    const view = new PromptView({} as WorkspaceLeaf, { controller: () => s.c });
    view.setStateKey("write");
    await view.onOpen();
    const set = q(view.contentEl, ".ew-set");
    expect([...set.children].map((x) => x.tagName + (x.classList.contains("ew-muted") ? ".muted" : ""))).toEqual(
      ["H1", "DIV.muted", "TEXTAREA", "DIV"]);
    expect(q(set, "[data-role=format-note]").textContent).toBe("这一步的输出格式改了，你以前的写法可能不适用");
  });

  it("保存 sends the typed text; a 422 keeps it in the box and says why; 恢复默认 puts the default back", async () => {
    const { root, notices, c } = await open("write");
    const ta = q<HTMLTextAreaElement>(root, "#prompt-edit");
    ta.value = "输出 JSON";
    q<HTMLButtonElement>(root, ".ew-row > .ew-primary").click();
    await new Promise((r) => setTimeout(r, 0));
    expect(notices[notices.length - 1]).toBe("这里只写怎么做；输出的格式是固定的，不能在这里改。");
    expect(ta.value).toBe("输出 JSON");
    ta.value = "我的写法";
    q<HTMLButtonElement>(root, ".ew-row > .ew-primary").click();
    await new Promise((r) => setTimeout(r, 0));
    expect(notices[notices.length - 1]).toBe("已保存。以后这一步按这个写。");
    expect(c.state.prompts.kind === "ready" && c.state.prompts.value.find((x) => x.step === "write")?.edited).toBe(true);
    q<HTMLButtonElement>(root, ".ew-row > button:not(.ew-primary)").click();
    await new Promise((r) => setTimeout(r, 0));
    expect(notices[notices.length - 1]).toBe("已恢复默认。");
    expect(ta.value).toBe(PROMPT_STEPS.find((p) => p.step === "write")?.default_text);
  });

});
