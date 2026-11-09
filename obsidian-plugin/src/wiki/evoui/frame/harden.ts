/**
 * Runs before anything else in the frame. CSP has no working switch for these in Obsidian's Chromium (measured: the `webrtc`
 * directive does not stop a STUN packet, and sandboxed frames report Notification as granted), so the frame's realm goes without.
 */
const w = window as unknown as Record<string, unknown>;
for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel", "RTCIceTransport", "Notification"]) {
  try { delete w[name]; } catch { /* not configurable: the CSP is still there */ }
}
