import { describe, expect, it } from "vitest";
import { ENGINE_PARAMS, ENGINE_PROMPTS } from "./fixtures";
import { renderEngine } from "./render-engine";
import { fakeActions } from "./render-test-kit";
import type { EngineOut } from "./schema";
import { INITIAL_PERSONAL, INITIAL_WIKI_STATE, type WikiState } from "./state";

const ENGINE: EngineOut = { prompts: [...ENGINE_PROMPTS], params: [...ENGINE_PARAMS] };
const loaded = (o: Partial<WikiState> = {}): WikiState => ({
  ...INITIAL_WIKI_STATE, conn: "online", tab: "engine",
  rsi: { kind: "ready", value: [] }, engine: { kind: "ready", value: ENGINE },
  ...o,
});
const draw = (s: WikiState, a = fakeActions()) => ({ a, b: renderEngine(s, a) });
const sections = (b: HTMLElement): HTMLElement[] => [...b.querySelectorAll<HTMLElement>(":scope > section")];

describe("个性化 (app.js engineView)", () => {
  it("the body: 我的学习偏好, 活动记录, 资料, then the Bot / Prompt / 评估 settings until they move to 设置", () => {
    const { b } = draw(loaded());
    expect(b.className).toBe("ew-side-body ew-pz");
    expect(b.dataset.tab).toBe("engine");
    expect(b.style.height).toBe("100%");
    expect([...b.children].map((x) => x.id || x.querySelector(":scope > #pz-log")?.id || x.querySelector(":scope > .ew-pz-entry > b")?.textContent)).toEqual(
      ["我的背景", "pz-rules", "pz-log", "资料"]);
    expect(sections(b).map((x) => x.querySelector(":scope > h2")?.textContent)).toEqual([]);
    expect(b.querySelector("#eng-log, #bot-runs, .ew-cgroup")).toBeNull();
  });

  it("管理偏好 replaces the whole body (no settings under it)", () => {
    const { b } = draw(loaded({ personal: { ...INITIAL_PERSONAL, manage: true } }));
    expect(sections(b)).toEqual([]);
    expect(b.querySelector("#pz-rules, #pz-log")).toBeNull();
    expect(b.querySelector("[data-action=prefs-back]")?.textContent).toBe("‹ 个性化");
  });

});
