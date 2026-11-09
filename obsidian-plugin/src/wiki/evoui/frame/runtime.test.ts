import { expect, it, vi } from "vitest";
import { EXAMPLES } from "../evoui-fixtures";
import { frameTheme } from "../frame-theme";
import { startFrame } from "./runtime";
import { trustEngineForTests } from "./engine-safety";
it("duplicate initialization preserves the running component and does not render or reset it", () => {
 trustEngineForTests(true);
 const mount = document.createElement("div"); document.body.append(mount);
 const render = vi.spyOn(window.EvoUI!, "render"); const receive = startFrame(window, mount, () => undefined);
 const init = { t: "init" as const, locale: "en" as const, block: EXAMPLES[0], blockId: "qa", progress: null, theme: frameTheme(mount) };
 try {
  receive(init); expect(render).toHaveBeenCalledOnce(); const first = mount.firstChild;
  const input = mount.querySelector<HTMLInputElement>("input"); if (input) input.value = "1.75";
  receive(init); expect(render).toHaveBeenCalledOnce(); expect(mount.firstChild).toBe(first);
  if (input) expect(input.value).toBe("1.75");
 } finally { receive({ t: "stop" }); render.mockRestore(); mount.remove(); trustEngineForTests(null); }
});
