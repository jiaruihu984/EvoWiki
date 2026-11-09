import { tr } from "./i18n/index";
/** Backup status stays in Settings; reads and explicit retries never produce notices. */
import { jsonCall, type ClientConfig, type JsonTransport } from "./api/client";
import { ApiError, OfflineError } from "./api/errors";
import { btn, el } from "./ui/ew";
import { setStatus, settingRow, statusLine } from "./ui/settings-rows";

export interface BackupStatus {
  disabled: boolean;
  running: boolean;
  last_success_at: string | null;
  last_failure_reason: string | null;
  warning: string | null;
  files: string[];
}

export class MaintenanceClient {
  constructor(private readonly config: () => ClientConfig, private readonly http: JsonTransport) {}
  status(): Promise<BackupStatus> { return jsonCall(this.http, this.config(), "GET", "/health/backup"); }
  backup(): Promise<BackupStatus> { return jsonCall(this.http, this.config(), "POST", "/health/backup/run"); }
}

export const MAINTENANCE_COPY = {
  get heading() { return tr("maintenance.MAINTENANCE_COPY.heading.1"); },
  get loading() { return tr("maintenance.MAINTENANCE_COPY.loading.2"); },
  get disabled() { return tr("maintenance.MAINTENANCE_COPY.disabled.3"); },
  get running() { return tr("maintenance.MAINTENANCE_COPY.running.4"); },
  get never() { return tr("maintenance.MAINTENANCE_COPY.never.5"); },
  lastSuccess: (at: string): string => tr("maintenance.MAINTENANCE_COPY.lastSuccess.6", { v0: new Date(at).toLocaleString() }),
  files: (count: number): string => tr("maintenance.MAINTENANCE_COPY.files.7", { v0: count }),
  get now() { return tr("maintenance.MAINTENANCE_COPY.now.8"); },
  get retry() { return tr("maintenance.MAINTENANCE_COPY.retry.9"); },
  get refresh() { return tr("maintenance.MAINTENANCE_COPY.refresh.10"); },
  get readFailed() { return tr("maintenance.MAINTENANCE_COPY.readFailed.11"); },
  get backupFailed() { return tr("maintenance.MAINTENANCE_COPY.backupFailed.12"); },
  get offline() { return tr("maintenance.MAINTENANCE_COPY.offline.13"); },
  get needsSettings() { return tr("maintenance.MAINTENANCE_COPY.needsSettings.14"); },
};

const message = (error: unknown, fallback: string): string => error instanceof OfflineError
  ? MAINTENANCE_COPY.offline : error instanceof ApiError ? error.detail : fallback;

export function renderMaintenanceSection(section: HTMLElement, client: Pick<MaintenanceClient, "status" | "backup">,
  configured: () => boolean): { dispose(): void } {
  let alive = true;
  let busy = false;
  let backingUp = false;
  let state: BackupStatus | null = null;
  let error = "";
  const body = section.appendChild(el("div"));
  const draw = (): void => {
    if (!alive) return;
    const status = statusLine();
    const refresh = btn(() => (MAINTENANCE_COPY.refresh), "link", () => { void read(); });
    refresh.disabled = busy;
    if (state === null) {
      setStatus(status, error || MAINTENANCE_COPY.loading);
      body.replaceChildren(status, refresh);
      return;
    }
    const label = state.disabled ? MAINTENANCE_COPY.disabled : state.running || backingUp ? MAINTENANCE_COPY.running
      : state.last_success_at === null ? MAINTENANCE_COPY.never : MAINTENANCE_COPY.lastSuccess(state.last_success_at);
    const action = btn(() => (error || state?.warning ? MAINTENANCE_COPY.retry : MAINTENANCE_COPY.now), "", () => { void backup(); });
    action.disabled = busy || state.disabled || state.running;
    const row = el("div");
    const fields = settingRow(row, label, null, action);
    if (state.files.length > 0) fields.left.append(el("div", "muted", () => (MAINTENANCE_COPY.files(state?.files.length ?? 0))));
    setStatus(status, error || (state.disabled ? "" : state.warning ?? ""));
    body.replaceChildren(row, status, refresh);
  };
  const read = async (): Promise<void> => {
    if (!alive || busy) return;
    busy = true;
    draw();
    try { const result = await client.status(); if (alive) { state = result; error = ""; } }
    catch (e) { if (alive) error = message(e, MAINTENANCE_COPY.readFailed); }
    finally { busy = false; draw(); }
  };
  const backup = async (): Promise<void> => {
    if (!alive || busy || state === null || state.disabled || state.running) return;
    busy = true;
    backingUp = true;
    error = "";
    draw();
    try { const result = await client.backup(); if (alive) state = result; }
    catch (e) { if (alive) error = message(e, MAINTENANCE_COPY.backupFailed); }
    finally { busy = false; backingUp = false; draw(); }
  };
  if (configured()) void read();
  else body.append(el("div", "muted", () => (MAINTENANCE_COPY.needsSettings)));
  return { dispose(): void { alive = false; } };
}
