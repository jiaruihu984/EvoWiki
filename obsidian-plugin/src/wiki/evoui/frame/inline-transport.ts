/** Tests only: the frame's runtime on a plain div in this window, over the same messages (jsdom runs no iframe). */
import type { FrameTransport, MakeTransport } from "../frame-host";
import { type FromFrame, readFromFrame, type ToFrame } from "../protocol";
import { startFrame } from "./runtime";

export const inlineTransport: MakeTransport = (host, receive): FrameTransport => {
  const boot = crypto.getRandomValues(new Uint32Array(4)).join("-");
  const mount = document.createElement("div");
  mount.className = "evowiki-evoui evowiki-evoui-framed";
  host.append(mount);
  const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
  const toFrame = startFrame(window, mount, (m: FromFrame) => {
    const read = readFromFrame(clone(m));
    if (read !== null) receive(read);
  });
  return {
    post: (m: ToFrame) => { toFrame(clone(m)); },
    remove: () => { mount.remove(); },
    connected: () => mount.isConnected,
    setHeight: () => undefined,
    start: () => { receive({ t: "up", boot }); },
  };
};
