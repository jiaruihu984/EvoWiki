import type { UsageClient } from "./wiki/usage";
/** The account gate delegates to one setup flow; no separate budget modal. */
export async function prepareBudget(client: Pick<UsageClient, "settings">, setup: (required: boolean) => Promise<void>, signal?: AbortSignal, reopen = false): Promise<void> {
  signal?.throwIfAborted();
  const settings = await client.settings();
  signal?.throwIfAborted();
  if (settings.budget_confirmed && !reopen) return;
  await setup(!settings.budget_confirmed);
  signal?.throwIfAborted();
}
