import { expect, it } from "vitest";
import { eventKey } from "./block-event";
import { localBlockEventStore } from "./event-store";
const storage = () => {
  const rows = new Map<string, string>();
  return { getItem: (key: string) => rows.get(key) ?? null, setItem: (key: string, value: string) => { rows.set(key, value); }, removeItem: (key: string) => { rows.delete(key); } };
};
const event = { type: "item" as const, node: "quiz", revision: 3, session: "original-session", attempt: 1, item: 0, response: 1 };
it("scopes queued evidence to the exact server and account, preserving its original payload", () => {
  const disk = storage();
  const original = localBlockEventStore(disk, { server: "https://one", user: "u1" });
  original.save([{ assetId: "ia_1", event }]);
  expect(localBlockEventStore(disk, { server: "https://two", user: "u1" }).load()).toEqual([]);
  expect(localBlockEventStore(disk, { server: "https://one", user: "u2" }).load()).toEqual([]);
  expect(localBlockEventStore(disk, { server: "https://one", user: "u1" }).load()).toEqual([{ assetId: "ia_1", event }]);
  original.save([]);
  expect(original.load()).toEqual([]);
});
it("uses the server's exact component and missing-attempt keys", () => {
  expect(eventKey({ ...event, kind: "component" })).toBe("quiz:0");
  expect(eventKey({ type: "item", node: "quiz", item: "词", revision: 3 })).toBe('quiz:"词":None:3');
});
