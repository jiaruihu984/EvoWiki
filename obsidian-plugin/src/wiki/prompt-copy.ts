import { tr } from "../i18n/index";
/** 提示词设置 (设置 → EvoWiki) and its centre-pane editors (app.js renderPrompt). */
export const PROMPT_COPY = {
  category: (category: string): string => tr(`wiki.prompt.category.${category}`),
  name: (step: string): string => tr(`wiki.prompt.name.${step}`),
  get title() { return tr("wiki.prompt-copy.PROMPT_COPY.title.1"); },
  helpLabel: (name: string): string => tr("wiki.prompt-copy.PROMPT_COPY.helpLabel.2", { v0: name }),
  get edited() { return tr("wiki.prompt-copy.PROMPT_COPY.edited.3"); },
  /** A step whose own wording the old-format rules could not place (its full note: PromptStepOut.format_note, in the editor). */
  get formatChanged() { return tr("wiki.prompt-copy.PROMPT_COPY.formatChanged.4"); },
  get save() { return tr("wiki.prompt-copy.PROMPT_COPY.save.5"); },
  get reset() { return tr("wiki.prompt-copy.PROMPT_COPY.reset.6"); },
  promptLabel: (name: string): string => tr("wiki.prompt-copy.PROMPT_COPY.promptLabel.7", { v0: name }),
  get saved() { return tr("wiki.prompt-copy.PROMPT_COPY.saved.8"); },
  get resetDone() { return tr("wiki.prompt-copy.PROMPT_COPY.resetDone.9"); },
  get loading() { return tr("wiki.prompt-copy.PROMPT_COPY.loading.10"); },
  /** A step that is no longer in the list. */
  get gone() { return tr("wiki.prompt-copy.PROMPT_COPY.gone.11"); },
} as const;
