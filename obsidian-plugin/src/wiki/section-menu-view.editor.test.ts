/**
 * The Live Preview 「⋯」 decorations in a real CodeMirror editor: which `##` lines get the button, that edits to the
 * block re-read the note, and that typing below the block (in 我的笔记) only maps the buttons without re-reading it.
 */
import { EditorState } from "@codemirror/state";
import { type DecorationSet, EditorView, type ViewPlugin } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { editorLivePreviewField } from "../../test/obsidian-mock";
import { CheckBlockHub } from "./check-block";
import type { WikiController } from "./controller";
import { firstPage, setup } from "./controller-test-kit";
import { renderWikiNote, WIKI_END } from "./note/format";
import { toWikiNoteInput } from "./note-input";
import { noteHeadings } from "./section-menu";
import { sectionMenuExtension } from "./section-menu-view";

vi.mock("./section-menu", async (importOriginal) => {
  const m = await importOriginal<typeof import("./section-menu")>();
  return { ...m, noteHeadings: vi.fn(m.noteHeadings) };
});

const views: EditorView[] = [];
afterEach(() => { for (const v of views.splice(0)) v.destroy(); vi.mocked(noteHeadings).mockClear(); });

async function editor() {
  const s = setup();
  await firstPage(s.c);
  if (s.c.state.page.kind !== "open") throw new Error("no page");
  const md = renderWikiNote(toWikiNoteInput(s.c.state.page.page, s.c.names)).markdown;
  const hub = new CheckBlockHub<WikiController>();
  hub.set(s.c);
  const ext = sectionMenuExtension(hub, vi.fn());
  const view = new EditorView({ state: EditorState.create({ doc: md, extensions: [editorLivePreviewField, ext] }) });
  views.push(view);
  /** The line text each button sits at the end of. */
  const buttons = (): string[] => {
    const set = (view.plugin(ext as ViewPlugin<{ decorations: DecorationSet }>)!).decorations;
    const out: string[] = [];
    set.between(0, view.state.doc.length, (from) => { out.push(view.state.doc.lineAt(from).text); });
    return out;
  };
  const type = (at: number, insert: string, to = at) => view.dispatch({ changes: { from: at, to, insert } });
  const endTo = () => { const d = view.state.doc; for (let n = 1; n <= d.lines; n++) if (d.line(n).text === WIKI_END) return d.line(n).to; return -1; };
  return { view, buttons, type, endTo, md };
}

describe("section menu in Live Preview", () => {
  it("a button at the end of each matching section heading", async () => {
    const { buttons } = await editor();
    expect(buttons()).toContain("## z = ±1 是什么意思");
    expect(buttons().every((t) => t.startsWith("## "))).toBe(true);
  });

  it("typing in 我的笔记 (below the END line) keeps the buttons and does not re-read the note", async () => {
    const { buttons, type, endTo, view } = await editor();
    const before = buttons();
    vi.mocked(noteHeadings).mockClear();
    for (const ch of "我今天读完了这一页") type(view.state.doc.length, ch);
    type(endTo() + 1, "\n新的一行\n");
    expect(noteHeadings).not.toHaveBeenCalled();
    expect(buttons()).toEqual(before);
  });

  it("typing a marker line below the block re-reads it (two END lines: no buttons)", async () => {
    const { buttons, type, view } = await editor();
    vi.mocked(noteHeadings).mockClear();
    type(view.state.doc.length, `\n${WIKI_END}\n`);
    expect(noteHeadings).toHaveBeenCalled();
    expect(buttons()).toEqual([]);
  });

  it("editing inside the block re-reads it: a changed heading loses its button, undoing the edit brings it back", async () => {
    const { buttons, type, view } = await editor();
    const before = buttons();
    const line = [...Array(view.state.doc.lines).keys()].map((i) => view.state.doc.line(i + 1)).find((l) => l.text === "## z = ±1 是什么意思")!;
    vi.mocked(noteHeadings).mockClear();
    type(line.to, "（改）");
    expect(noteHeadings).toHaveBeenCalled();
    expect(buttons()).toEqual(before.filter((t) => t !== "## z = ±1 是什么意思"));
    type(line.to, "", line.to + "（改）".length);   // undo the edit: the button is back
    expect(buttons()).toEqual(before);
  });

  it("an edit that joins the END line with the next one re-reads the note", async () => {
    const { buttons, type, endTo, view } = await editor();
    type(view.state.doc.length, "\n我的一行");
    vi.mocked(noteHeadings).mockClear();
    type(endTo(), "", endTo() + 1);               // delete the newline after the END line
    expect(noteHeadings).toHaveBeenCalled();
    expect(buttons().every((t) => t.startsWith("## "))).toBe(true);
  });
});
