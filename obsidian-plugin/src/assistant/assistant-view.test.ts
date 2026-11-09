import { describe, expect, it, vi } from "vitest";
import type { WorkspaceLeaf } from "obsidian";
import { ASSISTANT_VIEW_TYPE, AssistantView } from "./assistant-view";
import { AssistantPanel } from "./assistant-panel";
import { AssistantController } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import type { AssistantHost } from "./host";
import { type ActiveNote, type AssistantSessionStore, sessionKey, type Target } from "./session-store";

const PCA: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const PAGE: Target = { kind: "page", pageId: "page_pca", title: "主成分分析" };
const ID = { server: "http://127.0.0.1:8000", user: "jerry" };
const flush = () => new Promise((r) => setTimeout(r, 0));

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

function rig(stored: Record<string, string> = {}) {
  const api = new FakeAssistantApi();
  const store: AssistantSessionStore = { get: (k) => stored[k] ?? null, set: () => Promise.resolve() };
  const ctl = new AssistantController({ api, store, identity: () => ID, newId: () => "t" });
  let next: () => Promise<ActiveNote> = () => Promise.resolve(PCA);
  const changeCbs: (() => void)[] = [];
  const offs = vi.fn();
  const renderMarkdown = vi.fn();
  const host: AssistantHost = {
    activeNote: vi.fn(() => next()),
    onActiveChange: vi.fn((cb: () => void) => { changeCbs.push(cb); return offs; }),
    renderMarkdown,
    conceptName: (id) => id,
  };
  const view = new AssistantView({} as WorkspaceLeaf, ctl, host);
  return { api, ctl, host, view, changeCbs, offs, renderMarkdown, setNext: (f: () => Promise<ActiveNote>) => { next = f; } };
}
/** The muted status line at the top of the thread (e.g. 打开一篇连到概念的笔记… when no note is shown), or null. */
const status = (v: AssistantView) => v.contentEl.querySelector(".ew-thread > .ew-muted")?.textContent ?? null;

describe("AssistantView", () => {
  it("has its type, title and icon", () => {
    const { view } = rig();
    expect(view.getViewType()).toBe(ASSISTANT_VIEW_TYPE);
    expect(ASSISTANT_VIEW_TYPE).toBe("evowiki-assistant");
    expect(view.getDisplayText()).toBe("EvoWiki 陪读助手");
    expect(view.getIcon()).toBe("messages-square");
  });

  it("onOpen shows the active note's conversation", async () => {
    const { view, ctl } = rig();
    await view.onOpen(); await flush();
    expect(ctl.state.conv?.target).toEqual(PCA);
    expect(status(view)).toBeNull();
    expect((view.contentEl.querySelector(".ew-composer") as HTMLElement).hidden).toBe(false);
  });

  it("mounts the demo's .ew-asst inside an .ew wrapper (the EXACT CSS is scoped to .ew)", async () => {
    const { view } = rig();
    await view.onOpen(); await flush();
    const wrap = view.contentEl.firstElementChild as HTMLElement;
    expect(wrap.classList.contains("ew")).toBe(true);
    expect(wrap.firstElementChild!.className).toBe("ew-asst");
  });

  it("an active-note change re-resolves and switches", async () => {
    const { view, ctl, changeCbs, setNext } = rig();
    await view.onOpen(); await flush();
    setNext(() => Promise.resolve(PAGE));
    expect(changeCbs).toHaveLength(1);
    changeCbs[0]!(); await flush();
    expect(ctl.state.conv?.target).toEqual(PAGE);
    expect(status(view)).toBeNull();
  });

  it("a slower earlier resolve that settles after a newer one is dropped", async () => {
    const { view, ctl, changeCbs, setNext } = rig();
    await view.onOpen(); await flush();
    const slow = deferred<ActiveNote>(); const fast = deferred<ActiveNote>();
    setNext(() => slow.promise); changeCbs[0]!();
    setNext(() => fast.promise); changeCbs[0]!();
    fast.resolve(PAGE); await flush();
    expect(ctl.state.conv?.target).toEqual(PAGE);
    slow.resolve({ kind: "unsupported", title: "Diary" }); await flush();
    expect(ctl.state.active).toEqual(PAGE);
    expect(status(view)).toBeNull();
  });

  it("onClose unsubscribes from the controller and the host", async () => {
    const { view, ctl, offs, setNext, changeCbs } = rig();
    await view.onOpen(); await flush();
    await view.onClose();
    expect(offs).toHaveBeenCalledOnce();
    ctl.show({ kind: "none" }); // a later controller notify does not touch the closed view's DOM
    expect(status(view)).toBeNull();
    ctl.show(PAGE);
    // A resolve still in flight at close is dropped as well.
    const late = deferred<ActiveNote>();
    setNext(() => late.promise); changeCbs[0]!(); late.resolve(PCA); await flush();
    expect(ctl.state.active).toEqual(PAGE);
  });

  it("opening the view makes no API call for a note without a stored session", async () => {
    const { view, api } = rig();
    await view.onOpen(); await flush();
    expect(api.calls).toEqual({ listSessions: [], createSession: [], sendMessage: [], decide: [], getSession: [] });
  });

  it("opening the view on a note with a stored session makes only the one session read", async () => {
    const { view, api } = rig({ [sessionKey(ID, PCA)]: "ses_old" });
    await view.onOpen(); await flush();
    expect(api.calls).toEqual({ listSessions: [], createSession: [], sendMessage: [], decide: [], getSession: ["ses_old"] });
  });

  it("renders finished answers through the host with the view as owner", async () => {
    const { view, ctl, renderMarkdown } = rig();
    await view.onOpen(); await flush();
    ctl.setDraft("为什么"); await ctl.send();
    expect(renderMarkdown).toHaveBeenCalled();
    expect(renderMarkdown.mock.calls[0]![2]).toBe(view);
  });
});

describe("AssistantPanel with a chips source", () => {
  it("shows no recommended-question chips even when the Bot has some (copy guide)", async () => {
    const { host, ctl } = rig();
    (host.activeNote as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve(PAGE));
    const sug = [{ suggestion_id: "c1", title: "特征值是什么？" }, { suggestion_id: "c2", title: "再举个例子" }] as never[];
    const list = vi.fn((id: string) => (id === "page_pca" ? sug : []));
    const onChip = vi.fn();
    const panel = new AssistantPanel(ctl, host, {} as never, { list, settings: () => ({ kinds: { chat: true }, page_cap: 2 }), onChip, subscribe: () => () => undefined });
    panel.open(); await flush();
    expect(panel.root.querySelector(".ew-composer .ew-sugg")).toBeNull();
    expect(onChip).not.toHaveBeenCalled();
    panel.close();
  });
});
