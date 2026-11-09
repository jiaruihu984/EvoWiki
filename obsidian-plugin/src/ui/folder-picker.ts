import { FuzzySuggestModal, type App, type TFolder } from "obsidian";
import { validateNotesFolder } from "../notes/note-format";
import { setupText } from "../setup-copy";

/** Select only a directory inside the currently open vault. */
export function pickNotesFolder(app: App): Promise<string | null> {
  return new Promise((resolve) => {
    class FolderPicker extends FuzzySuggestModal<TFolder> {
      override getItems(): TFolder[] {
        return app.vault.getAllFolders(false).filter((folder) => validateNotesFolder(folder.path).ok);
      }
      override getItemText(folder: TFolder): string { return folder.path; }
      override onChooseItem(folder: TFolder): void { resolve(folder.path); }
      override onClose(): void { queueMicrotask(() => resolve(null)); }
    }
    const picker = new FolderPicker(app);
    picker.setPlaceholder(setupText("chooseFolder"));
    picker.open();
  });
}
