import type { BlockProgressData } from "./block-event";
import { readFromFrame } from "./protocol";
import { validAssetId } from "../embed/embed-block";

export interface QueuedBlockProgress { assetId: string; progress: BlockProgressData }
/** Latest unsent UI state, separate from immutable result evidence. */
export interface BlockProgressStore {
  load(): QueuedBlockProgress[];
  save(progress: readonly QueuedBlockProgress[]): void;
}
export function localBlockProgressStore(storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  identity: { server: string; user: string }): BlockProgressStore {
  const key = `evowiki.blockProgress:${JSON.stringify([identity.server, identity.user])}`;
  return {
    load: () => {
      const raw = storage.getItem(key);
      if (raw === null) return [];
      const value: unknown = JSON.parse(raw);
      if (!Array.isArray(value)) throw new Error("Stored interactive progress is not a list");
      return value.map((entry: unknown) => {
        if (typeof entry !== "object" || entry === null) throw new Error("Invalid stored interactive progress");
        const row = entry as Record<string, unknown>;
        const message = readFromFrame({ t: "progress", progress: row.progress });
        if (typeof row.assetId !== "string" || !validAssetId(row.assetId) || message?.t !== "progress") throw new Error("Invalid stored interactive progress");
        return { assetId: row.assetId, progress: message.progress };
      });
    },
    save: (progress) => {
      if (progress.length === 0) storage.removeItem(key);
      else storage.setItem(key, JSON.stringify(progress));
    },
  };
}
