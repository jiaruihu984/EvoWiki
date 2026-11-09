/**
 * The plugin's view of the assistant's HTTP shapes and SSE events: the wire types generated from
 * assistant/contracts.py and agent/tools.py (src/generated/assistant.ts, by scripts/gen_plugin_types.py; CONTRACT_FIELDS
 * there gives the parser its allowed keys), plus the plugin-side shapes below.
 */
export * from "../generated/assistant";

/** The user's edited draft of an action card (POST /wiki/bot/actions/{id}/approve `draft`, a bare dict on the server): the
 * new page list, in order. A page that was in the proposal keeps its `key` (with `title` when he renamed it: the server
 * keeps its key, level and covers); one the learner added has `title` and `isNew`. */
export interface ActionDraftPage { key?: string; title?: string; covers?: string; isNew?: boolean }
export type ActionDraft = { pages: ActionDraftPage[] } | { text: string };
