/**
 * The script of the sandboxed frame: the whole of what runs for an EvoUI block (esbuild.config.mjs bundles it into a string
 * for frame-host.ts). The only peer it listens to is the main window, two levels up (main window > gate frame > this frame).
 */
import "./harden";
import type { FromFrame, ToFrame } from "../protocol";
import { startFrame } from "./runtime";

const main = window.parent.parent;
const mount = document.getElementById("mount")!;
const post = (m: FromFrame): void => { main.postMessage(m, "*"); };
const receive = startFrame(window, mount, post);
window.addEventListener("message", (e) => { if (e.source === main) receive(e.data as ToFrame); });
post({ t: "up", boot: crypto.getRandomValues(new Uint32Array(4)).join("-") });
