import { resolve } from "node:path";
import { EventEmitter } from "node:events";
import { spawn } from "node:child_process";
import { afterEach, expect, it, vi } from "vitest";
import { LocalSetupClient, parseLocalSetup, type LocalSetupMetadata } from "./local-setup";
vi.mock("node:child_process", () => { const spawn = vi.fn(); return { spawn, default: { spawn } }; });
vi.mock("node:fs/promises", () => { const realpath = (value: string) => Promise.resolve(value); return { realpath, default: { realpath } }; });
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });
const root = resolve("中文 空格", "checkout");
const metadata: LocalSetupMetadata = { root, executable: resolve("tools", "uv.exe"), args: ["run", "--isolated", "--no-project", "--with", "python-dotenv==1.2.4", "python", resolve(root, "scripts", "evowiki_run.py"), "setup"] };
it("only the fixed executable and setup script are accepted, including Unicode and spaces", () => {
  expect(parseLocalSetup(metadata)).toEqual(metadata);
  expect(parseLocalSetup({ ...metadata, executable: resolve("tools", "cmd.exe") })).toBeNull();
  expect(parseLocalSetup({ ...metadata, args: [...metadata.args, "--anything"] })).toBeNull();
  expect(parseLocalSetup({ ...metadata, args: metadata.args.map((arg, index) => index === 6 ? resolve("other", "scripts", "evowiki_run.py") : arg) })).toBeNull();
});
it("credentials travel only in stdin, never process arguments or returned results", async () => {
  const child = Object.assign(new EventEmitter(), { stdin: { on: vi.fn(), end: vi.fn() },
    stdout: Object.assign(new EventEmitter(), { setEncoding: vi.fn() }), stderr: { resume: vi.fn() }, kill: vi.fn() });
  vi.mocked(spawn).mockReturnValue(child as never);
  const pending = new LocalSetupClient(metadata).request({ op: "check", language: "en", provider: "anthropic", api_key: "synthetic-qa-key" });
  await vi.waitFor(() => expect(spawn).toHaveBeenCalledOnce());
  expect(JSON.stringify(vi.mocked(spawn).mock.calls)).not.toContain("synthetic-qa-key");
  expect(child.stdin.end).toHaveBeenCalledWith(expect.stringContaining('"api_key":"synthetic-qa-key"'));
  child.stdout.emit("data", JSON.stringify({ ok: true, code: "checked", revision: "r1", providers: [], selected_provider: null, errors: [], restart: { state: "unchanged", port: null } })); child.emit("close", 0);
  expect(JSON.stringify(await pending)).not.toContain("synthetic-qa-key");
  expect(vi.mocked(spawn).mock.calls[0]?.[2]).toMatchObject({ shell: false, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
});

it("a slow restart stays serialized after its uncertain deadline until its process closes", async () => {
  vi.useFakeTimers();
  const child = Object.assign(new EventEmitter(), { stdin: { on: vi.fn(), end: vi.fn() }, stdout: Object.assign(new EventEmitter(), { setEncoding: vi.fn() }), stderr: { resume: vi.fn() }, kill: vi.fn() });
  vi.mocked(spawn).mockReturnValue(child as never);
  const client = new LocalSetupClient(metadata);
  const pending = client.request({ op: "apply", language: "en" });
  const rejected = expect(pending).rejects.toThrow("local_setup_timeout");
  await vi.advanceTimersByTimeAsync(120_001);
  expect(child.kill).not.toHaveBeenCalled();
  await expect(client.request({ op: "apply", language: "en" })).rejects.toThrow("local_setup_in_progress");
  await vi.advanceTimersByTimeAsync(540_000); await rejected;
  await expect(new LocalSetupClient(metadata).request({ op: "apply", language: "en" })).rejects.toThrow("local_setup_in_progress");
  expect(child.kill).not.toHaveBeenCalled(); child.emit("close", 0);
});

it("model discovery sends its draft only over stdin and returns the provider catalog", async () => {
  const child = Object.assign(new EventEmitter(), { stdin: { on: vi.fn(), end: vi.fn() },
    stdout: Object.assign(new EventEmitter(), { setEncoding: vi.fn() }), stderr: { resume: vi.fn() }, kill: vi.fn() });
  vi.mocked(spawn).mockReturnValue(child as never);
  const pending = new LocalSetupClient(metadata).request({ op: "models", language: "en", provider: "anthropic", api_key: "synthetic-model-key" });
  await vi.waitFor(() => expect(spawn).toHaveBeenCalledOnce());
  expect(JSON.stringify(vi.mocked(spawn).mock.calls)).not.toContain("synthetic-model-key");
  expect(child.stdin.end).toHaveBeenCalledWith(expect.stringContaining('"op":"models"'));
  const model_catalog = { provider: "anthropic", models: ["claude-new", "claude-older"] };
  child.stdout.emit("data", JSON.stringify({ ok: true, code: "ok", revision: "r1", providers: [], selected_provider: null,
    errors: [], restart: { state: "unchanged", port: null }, model_catalog }));
  child.emit("close", 0);
  expect((await pending).model_catalog).toEqual(model_catalog);
});
