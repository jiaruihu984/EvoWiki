// The demo notices (src/ui/ew-notice.ts) as the tests read them: the text of every notice on screen, oldest first.
import { noticeHost } from "../src/ui/ew-notice";

/** Texts of the `.ew-notice`s now shown (each one's first span). */
export function ewNotices(): string[] {
  return [...noticeHost().children].map((n) => n.firstElementChild?.textContent ?? "");
}

/** Removes every notice (call where a test used to clear Obsidian's `notices`). */
export function clearEwNotices(): void {
  noticeHost().replaceChildren();
}
