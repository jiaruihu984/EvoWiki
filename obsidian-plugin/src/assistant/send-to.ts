/**
 * The Bot's 问助手 / chip hand-off (main.ts assistantSend): with a page target, that page's conversation is shown first
 * (the same `show(target)` the quote path uses) so the question lands there, never in whichever note was active. A send
 * the assistant refuses (busy, still restoring...) leaves the text in the box; with no conversation at all the text is
 * handed to `lost`, so it is never dropped silently.
 */
import type { AssistantController } from "./controller";
import type { Target } from "./session-store";

export type SendPage = { pageId: string; title: string };
type Port = Pick<AssistantController, "show" | "sendText" | "setDraft" | "state">;

export async function sendToAssistant(a: Port, text: string, page: SendPage | undefined, lost: (text: string) => void): Promise<void> {
  if (page !== undefined) { const t: Target = { kind: "page", pageId: page.pageId, title: page.title }; a.show(t); }
  if (a.state.conv === null) { lost(text); return; }
  if (await a.sendText(text)) return;
  a.setDraft(text);
}
