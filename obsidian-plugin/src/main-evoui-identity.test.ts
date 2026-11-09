import { describe, expect, it, vi } from "vitest";
import EvoWikiPlugin from "./main";
import type { EvouiPort } from "./wiki/evoui/evoui-block";
import type { BlockProgressData } from "./wiki/evoui/block-event";

function rig() {
  const plugin = new EvoWikiPlugin({} as never, {} as never);
  const controller = () => ({ blockEvent: vi.fn(), saveBlockProgress: vi.fn(), blockProgress: vi.fn((): Promise<BlockProgressData | null> => Promise.resolve({ revision: 1 })) });
  let current = controller();
  const settings = { serverUrl: "https://server-one", userId: "user-one" };
  const methods = { readVaultText: vi.fn(() => Promise.resolve("cached-one")), canFetchAssets: vi.fn(() => true), isGenerated: vi.fn(() => Promise.resolve(true)), imageBytes: vi.fn(() => Promise.resolve(new ArrayBuffer(1))),
    assets: { pathFor: vi.fn(() => "Wiki/assets/ia_1.json"), retrieve: vi.fn(() => Promise.resolve("written")) } };
  Object.assign(plugin, { data: { settings }, wikiDemo: false, wikiController: () => current, ...methods });
  const port = (plugin as unknown as { evouiPort(): EvouiPort }).evouiPort();
  return { port, settings, methods, current: () => current, replace: () => { current = controller(); } };
}
const event = { type: "item" as const, node: "quiz", revision: 4, session: "original-render" };

describe("EvoUI port identity", () => {
  for (const field of ["serverUrl", "userId"] as const) {
    it(`refuses old frame callbacks after ${field} changes`, async () => {
      const r = rig();
      r.settings[field] = "different";
      r.replace();
      r.port.onEvent!("ia_1", event);
      r.port.onProgress!("ia_1", { revision: 4 });
      expect(r.current().blockEvent).not.toHaveBeenCalled();
      expect(r.current().saveBlockProgress).not.toHaveBeenCalled();
      expect(await r.port.progress!("ia_1")).toBeNull();
      expect(r.current().blockProgress).not.toHaveBeenCalled();
      expect(await r.port.read("old-account/asset.json")).toBeNull();
      expect(r.port.assetPath("ia_1")).toBeNull();
      expect(r.port.canFetch()).toBe(false);
      expect(await r.port.retrieve("ia_1")).toBe(false);
      expect(await r.port.isGenerated!("old block")).toBe(false);
      expect(await r.port.image!("ia_1")).toBeNull();
      expect(r.methods.readVaultText).not.toHaveBeenCalled();
      expect(r.methods.assets.retrieve).not.toHaveBeenCalled();
    });
  }
  it("lets the same identity reconstruct its controller", async () => {
    const r = rig();
    r.replace();
    r.port.onEvent!("ia_1", event);
    r.port.onProgress!("ia_1", { revision: 4 });
    expect(r.current().blockEvent).toHaveBeenCalledWith("ia_1", event);
    expect(r.current().saveBlockProgress).toHaveBeenCalledWith("ia_1", { revision: 4 });
    expect(await r.port.progress!("ia_1")).toEqual({ revision: 1 });
  });
  it("rejects a progress response requested before the account changed", async () => {
    const r = rig();
    let finish!: (value: BlockProgressData) => void;
    r.current().blockProgress.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const pending = r.port.progress!("ia_1");
    r.settings.userId = "different";
    finish({ revision: 7 });
    expect(await pending).toBeNull();
  });
});


it("drops cached asset bytes read before an identity switch", async () => {
  const r = rig();
  let finish!: (text: string) => void;
  r.methods.readVaultText.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const pending = r.port.read("old-account/asset.json");
  r.settings.serverUrl = "different";
  finish("old private content");
  expect(await pending).toBeNull();
});
