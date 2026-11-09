/**
 * CSP has no working switch for WebRTC in Obsidian's Chromium: a STUN packet leaves any frame that can run a script (an
 * `app` block, an embedded-html file). Only the window's own WebRTC policy stops it (measured: 20 datagrams without it, 0
 * with it). Set when the plugin loads and for each popout window.
 */
export function blockWebRtcUdp(win: Window): void {
  const w = win as unknown as { require?: (m: string) => { remote: { getCurrentWebContents(): { setWebRTCIPHandlingPolicy(p: string): void } } } };
  if (w.require === undefined) return;      // not Electron: no such policy to set
  try {
    w.require("electron").remote.getCurrentWebContents().setWebRTCIPHandlingPolicy("disable_non_proxied_udp");
  } catch (e) { console.warn("EvoWiki: could not set the WebRTC policy of this window", e); }
}
