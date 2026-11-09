import { afterEach, describe, expect, it, vi } from "vitest";
import { ENGINE_COPY } from "./engine-copy";
import { mountEngineSettings } from "./engine-settings";
import { setup } from "./controller-test-kit";
import { fakeActions } from "./render-test-kit";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";

afterEach(() => { document.body.replaceChildren(); });

function draw(s: Partial<WikiState>) {
  const a = fakeActions();
  const box = document.createElement("div");
  document.body.append(box);
  mountEngineSettings(box, { state: () => ({ ...INITIAL_WIKI_STATE, conn: "online", configured: true, ...s }), subscribe: () => () => undefined, actions: a, load: () => undefined });
  return { a, box };
}

describe("设置 · 自动改进讲解和助手", () => {
  it("the heading has the ? and the switch, no line under it; the switch asks and keeps showing what the server said", () => {
    const { a, box } = draw({ rsiSwitch: { kind: "ready", value: false } });
    const sw = box.querySelector<HTMLButtonElement>("#rsi-enabled")!;
    const section = sw.closest("section")!;
    expect(section.querySelector("h2")?.contains(sw)).toBe(true);
    expect(section.querySelector("h2 .ew-qtip .ew-tipbox")?.textContent).toContain(ENGINE_COPY.rsiHelp[1]);
    expect([...section.children].map((e) => e.tagName)).toEqual(["H2"]);
    expect(sw.getAttribute("aria-checked")).toBe("false");
    sw.click();
    expect(a.setRsiSwitch).toHaveBeenCalledWith(true);
    expect(sw.getAttribute("aria-checked")).toBe("false");
  });

  it("the controller reads the switch with the settings and writes it", async () => {
    const { c, api } = setup();
    await c.loadEngine();
    expect(c.state.rsiSwitch).toEqual({ kind: "ready", value: false });
    await c.setRsiSwitch(true);
    expect(c.state.rsiSwitch).toEqual({ kind: "ready", value: true });
    expect((await api.rsiSwitch()).on).toBe(true);
    const fail = vi.spyOn(api, "putRsiSwitch").mockRejectedValueOnce(new Error("x"));
    await c.setRsiSwitch(false);
    expect(fail).toHaveBeenCalled();
    expect(c.state.rsiSwitch).toEqual({ kind: "ready", value: true });
  });
});
