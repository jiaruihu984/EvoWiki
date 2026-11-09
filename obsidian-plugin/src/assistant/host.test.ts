import { describe, expect, it } from "vitest";
import { droppedVaultPath } from "./host";

describe("droppedVaultPath (a file dragged from Obsidian's file list)", () => {
  it("reads the file of an obsidian:// link, decoded", () => {
    expect(droppedVaultPath("obsidian://open?vault=v&file=%E5%AD%A6%2F%E8%AE%B2%E4%B9%89.pdf"))
      .toEqual({ path: "学/讲义.pdf", link: true });
  });
  it("an obsidian:// link without a file is still a library drop (unreadable), not ignored", () => {
    expect(droppedVaultPath("obsidian://open?vault=v")).toEqual({ path: "", link: true });
  });
  it("a plain line is a vault path to try; web and file URLs and empty text are not", () => {
    expect(droppedVaultPath("\n  学/讲义.pdf\nx")).toEqual({ path: "学/讲义.pdf", link: false });
    expect(droppedVaultPath("https://example.com/a.pdf")).toBeNull();
    expect(droppedVaultPath("file:///C:/a.pdf")).toBeNull();
    expect(droppedVaultPath("  \n ")).toBeNull();
  });
});
