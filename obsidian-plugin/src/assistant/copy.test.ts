import { expect, it } from "vitest";
import { ERROR_CODES } from "./types";
import { actionWords, attFacts, charsText, decidedFailedText, errorText, proposalErrorText, turnStatus } from "./copy";

it("ledger card words from the kind and the title (split / path / merge / rule / new page; the rest keep the title)", () => {
  expect(actionWords("page_split", "把「特征值与特征向量」拆成 3 页", 3)).toMatchObject({
    ask: "建议拆分「特征值与特征向量」", yes: "确认拆分", no: "取消", doing: "正在拆分「特征值与特征向量」", did: "已拆分「特征值与特征向量」",
    sub: "" });
  expect(actionWords("path_edit", "调整「PCA」的学习顺序", 0)).toMatchObject({ ask: "建议调整「PCA」的学习顺序", yes: "确认调整", no: "取消", did: "已调整「PCA」的学习路径" });
  expect(actionWords("page_merge", "把 方差、协方差 合成一页「方差和协方差」", 2)).toMatchObject({
    ask: "建议合并「方差」和「协方差」", yes: "确认合并", no: "取消", doing: "正在合并「方差」和「协方差」", did: "已合并为「方差和协方差」" });
  expect(actionWords("rule", "记一条讲解规则：先给例子", 0)).toMatchObject({ yes: "确认修改", no: "取消", did: "已修改讲解规则" });
  expect(actionWords("page_new", "写一页「梯度」", 0)).toMatchObject({ ask: "建议新建页面「梯度」", yes: "确认新建", did: "已创建「梯度」页面" });
  expect(actionWords("goal_new", "开一个新目标：学 PCA", 0)).toEqual({ ask: "开一个新目标：学 PCA", sub: "", yes: "确认", no: "取消", doing: "开一个新目标：学 PCA", did: "开一个新目标：学 PCA" });
});

it("every error code has a Chinese text and detail is never shown", () => {
  const texts = ERROR_CODES.map((code) => errorText({ kind: "event", code, retryable: false }));
  for (const t of texts) expect(t).toMatch(/[一-鿿]/);
  expect(errorText({ kind: "offline" })).toBe("未连接到服务器");
  expect(errorText({ kind: "http", status: 502 })).toBe("没能完成，请再试一次。");
});

it("proposal errors have their own copy (no 重试, no 消息)", () => {
  expect(proposalErrorText({ kind: "offline" })).toBe("未连接到服务器");
  expect(proposalErrorText({ kind: "http", status: 400 })).toBe("没发出去。可以再点一次。");
  expect(proposalErrorText({ kind: "http", status: 502 })).toBe("没发出去。可以再点一次。");
  expect(proposalErrorText({ kind: "broken" })).toBe("没确认是否添加。可以再点一次。");
  for (const e of [{ kind: "offline" }, { kind: "http", status: 409 }, { kind: "broken" }, { kind: "aborted" }] as const) {
    expect(proposalErrorText(e)).not.toMatch(/重试|消息/);
  }
});

it("real-server status copy: 503 not enabled, unknown page, busy, proposal 409/404", () => {
  expect(errorText({ kind: "not_enabled" })).toBe("请切换模型重试");
  expect(errorText({ kind: "unknown_page" })).toBe("服务器上找不到这一页（可能还没同步，或已删除）。");
  expect(errorText({ kind: "busy" })).toBe("上一轮还没结束，等它结束再问。");
  // busy: the composer has a model picker, so 切换模型 is something the user can do; a timeout just says so
  expect(errorText({ kind: "event", code: "llm_busy", retryable: true, detail: "请切换模型重试" })).toBe("请切换模型重试");
  expect(errorText({ kind: "event", code: "llm_timeout", retryable: true })).toBe("请切换模型重试");
  expect(proposalErrorText({ kind: "http", status: 409 })).toBe("这一轮还在进行，等回答结束再点。");
  expect(proposalErrorText({ kind: "http", status: 404 })).toBe("这条提议在服务器上找不到了。");
});

it("a failed 加入页面 shows the server reason only when it is Chinese", () => {
  expect(decidedFailedText("这一段已钉住，不能改写")).toBe("没有添加：这一段已钉住，不能改写。");
  expect(decidedFailedText("没有这个页面：page_x。")).toBe("没有添加：没有这个页面：page_x。");
  const generic = "没有添加，页面没有改动。";
  expect(decidedFailedText("")).toBe(generic);
  expect(decidedFailedText("   ")).toBe(generic);
  expect(decidedFailedText("KeyError: 'sections'")).toBe(generic);
});

it("charsText: 万 to one decimal, 千 under 1 万, plain under 1 千", () => {
  expect(charsText(123_456)).toBe("约 12.3 万字");
  expect(charsText(10_000)).toBe("约 1 万字");
  expect(charsText(9_960)).toBe("约 1 万字");
  expect(charsText(9_400)).toBe("约 9 千字");
  expect(charsText(1_200)).toBe("约 1 千字");
  expect(charsText(300)).toBe("约 300 字");
  expect(charsText(0)).toBe("");
});

it("attFacts: pages and characters; nothing for an image", () => {
  expect(attFacts({ kind: "pdf", pages: 40, chars: 52_000 })).toBe("40 页 · 约 5.2 万字");
  expect(attFacts({ kind: "md", pages: null, chars: 3_000 })).toBe("约 3 千字");
  expect(attFacts({ kind: "pdf", pages: 40 })).toBe("40 页");
  expect(attFacts({ kind: "image", pages: 1, chars: 10 })).toBe("");
});

it("turnStatus: a partial sent card names its unreadable pages", () => {
  expect(turnStatus({ status: "partial", deleted: false, unreadable: [3, 4, 5, 9] })).toEqual(["部分页读不出 · 第 3–5、9 页", "warn"]);
  expect(turnStatus({ status: "partial", deleted: false })).toEqual(["部分页读不出", "warn"]);
});
