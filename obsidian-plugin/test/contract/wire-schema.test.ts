/** The contract tests' validator (./wire-schema.ts) over the generated src/generated/wire-schema.json; no backend. */
import { describe, expect, it } from "vitest";
import { loadWireSchema } from "./wire-schema";

const wire = loadWireSchema();
const settings = { review_per_day: 20, page_limit_usd: null, language: "zh" };

describe("wire-schema", () => {
  it("accepts a valid output shape and names the renamed models by their TS names", () => {
    expect(wire.validate("SettingsIO", settings)).toEqual([]);
    for (const m of ["PageOut", "RsiChangeOut", "ChangeOut", "SourceSectionOut", "SourceUseOut"]) expect(wire.models.has(m), m).toBe(true);
  });

  it("refuses a missing key (defaults included), an unknown key, a bad Literal and a wrong type", () => {
    const missing: Record<string, unknown> = { ...settings };
    delete missing.language;
    expect(wire.validate("SettingsIO", missing).join()).toMatch(/language/);
    expect(wire.validate("SettingsIO", { ...settings, x: 1 }).join()).toMatch(/'x'/);
    expect(wire.validate("SettingsIO", { ...settings, language: "meh" }).join()).toMatch(/language/);
    expect(wire.validate("SettingsIO", { ...settings, review_per_day: 1.5 }).join()).toMatch(/review_per_day/);
    // 整理 is gone: its old keys are unknown keys now
    expect(wire.validate("SettingsIO", { ...settings, curate: "goal", days: 7 }).join()).toMatch(/curate/);
    const err = { code: "llm_busy", retryable: true, detail: "" };
    expect(wire.validate("ErrorEvent", err)).toEqual([]);
    expect(wire.validate("ErrorEvent", { ...err, code: "nope" }).join()).toMatch(/code/);
  });

  it("throws on a model it does not define (fail closed)", () => {
    expect(() => wire.validate("NoSuchOut", {})).toThrow(/NoSuchOut/);
  });
});
