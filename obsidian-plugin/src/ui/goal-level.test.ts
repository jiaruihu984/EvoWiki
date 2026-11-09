import { afterEach, expect, it } from "vitest";
import { goalLevelLabel } from "./goal-level";
import { setUiLanguage } from "../i18n";
import { el } from "./ew";
afterEach(() => setUiLanguage("zh"));
it("course-level badges react without translating custom labels or replacing their node", () => {
 const badge = el("span", "tg", () => goalLevelLabel("核心")); setUiLanguage("en"); expect(badge.textContent).toBe("Core");
 expect(goalLevelLabel("User 核心 project")).toBe("User 核心 project");
 setUiLanguage("zh"); expect(badge.textContent).toBe("核心");
});
