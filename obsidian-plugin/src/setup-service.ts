import { jsonCall } from "./api/client";
import type { WikiConfig, WikiHttp } from "./wiki/api";
export interface ForgePermissions {
  tool: boolean; patch: boolean; requested_tool: boolean; requested_patch: boolean; global_allowed: boolean; blockers: string[];
}
export interface SetupSnapshot {
  model_configured: boolean;
  rsi: { on: boolean; available: boolean; blockers: string[] };
  forge: ForgePermissions;
  background: { paused: boolean; includes_memory: boolean; idle_write: boolean };
  backup: { enabled: boolean; directory: string | null; keep: number; every_seconds: number };
  usage_timezone: string;
}
export class SetupService {
  constructor(private readonly config: () => WikiConfig, private readonly http: WikiHttp) {}
  get(): Promise<SetupSnapshot> { return jsonCall(this.http, this.config(), "GET", "/wiki/setup"); }
  forge(body: { tool: boolean; patch: boolean }): Promise<ForgePermissions> { return jsonCall(this.http, this.config(), "PUT", "/wiki/setup/forge", body); }
}
