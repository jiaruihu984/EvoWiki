import { describe, expect, it, vi } from "vitest";
import { NoteActivation } from "./tab-rules";

describe("NoteActivation (app.js openTab: a note becoming active shows 助手)", () => {
  const A = { id: "leafA" }, B = { id: "leafB" };
  it("nothing during start-up; the note active at ready() counts as already shown", () => {
    const to = vi.fn();
    const n = new NoteActivation(to);
    n.fileOpened("a.md", "md");
    n.markdownLeafActive(A, "a.md");
    expect(to).not.toHaveBeenCalled();
    n.ready(A, "a.md");
    n.markdownLeafActive(A, "a.md");          // focus back to the same note: no change
    n.fileOpened("a.md", "md");
    expect(to).not.toHaveBeenCalled();
  });

  it("another .md opened, or another Markdown leaf's tab switched to → 助手; non-Markdown files do not", () => {
    const to = vi.fn();
    const n = new NoteActivation(to);
    n.ready(A, "a.md");
    n.fileOpened("b.md", "md");
    expect(to).toHaveBeenCalledTimes(1);
    n.fileOpened("pic.png", "png");
    expect(to).toHaveBeenCalledTimes(1);
    n.markdownLeafActive(B, "c.md");          // switching tabs fires leaf change, then file-open for the same file
    n.fileOpened("c.md", "md");
    expect(to).toHaveBeenCalledTimes(2);
    n.markdownLeafActive(A, "b.md");          // back to the first tab
    expect(to).toHaveBeenCalledTimes(3);
    n.markdownLeafActive(A, "b.md");          // focus returning from the sidebar
    n.markdownLeafActive(B, null);            // an empty leaf
    expect(to).toHaveBeenCalledTimes(3);
  });

  it("start with no note open: the first note opened shows 助手", () => {
    const to = vi.fn();
    const n = new NoteActivation(to);
    n.ready(null, null);
    n.fileOpened("a.md", "md");
    expect(to).toHaveBeenCalledOnce();
  });
});
