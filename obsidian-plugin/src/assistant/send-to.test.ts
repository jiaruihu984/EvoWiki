import { describe, expect, it, vi } from "vitest";
import { sendToAssistant } from "./send-to";

function port(over: { conv?: object | null; sent?: boolean } = {}) {
  const calls: string[] = [];
  const state = { conv: over.conv === undefined ? {} : over.conv };
  const a = {
    state: state as never,
    show: vi.fn((t: unknown) => { calls.push(`show:${JSON.stringify(t)}`); }),
    sendText: vi.fn((t: string) => { calls.push(`send:${t}`); return Promise.resolve(over.sent ?? true); }),
    setDraft: vi.fn((t: string) => { calls.push(`draft:${t}`); }),
  };
  return { a, calls };
}

describe("sendToAssistant", () => {
  it("shows the page's conversation, then sends", async () => {
    const { a, calls } = port();
    await sendToAssistant(a, "问题", { pageId: "p1", title: "T" }, vi.fn());
    expect(calls).toEqual(['show:{"kind":"page","pageId":"p1","title":"T"}', "send:问题"]);
  });
  it("without a page target it sends in the current conversation", async () => {
    const { a, calls } = port();
    await sendToAssistant(a, "问题", undefined, vi.fn());
    expect(calls).toEqual(["send:问题"]);
  });
  it("a refused send leaves the text in the draft", async () => {
    const { a, calls } = port({ sent: false });
    const lost = vi.fn();
    await sendToAssistant(a, "问题", { pageId: "p1", title: "T" }, lost);
    expect(calls[calls.length - 1]).toBe("draft:问题");
    expect(lost).not.toHaveBeenCalled();
  });
  it("no conversation at all: the text goes to the lost handler, nothing is sent", async () => {
    const { a } = port({ conv: null });
    const lost = vi.fn();
    await sendToAssistant(a, "问题", undefined, lost);
    expect(lost).toHaveBeenCalledWith("问题");
    expect(a.sendText).not.toHaveBeenCalled();
    expect(a.setDraft).not.toHaveBeenCalled();
  });
});
