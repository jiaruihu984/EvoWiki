import type { BlockEvent } from "./block-event";
import { readFromFrame } from "./protocol";
import { validAssetId } from "../embed/embed-block";

export interface QueuedBlockEvent { assetId: string; event: BlockEvent }
/** Synchronous durable handoff: a failed save must not be reported as accepted. */
export interface BlockEventStore {
  load(): QueuedBlockEvent[];
  save(events: readonly QueuedBlockEvent[]): void;
}
export function localBlockEventStore(storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  identity: { server: string; user: string }): BlockEventStore {
  const key = `evowiki.blockEvents:${JSON.stringify([identity.server, identity.user])}`;
  return {
    load: () => {
      const raw = storage.getItem(key);
      if (raw === null) return [];
      const value: unknown = JSON.parse(raw);
      if (!Array.isArray(value)) throw new Error("Stored interactive results are not a list");
      return value.map((entry: unknown) => {
        if (typeof entry !== "object" || entry === null) throw new Error("Invalid stored interactive result");
        const row = entry as Record<string, unknown>;
        const message = readFromFrame({ t: "event", ev: row.event });
        if (typeof row.assetId !== "string" || !validAssetId(row.assetId) || message?.t !== "event") throw new Error("Invalid stored interactive result");
        return { assetId: row.assetId, event: message.ev };
      });
    },
    save: (events) => {
      if (events.length === 0) storage.removeItem(key);
      else storage.setItem(key, JSON.stringify(events));
    },
  };
}
