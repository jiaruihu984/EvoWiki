import { describe, expect, it } from "vitest";
import { engineParamNote } from "./engine-copy";

describe("engineParamNote", () => {
  it.each([
    [{ default: true, tuned_at: null, why: null }, "默认"],
    [{ default: true, tuned_at: "2026-10-09", why: "x" }, "默认"],
    [{ default: false, tuned_at: "2026-10-09", why: "你隔 3 天复习大多还记得" }, "10月9日调过 · 因为你隔 3 天复习大多还记得"],
    [{ default: false, tuned_at: "2026-01-05", why: null }, "1月5日调过"],
    [{ default: false, tuned_at: "2026-12-31", why: "  " }, "12月31日调过"],
    [{ default: false, tuned_at: null, why: null }, "调过"],
    [{ default: false, tuned_at: null, why: "你的自述偏乐观" }, "调过 · 因为你的自述偏乐观"],
    [{ default: false, tuned_at: "10月9日", why: null }, "调过"],
    [{ default: false, tuned_at: "2026-10-09T08:00:00Z", why: null }, "调过"],
  ] as const)("%j → %s", (p, note) => { expect(engineParamNote(p)).toBe(note); });
});
