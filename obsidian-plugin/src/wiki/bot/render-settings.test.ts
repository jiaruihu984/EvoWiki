import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_BOT_SETTINGS } from "../fixtures";
import { btn, fakeActions } from "../render-test-kit";
import { INITIAL_WIKI_STATE, type WikiState } from "../state";
import type { BotSettings, SettingsPatch } from "./bot-schema";
import { settingsSection } from "./render-settings";

afterEach(() => { document.body.replaceChildren(); });

const st = (o: Partial<BotSettings> = {}): WikiState => ({
  ...INITIAL_WIKI_STATE, bot: { ...INITIAL_WIKI_STATE.bot, settings: { kind: "ready", value: { ...DEFAULT_BOT_SETTINGS, ...o } } } });
function draw(s: WikiState) {
  const a = fakeActions();
  const x = document.createElement("section");
  settingsSection(x, s, a);
  document.body.append(x);
  return { a, x };
}
const saved = (a: ReturnType<typeof fakeActions>): SettingsPatch => a.botSaveSettings.mock.calls[a.botSaveSettings.mock.calls.length - 1]![0] as SettingsPatch;
const change = (e: HTMLInputElement | HTMLSelectElement, v: string): void => { e.value = v; e.dispatchEvent(new Event("change")); };

describe("参数 · Bot 设置 (app.js botSettings)", () => {
  it("ids and order: 开启 Bot master switch, Bot 权限 (kind switches, six levels), 提醒; no captions", () => {
    const { x } = draw(st());
    expect(x.id).toBe("bot-settings");
    const ids = [...x.querySelectorAll("[id]")].map((e) => e.id);
    expect(ids).toEqual(["bot-enabled", "bot-kind-review", "bot-kind-remedial", "bot-kind-goal", "bot-kind-prefetch",
      "bot-kind-weekly", "bot-kind-source", "bot-level-0", "bot-level-1", "bot-level-2", "bot-level-3", "bot-level-4", "bot-level-5",
      "bot-perday", "bot-wrong-days", "bot-wrong-n", "bot-idle", "bot-check-at"]);
    expect([...x.querySelectorAll(":scope > .ew-sub")].map((e) => e.textContent)).toEqual(["Bot 权限", "提醒规则"]);
    expect([...x.querySelectorAll<HTMLElement>(".ew-srow .ew-n")].map((e) => e.textContent)).toEqual(
      ["复习", "补讲", "继续学习", "空闲时提前生成页面", "每周回顾", "资料更新",
        "修改已生成的页面正文", "新建页面", "删除或合并页面", "目录和主题结构", "讲解规则和学习状态评估", "学习路径顺序", "Bot 每日检查时间"]);
    // the three sentences: the number inputs sit inline in one row each (app.js `line`)
    expect([...x.querySelectorAll(".ew-sline")].map((r) => r.textContent)).toEqual(["每日待办上限项", "天内，同类题答错达到道时安排补讲", "连续天未学习时，提醒继续学习"]);
    expect(x.querySelector(".ew-srow .ew-d")).toBeNull();
    expect(x.textContent).not.toContain("你自己写的笔记");
    expect(x.querySelector("#bot-review-weekday, #bot-level-6")).toBeNull();
    expect(x.querySelector<HTMLInputElement>("#bot-perday")?.getAttribute("inputmode")).toBe("numeric");
    expect(x.querySelector("#bot-perday")?.className).toBe("ew-num");
    expect(["bot-perday", "bot-wrong-days", "bot-wrong-n", "bot-idle", "bot-check-at"].map((id) => x.querySelector("#" + id)?.getAttribute("aria-label")))
      .toEqual(["每日待办上限（项）", "答错统计天数", "同类题答错数量", "连续未学习天数", "Bot 每日检查时间"]);
  });

  it("开启 Bot: the heading switch pauses indefinitely or resumes, and waits for the server before changing state", () => {
    const { a, x } = draw(st());
    const p = x.querySelector<HTMLButtonElement>("h2 #bot-enabled")!;
    expect(x.querySelector("h2")?.textContent).toBe("开启 Bot");
    expect(p.className).toBe("ew-switch");
    expect(p.getAttribute("role")).toBe("switch");
    expect(p.getAttribute("aria-checked")).toBe("true");
    p.click();
    expect(a.botPause).toHaveBeenCalledWith("forever");
    expect(p.getAttribute("aria-checked")).toBe("true");
    const off = draw(st({ paused: "always" }));
    const sw = off.x.querySelector<HTMLButtonElement>("#bot-enabled")!;
    expect(sw.getAttribute("aria-checked")).toBe("false");
    sw.click();
    expect(off.a.botPause).toHaveBeenCalledWith("off");
    expect(sw.getAttribute("aria-checked")).toBe("false");
    expect(x.querySelector("#bot-pause")).toBeNull();
    expect(x.textContent).not.toContain("暂停到明天");
  });

  it("temporary pauses show off only until paused_until expires; offline the master switch cannot send changes", () => {
    for (const [delta, expected] of [[60_000, "false"], [-60_000, "true"]] as const) {
      const { x } = draw(st({ paused: "until", paused_until: new Date(Date.now() + delta).toISOString() }));
      expect(x.querySelector("#bot-enabled")?.getAttribute("aria-checked")).toBe(expected);
    }
    const off = draw({ ...st(), conn: "offline" });
    const sw = off.x.querySelector<HTMLButtonElement>("#bot-enabled")!;
    expect(sw.disabled).toBe(true);
    sw.click();
    expect(off.a.botPause).not.toHaveBeenCalled();
  });

  it("toggling 补讲 saves wrong_answers and open_question together, as a patch of only those entries", () => {
    const { a, x } = draw(st({ failure_streak: 4 }));
    const sw = x.querySelector<HTMLButtonElement>("#bot-kind-remedial")!;
    expect(sw.getAttribute("aria-checked")).toBe("true");
    sw.click();
    expect(saved(a)).toEqual({ kinds: { wrong_answers: false, open_question: false } });
  });

  it("a 资料更新 row is shown (on by default); a kind the backend lacks has no row", () => {
    const kinds = { ...DEFAULT_BOT_SETTINGS.kinds };
    delete kinds.chat;
    const { x } = draw(st({ kinds }));
    const sw = (k: string) => x.querySelector<HTMLButtonElement>("#bot-kind-" + k);
    expect(sw("source")!.getAttribute("aria-checked")).toBe("true");
    expect(x.textContent).toContain("资料更新");
    expect(x.textContent).not.toContain("还没做好");
    expect(sw("chat")).toBeNull();
    expect(sw("prefetch")!.disabled).toBe(false);
    expect(sw("review")!.disabled).toBe(false);
  });

  it("numbers: valid saves into the right field; invalid reverts without a request", () => {
    const { a, x } = draw(st());
    const n = (id: string) => x.querySelector<HTMLInputElement>("#" + id)!;
    change(n("bot-perday"), "8");
    expect(saved(a).today_cap).toBe(8);
    change(n("bot-wrong-n"), "4");
    expect(saved(a).wrong_count).toBe(4);
    change(n("bot-wrong-days"), "10");
    expect(saved(a).wrong_days).toBe(10);
    change(n("bot-idle"), "5");
    expect(saved(a).idle_days).toBe(5);
    expect(a.botSaveSettings).toHaveBeenCalledTimes(4);
    a.botSaveSettings.mockClear();
    for (const [id, bad, was] of [["bot-perday", "0", "5"], ["bot-perday", "abc", "5"], ["bot-perday", "99", "5"],
      ["bot-wrong-n", "1", "3"], ["bot-wrong-days", "61", "7"], ["bot-idle", "", "3"]] as const) {
      const i = n(id);
      i.value = was;
      change(i, bad);
      expect(i.value, `${id} ${bad}`).toBe(was);
    }
    expect(a.botSaveSettings).not.toHaveBeenCalled();
    // the refusal says why, once, under the row; a good value takes it away
    const hints = () => [...x.querySelectorAll<HTMLElement>("[data-hint-for]")].map((h) => [h.dataset.hintFor, h.textContent]);
    change(n("bot-perday"), "60");
    change(n("bot-perday"), "61");
    expect(hints()).toEqual(expect.arrayContaining([["bot-perday", "只能填 1 到 50 的整数，没有保存。"]]));
    expect(hints().filter(([k]) => k === "bot-perday")).toHaveLength(1);
    change(n("bot-perday"), "6");
    expect(hints().filter(([k]) => k === "bot-perday")).toHaveLength(0);
  });

  it("每天检查 HH:MM saves daily_hour / daily_minute; invalid reverts", () => {
    const { a, x } = draw(st());
    const at = x.querySelector<HTMLInputElement>("#bot-check-at")!;
    expect(at.value).toBe("08:00");
    change(at, "07:30");
    expect(saved(a)).toMatchObject({ daily_hour: 7, daily_minute: 30 });
    a.botSaveSettings.mockClear();
    for (const bad of ["25:00", "8:60", "8", "abc", ""]) { change(at, bad); expect(at.value, bad).toBe("08:00"); }
    expect(a.botSaveSettings).not.toHaveBeenCalled();
    expect(x.querySelector<HTMLElement>("[data-hint-for='bot-check-at']")?.textContent).toBe("要写成 08:00 这样的时间，没有保存。");
  });

  it("levels: six selects (执行前确认 / 自动执行); the fifth row sets teaching_params and rsi together, the sixth path", () => {
    const { a, x } = draw(st());
    const sels = [0, 1, 2, 3, 4, 5].map((i) => x.querySelector<HTMLSelectElement>("#bot-level-" + i)!);
    expect(sels.map((s) => s.options[s.selectedIndex]?.textContent)).toEqual(
      ["自动执行", "自动执行", "执行前确认", "自动执行", "执行前确认", "自动执行"]);
    expect([...sels[0]!.options].map((o) => o.textContent)).toEqual(["执行前确认", "自动执行"]);
    change(sels[4]!, "auto_undo");
    expect(saved(a)).toEqual({ levels: { teaching_params: "auto_undo", rsi: "auto_undo" } });
    change(sels[2]!, "auto_undo");
    expect(saved(a)).toEqual({ levels: { delete_merge: "auto_undo" } });
    change(sels[5]!, "ask");
    expect(saved(a)).toEqual({ levels: { path: "ask" } });
  });

  it("学习路径顺序 is shown only when the backend's levels have path", () => {
    const levels: Partial<BotSettings["levels"]> = { ...DEFAULT_BOT_SETTINGS.levels };
    delete levels.path;
    const { x } = draw(st({ levels }));
    expect(x.querySelector("#bot-level-5")).toBeNull();
    expect(x.textContent).not.toContain("学习路径顺序");
    expect(x.querySelector("#bot-level-4")).not.toBeNull();
  });

  it("an unknown level is shown as a disabled selected option; the two-scope row shows ask when either scope is ask", () => {
    const { x } = draw(st({ levels: { ...DEFAULT_BOT_SETTINGS.levels, page_body: "never", teaching_params: "auto_undo", rsi: "ask" } }));
    const s0 = x.querySelector<HTMLSelectElement>("#bot-level-0")!;
    expect(s0.options[s0.selectedIndex]?.textContent).toBe("never");
    expect(s0.options[s0.selectedIndex]?.disabled).toBe(true);
    const s4 = x.querySelector<HTMLSelectElement>("#bot-level-4")!;
    expect(s4.value).toBe("ask");
  });

  it("loading and failed reads", () => {
    const s = { ...INITIAL_WIKI_STATE };
    expect(draw(s).x.textContent).toBe("开启 Bot正在加载…");
    const f = draw({ ...s, bot: { ...s.bot, settings: { kind: "error", message: "x" } } });
    expect(f.x.textContent).toBe("开启 Bot没能读到，重试");
    btn(f.x, ".ew-btn").click();
    expect(f.a.loadEngine).toHaveBeenCalled();
    // Offline: nothing in the section (the sidebar's one banner says it).
    expect(draw({ ...s, conn: "offline", bot: { ...s.bot, settings: { kind: "error", message: "x" } } }).x.textContent).toBe("开启 Bot");
  });
});
