import { afterEach, expect, it } from "vitest";
import "../setup-copy";
import { i18n, setUiLanguage, tr } from "./index";
import zh from "./zh.json";
import en from "./en.json";
import extraZh from "./extra-zh.json";
import extraEn from "./extra-en.json";
import { btn, el } from "../ui/ew";
import { textInput } from "../ui/settings-rows";
import { SETTINGS_COPY } from "../copy";
import { WIKI_COPY } from "../wiki/copy";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
afterEach(() => { setUiLanguage("zh"); document.body.replaceChildren(); });
it("both bundled catalogs have every key, English copy and identical interpolation fields", () => {
  for (const [chinese, english] of [[zh, en], [extraZh, extraEn]] as const) {
    expect(Object.keys(english).sort()).toEqual(Object.keys(chinese).sort());
    for (const [key, value] of Object.entries(english)) {
      expect(value, key).not.toMatch(/\p{Script=Han}/u);
      const fields = (text: string): string[] => [...text.matchAll(/{{([^}]+)}}/g)].map((m) => m[1]!).sort();
      expect(fields(value), key).toEqual(fields((chinese as Record<string, string>)[key]!));
    }
  }
});
it("every static production lookup exists and supplies its declared interpolation fields", () => {
  const catalog: Record<string, string> = { ...zh, ...extraZh };
  const walk = (directory: string): string[] => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : entry.name.endsWith(".ts") && !entry.name.endsWith(".test.ts") ? [path] : [];
  });
  const files = walk(join(process.cwd(), "src"));
  expect(files.some((path) => path.endsWith("main.ts"))).toBe(true);
  for (const path of files) {
    const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
    const visit = (node: ts.Node): void => {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "tr" && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
        const key = node.arguments[0].text;
        expect(catalog[key], `${path}: ${key}`).toBeDefined();
        const fields = [...catalog[key]!.matchAll(/{{([^}]+)}}/g)].map((match) => match[1]!);
        const values = node.arguments[1];
        const supplied = values && ts.isObjectLiteralExpression(values) ? values.properties.flatMap((property) => property.name ? [property.name.getText(source)] : []) : [];
        expect(fields.filter((field) => !supplied.includes(field)), `${path}: ${key}`).toEqual([]);
      }
      node.forEachChild(visit);
    };
    visit(source);
  }
});
it("registered labels and accessibility names update without replacing controls or user text", () => {
  const send = btn(() => WIKI_COPY.retry, "", () => {});
  const icon = document.createElement("span"); icon.dataset.icon = "retained"; send.append(icon);
  const user = el("p", "", "今天");
  const input = textInput("unfinished 中文 draft", () => SETTINGS_COPY.userName);
  document.body.append(send, input, user); input.focus(); input.setSelectionRange(3, 8);
  setUiLanguage("en");
  expect(send.textContent).toBe(WIKI_COPY.retry); expect(send.textContent).not.toMatch(/\p{Script=Han}/u);
  expect(input.getAttribute("aria-label")).toBe(SETTINGS_COPY.userName);
  expect(input.value).toBe("unfinished 中文 draft"); expect(input.selectionStart).toBe(3); expect(input.selectionEnd).toBe(8);
  expect(user.textContent).toBe("今天"); expect(document.activeElement).toBe(input);
  expect(send.querySelector("[data-icon=retained]")).toBe(icon);
  setUiLanguage("zh"); expect(send.textContent).toBe("重试");
});
it("nested status tables and parameterized copy read the active catalog", () => {
  setUiLanguage("en"); expect(WIKI_COPY.rsiStatus.live).not.toMatch(/\p{Script=Han}/u);
  expect(tr("runtime.profileRenameConflict", { path: "中文 vault/My background.md" })).toContain("中文 vault/My background.md");
  setUiLanguage("zh"); expect(WIKI_COPY.rsiStatus.live).toBe("已生效");
});

it("bundled setup copy has English coverage without untranslated system text", () => {
 const english = i18n.getResourceBundle("en", "translation") as Record<string, string>;
 const chinese = i18n.getResourceBundle("zh", "translation") as Record<string, string>;
 const setup = Object.keys(chinese).filter((key) => key.startsWith("setup.")); expect(setup.length).toBeGreaterThan(60);
 for (const key of setup) { expect(english[key], key).toBeTruthy(); expect(english[key], key).not.toMatch(/\p{Script=Han}/u); }
});
