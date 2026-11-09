import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import { basename, isAbsolute, join, normalize } from "node:path";
import type { UiLanguage } from "./i18n";

export interface LocalSetupMetadata { root: string; executable: string; args: string[] }
export interface ProviderStatus { id: string; configured: boolean; base_url: string; model: string; models: string[] }
export interface LocalSetupResult {
  ok: boolean; code: string; revision: string; providers: ProviderStatus[]; selected_provider: string | null;
  errors: { field: string; code: string }[]; restart: { state: string; port: number | null };
  local_options?: { forge_enabled: boolean; backup_directory: string; backup_keep: number };
  model_catalog?: { provider: string; models: string[] };
}
export interface LocalSetupRequest {
  op: "status" | "check" | "apply" | "models"; language: UiLanguage; expected_revision?: string;
  provider?: string; api_key?: string; clear_key?: boolean; base_url?: string;
  model?: string; models?: string[]; prices_file?: string;
  forge_enabled?: boolean; backup_directory?: string; backup_keep?: number;
}
const prefix = ["run", "--isolated", "--no-project", "--with", "python-dotenv==1.2.4", "python"];
export function parseLocalSetup(value: unknown): LocalSetupMetadata | null {
  if (!value || typeof value !== "object") return null;
  const m = value as Record<string, unknown>;
  if (typeof m.root !== "string" || typeof m.executable !== "string" || !isAbsolute(m.root) || !isAbsolute(m.executable)
    || !/^uv(?:\.exe)?$/i.test(basename(m.executable)) || !Array.isArray(m.args) || m.args.length !== 8) return null;
  const args = m.args;
  if (!prefix.every((part, i) => part === args[i]) || args[7] !== "setup" || typeof args[6] !== "string"
    || normalize(args[6]) !== normalize(join(m.root, "scripts", "evowiki_run.py"))) return null;
  return { root: m.root, executable: m.executable, args: args as string[] };
}

const runningInstances = new Set<string>();

/** Native desktop pipe only. Keys never enter argv, plugin storage, or error text. */
export class LocalSetupClient {
  constructor(private readonly metadata: LocalSetupMetadata) {}
  async request(request: LocalSetupRequest): Promise<LocalSetupResult> {
    const m = parseLocalSetup(this.metadata);
    if (!m) throw new Error("local_setup_invalid_instance");
    const [root, script] = await Promise.all([realpath(m.root), realpath(m.args[6]!)]);
    if (normalize(script).toLowerCase() !== normalize(join(root, "scripts", "evowiki_run.py")).toLowerCase()) throw new Error("local_setup_invalid_instance");
    const instance = normalize(root).toLowerCase();
    if (runningInstances.has(instance)) throw new Error("local_setup_in_progress");
    runningInstances.add(instance);
    return new Promise((resolve, reject) => {
      const child = spawn(m.executable, m.args, { cwd: root, shell: false, windowsHide: true, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" } });
      let output = ""; let settled = false;
      const fail = (code: string): void => { if (!settled) { settled = true; clearTimeout(timeout); reject(new Error(code)); } };
      // Helper control takes at most 600s; allow uv startup. Unknown outcomes stay locked until close.
      const timeout = setTimeout(() => fail("local_setup_timeout"), 660_000);
      child.on("error", () => { runningInstances.delete(instance); fail("local_setup_could_not_start"); });
      child.stdin.on("error", () => fail("local_setup_pipe_failed"));
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => { output += chunk; if (output.length > 1_000_000) { child.kill(); fail("local_setup_invalid_response"); } });
      // Drain stderr without storing or displaying it: native failures return a safe code on stdout.
      child.stderr.resume();
      child.on("close", () => {
        runningInstances.delete(instance);
        if (settled) return;
        try {
          const result = JSON.parse(output) as LocalSetupResult;
          if (typeof result.ok !== "boolean" || typeof result.code !== "string" || !Array.isArray(result.errors)) throw new Error();
          if (result.model_catalog && (!result.ok || result.model_catalog.provider !== request.provider
            || !Array.isArray(result.model_catalog.models) || !result.model_catalog.models.every(model => typeof model === "string" && model.length > 0))) throw new Error();
          settled = true; clearTimeout(timeout); resolve(result);
        } catch { fail("local_setup_invalid_response"); }
      });
      child.stdin.end(JSON.stringify(request));
    });
  }
}
