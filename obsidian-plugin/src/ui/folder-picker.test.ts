import { expect, it, vi } from "vitest";
import { FuzzySuggestModal, type App, type TFolder } from "obsidian";
import { pickNotesFolder } from "./folder-picker";

type Picker = FuzzySuggestModal<TFolder> & { onClose(): void };
it("offers only current-vault directories and keeps a selection when Obsidian closes first", async () => {
  const open = vi.spyOn(FuzzySuggestModal.prototype, "open");
  const folders = ["Study", "Study/New", ".obsidian", ".evowiki/cache", "D:/outside"].map((path) => ({ path }) as TFolder);
  const app = { vault: { getAllFolders: vi.fn(() => folders) } } as unknown as App;
  const result = pickNotesFolder(app);
  const modal = open.mock.instances[0] as unknown as Picker;
  expect(modal.getItems().map((folder) => folder.path)).toEqual(["Study", "Study/New"]);
  modal.onClose(); modal.onChooseItem(folders[1]!, new MouseEvent("click"));
  await expect(result).resolves.toBe("Study/New");
  open.mockRestore();
});
it("cancel resolves without selecting a directory", async () => {
  const open = vi.spyOn(FuzzySuggestModal.prototype, "open");
  const result = pickNotesFolder({ vault: { getAllFolders: () => [] } } as unknown as App);
  const modal = open.mock.instances[0] as unknown as Picker;
  modal.onClose(); await expect(result).resolves.toBeNull();
  open.mockRestore();
});
