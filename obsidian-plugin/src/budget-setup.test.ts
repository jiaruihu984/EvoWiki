import { expect, it, vi } from "vitest";
import { prepareBudget } from "./budget-setup";
const client = (confirmed: boolean) => ({ settings: vi.fn(() => Promise.resolve({ budget_confirmed: confirmed, daily_limit_usd: 5, monthly_limit_usd: 30 })) });
it("existing confirmed accounts continue without resetting or opening setup", async () => {
  const setup = vi.fn(); await prepareBudget(client(true), setup); expect(setup).not.toHaveBeenCalled();
});
it("an unconfirmed account waits for the unified setup and never confirms on its own", async () => {
  let finish!: () => void; const setup = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  let ready = false; const pending = prepareBudget(client(false), setup).then(() => { ready = true; });
  await Promise.resolve(); expect(setup).toHaveBeenCalledWith(true); expect(ready).toBe(false);
  finish(); await pending; expect(ready).toBe(true);
});
it("reopening does not treat an existing account as new", async () => {
  const setup = vi.fn(() => Promise.resolve()); await prepareBudget(client(true), setup, undefined, true); expect(setup).toHaveBeenCalledWith(false);
});
it("closing unfinished setup keeps the account blocked", async () => {
  await expect(prepareBudget(client(false), () => Promise.reject(new Error("cancelled")))).rejects.toThrow("cancelled");
});
it("an aborted identity cannot open setup after the old settings read returns", async () => {
  const abort = new AbortController(); const setup = vi.fn(); const pending = prepareBudget(client(false), setup, abort.signal); abort.abort();
  await expect(pending).rejects.toBeDefined(); expect(setup).not.toHaveBeenCalled();
});
