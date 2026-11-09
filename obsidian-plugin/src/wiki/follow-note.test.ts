import { describe, expect, it } from "vitest";
import { firstPage, setup } from "./controller-test-kit";
import { inFolder, pageToFollow } from "./follow-note";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";

const F = "EvoWiki";
const note = (pageId: string | null, path = "EvoWiki/A.md") => ({ path, pageId });
const st = (o: Partial<WikiState> = {}): WikiState => ({ ...INITIAL_WIKI_STATE, ...o });

describe("inFolder", () => {
  it("at or below the folder, case-insensitive; not a sibling with the same prefix", () => {
    expect(inFolder("EvoWiki/A.md", F)).toBe(true);
    expect(inFolder("evowiki/sub/A.md", F)).toBe(true);
    expect(inFolder("EvoWikiX/A.md", F)).toBe(false);
    expect(inFolder("Other/A.md", F)).toBe(false);
    expect(inFolder("A.md", "")).toBe(false);
  });
});

describe("pageToFollow", () => {
  it("a wiki note of another page → that page", () => {
    expect(pageToFollow(note("p2"), F, st())).toBe("p2");
    expect(pageToFollow(note("p2"), F, st({ page: { kind: "loading", pageId: "p1" } }))).toBe("p2");
  });

  it("nothing for no note, no id, outside the folder, the demo, or a write in flight", () => {
    expect(pageToFollow(null, F, st())).toBeNull();
    expect(pageToFollow(note(null), F, st())).toBeNull();
    expect(pageToFollow(note(""), F, st())).toBeNull();
    expect(pageToFollow(note("p2", "Notes/A.md"), F, st())).toBeNull();
    expect(pageToFollow(note("p2"), F, st({ demo: true }))).toBeNull();
    expect(pageToFollow(note("p2"), F, st({ busy: { what: "organize", abortable: true } }))).toBeNull();
  });

  it("nothing when the controller already shows that page", async () => {
    const { c } = setup();
    await firstPage(c);
    const id = c.state.page.kind === "open" ? c.state.page.page.page_id : "";
    expect(pageToFollow(note(id), F, c.state)).toBeNull();
    expect(pageToFollow(note(id + "x"), F, c.state)).toBe(id + "x");
  });

  it("nothing when that page is already loading or failed (no repeated GET on refocus)", () => {
    expect(pageToFollow(note("p2"), F, st({ page: { kind: "loading", pageId: "p2" } }))).toBeNull();
    expect(pageToFollow(note("p2"), F, st({ page: { kind: "error", pageId: "p2", message: "x" } }))).toBeNull();
  });
});
