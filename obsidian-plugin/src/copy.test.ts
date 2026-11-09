import { expect, it } from "vitest";
import { COPY, SETTINGS_COPY } from "./copy";

it("settings: 哪些内容会被发送 is one plain sentence plus details that keep the note-content promise", () => {
  expect(SETTINGS_COPY.privacySummary).toBe("你输入的文字和你点的按钮会发给上面填的服务器；另外，你改了 EvoWiki 写的页面笔记时，会自动发送改动的那几行和笔记路径。别的笔记内容不会自动发送。");
  expect(SETTINGS_COPY.privacyDetailsTitle).toBe("详情");
  const d = SETTINGS_COPY.privacyDetails.join(" | ");
  for (const item of ["学习目标", "提的问题", "答题选项", "复习自评", "选中的文字和它所在的标题", "用量设置", "用户名",
    "不会发送：笔记内容、文件名"]) expect(d).toContain(item);
  expect(d).not.toContain("旧版概念功能");
});

it("settings copy is Chinese with no research-stage wording", () => {
  const all = Object.values(SETTINGS_COPY).flat().join(" | ");
  for (const bad of [/S3/, /condition/i, /v1 identifies/i, /observation/i]) expect(all).not.toMatch(bad);
  expect(Object.keys(COPY)).not.toContain("privacyNote");
  expect(Object.keys(COPY)).not.toContain("viewSourceDesc");
});

it("the account words: no 'create it in the web app' message (unknown users are registered); a failed registration names the id", () => {
  expect(Object.keys(COPY).sort()).toEqual(["dataUnreadable", "offline", "registerFailed"]);
  expect(COPY.registerFailed("jerry", "boom")).toBe("没能在 EvoWiki 里创建 jerry：boom");
});

it("settings copy has no English command names (only product names and 'id'); no 旧版 toggle any more (其他 now holds the connection)", () => {
  const all = Object.values(SETTINGS_COPY).flat().join(" | ");
  expect(all.replace(/\.obsidian|\.evowiki|EvoWiki|Obsidian|Wiki|\bid\b/g, "")).not.toMatch(/[A-Za-z]{2,}/);
  for (const k of ["legacyName", "legacyDesc", "legacyNeedsReload", "viewSourceName"]) expect(Object.keys(SETTINGS_COPY)).not.toContain(k);
});
