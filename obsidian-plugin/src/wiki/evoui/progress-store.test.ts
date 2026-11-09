import { expect, it } from "vitest";
import { localBlockProgressStore } from "./progress-store";
import { localBlockEventStore } from "./event-store";
it("scopes newest progress separately from evidence and from other accounts/servers", () => {
  const disk = new Map<string, string>();
  const storage = { getItem: (key: string) => disk.get(key) ?? null, setItem: (key: string, value: string) => { disk.set(key, value); }, removeItem: (key: string) => { disk.delete(key); } };
  const identity = { server: "server", user: "user" };
  const store = localBlockProgressStore(storage, identity);
  const latest = [{ assetId: "ia_1", progress: { revision: 9, v: 2, state: { answer: 1 }, nodes: {} } }];
  store.save(latest);
  expect(localBlockProgressStore(storage, identity).load()).toEqual(latest);
  expect(localBlockProgressStore(storage, { ...identity, user: "other" }).load()).toEqual([]);
  expect(localBlockProgressStore(storage, { ...identity, server: "other" }).load()).toEqual([]);
  expect(localBlockEventStore(storage, identity).load()).toEqual([]);
  store.save([]);
  expect(store.load()).toEqual([]);
});
