/**
 * One evoui.js result event, exactly as the renderer's `opts.onEvent` hands it over (EvoUI v2 D3; evoui.js `ctx.emit`):
 * item / summary / route / request, each with its node key, the render's `session` and the `revision`. It is the wire
 * shape of BlockEventIn (POST /wiki/assets/{id}/results `events`), sent as is; the server grades items and dedups by
 * eventKey, so sending an event again is harmless.
 */
export interface BlockEvent {
  type: "item" | "summary" | "route" | "request";
  node: string;
  revision: number;
  block?: string | null;
  session?: string | null;
  attempt?: number | null;
  item?: number | string | null;
  [field: string]: unknown;
}

/** The server's dedup key (wikiapp/block_results.py `event_key`), used to keep one copy of an event while it waits. */
export function eventKey(ev: BlockEvent): string {
  if (ev.kind === "component") return `${ev.node}:${JSON.stringify(ev.item ?? null)}`;
  const key = ev.type === "item"
    ? `${ev.node}:${JSON.stringify(ev.item ?? null)}:${ev.attempt == null ? "None" : String(ev.attempt)}:${String(ev.revision)}`
    : `${ev.node}:${ev.type}:${String(ev.attempt ?? 0)}:${String(ev.revision)}`;
  return typeof ev.session === "string" && ev.session !== "" ? `${ev.session}|${key}` : key;
}

/** evoui.js `progress()`: {v: 2, revision, state, nodes}; handed back to `render` as `opts.progress`. */
export interface BlockProgressData { revision: number; [field: string]: unknown }
/** Wire shape of BlockProgressIn (POST /wiki/assets/{id}/results `progress`). */
export interface BlockProgressIn { revision: number; data: BlockProgressData }
/** Wire shape of BlockProgressOut (GET /wiki/assets/{id}/progress); revision 0 + data null = nothing stored. */
export interface BlockProgressOut { revision: number; data: BlockProgressData | null }
