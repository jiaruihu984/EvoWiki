import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "./api/errors";
import { MaintenanceClient, MAINTENANCE_COPY as C, renderMaintenanceSection, type BackupStatus } from "./maintenance";
import { clearEwNotices, ewNotices } from "../test/ew-notices";

const healthy = (): BackupStatus => ({ disabled: false, running: false,
  last_success_at: "2026-11-08T10:00:00Z", last_failure_reason: null, warning: null, files: ["backup.dump"] });
const flush = async (): Promise<void> => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
afterEach(() => { document.body.replaceChildren(); clearEwNotices(); });

function rig(state = healthy(), configured = true) {
  const status = vi.fn(() => Promise.resolve(state));
  const backup = vi.fn(() => Promise.resolve(healthy()));
  const root = document.body.appendChild(document.createElement("section"));
  const handle = renderMaintenanceSection(root, { status, backup }, () => configured);
  const button = (text: string): HTMLButtonElement => {
    const found = [...root.querySelectorAll("button")].find((b) => b.textContent === text);
    if (!found) throw new Error(`No ${text} button`);
    return found;
  };
  return { status, backup, root, handle, button };
}

describe("Maintenance settings", () => {
  it("uses the current server identity for the status and explicit backup endpoints", async () => {
    let baseUrl = "http://127.0.0.1:8123", userId = "first";
    const request = vi.fn(() => Promise.resolve({ status: 200, text: JSON.stringify(healthy()) }));
    const client = new MaintenanceClient(() => ({ baseUrl, userId }), { request });
    await client.status();
    baseUrl = "http://127.0.0.1:8124"; userId = "second";
    await client.backup();
    expect(request.mock.calls).toEqual([
      [{ method: "GET", url: "http://127.0.0.1:8123/health/backup", headers: { "X-EvoWiki-User": "first" } }],
      [{ method: "POST", url: "http://127.0.0.1:8124/health/backup/run", headers: { "X-EvoWiki-User": "second" } }],
    ]);
  });

  it("does not ask an unconfigured server", async () => {
    const r = rig(healthy(), false);
    await flush();
    expect(r.status).not.toHaveBeenCalled();
    expect(r.root.textContent).toBe(C.needsSettings);
  });

  it("preserves disabled and failed backup responses from the API", async () => {
    for (const status of [409, 503]) {
      const client = new MaintenanceClient(() => ({ baseUrl: "http://local", userId: "u" }), {
        request: () => Promise.resolve({ status, text: '{"detail":"备份未完成，请检查配置"}' }),
      });
      await expect(client.backup()).rejects.toMatchObject({ status, detail: "备份未完成，请检查配置" });
    }
  });

  it("keeps a current warning visible beside historical success, with an explicit retry", async () => {
    const r = rig({ ...healthy(), last_failure_reason: "disk full", warning: "disk full" });
    await flush();
    expect(r.root.textContent).toContain("最近成功");
    expect(r.root.textContent).toContain("disk full");
    expect(r.button(C.retry).disabled).toBe(false);
    expect(ewNotices()).toEqual([]);
  });

  it("an old failure cleared by the server does not remain a current warning", async () => {
    const r = rig({ ...healthy(), last_failure_reason: "old error" });
    await flush();
    expect(r.root.textContent).not.toContain("old error");
    expect(r.button(C.now).disabled).toBe(false);
  });

  it("disabled backup is explicit, cannot be run, and can be refreshed after configuration", async () => {
    const r = rig({ ...healthy(), disabled: true, warning: "backups are disabled" });
    await flush();
    expect(r.root.textContent).toContain(C.disabled);
    expect(r.button(C.retry).disabled).toBe(true);
    r.button(C.retry).click();
    expect(r.backup).not.toHaveBeenCalled();
    r.status.mockResolvedValueOnce(healthy());
    r.button(C.refresh).click();
    await flush();
    expect(r.root.textContent).not.toContain(C.disabled);
    expect(r.button(C.now).disabled).toBe(false);
  });

  it("a running backup is visible and never launches a second backup", async () => {
    const r = rig({ ...healthy(), running: true });
    await flush();
    expect(r.root.textContent).toContain(C.running);
    expect(r.button(C.now).disabled).toBe(true);
    r.button(C.now).click();
    expect(r.backup).not.toHaveBeenCalled();
    expect(r.button(C.refresh).disabled).toBe(false);
  });

  it("runs one explicit retry, waits visibly, and clears the warning when it succeeds", async () => {
    const r = rig({ ...healthy(), warning: "disk full" });
    await flush();
    let finish = (_s: BackupStatus): void => undefined;
    r.backup.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    r.button(C.retry).click();
    r.button(C.retry).click();
    expect(r.backup).toHaveBeenCalledOnce();
    expect(r.root.textContent).toContain(C.running);
    expect(r.button(C.refresh).disabled).toBe(true);
    finish(healthy());
    await flush();
    expect(r.root.textContent).not.toContain("disk full");
    expect(r.button(C.now).disabled).toBe(false);
    expect(ewNotices()).toEqual([]);
  });

  it("failed backup stays visible, does not retry itself, and a user retry can recover", async () => {
    const r = rig();
    await flush();
    r.backup.mockRejectedValueOnce(new ApiError(503, "备份未完成，请查看备份状态后重试"));
    r.button(C.now).click();
    await flush();
    expect(r.root.textContent).toContain("备份未完成");
    expect(r.backup).toHaveBeenCalledOnce();
    expect(r.button(C.retry).disabled).toBe(false);
    r.button(C.retry).click();
    await flush();
    expect(r.backup).toHaveBeenCalledTimes(2);
    expect(r.root.textContent).not.toContain("备份未完成");
    expect(ewNotices()).toEqual([]);
  });

  it("a failed status read offers one manual retry; disposal ignores a late response", async () => {
    const r = rig();
    await flush();
    r.status.mockRejectedValueOnce(new OfflineError());
    r.button(C.refresh).click();
    await flush();
    expect(r.root.textContent).toContain(C.offline);
    expect(r.status).toHaveBeenCalledTimes(2);
    let finish = (_s: BackupStatus): void => undefined;
    r.status.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    r.button(C.refresh).click();
    r.handle.dispose();
    const before = r.root.textContent;
    finish({ ...healthy(), warning: "late old server result" });
    await flush();
    expect(r.root.textContent).toBe(before);
    expect(r.root.textContent).not.toContain("late old server result");
  });
});
