/** One-shot animations survive a re-render: a rebuilt node's CSS animations restart at 0, so they are moved to where they would be. */

/** Moves the CSS animations of `node` (and below) to `Date.now() - startedAt`; runs after the node is attached (a microtask). No getAnimations (jsdom): nothing. */
export function resumeAnimations(node: HTMLElement, startedAt: number): void {
  queueMicrotask(() => {
    if (!node.isConnected || typeof node.getAnimations !== "function") return;
    const t = Date.now() - startedAt;
    for (const an of node.getAnimations({ subtree: true })) an.currentTime = t;
  });
}

/** How long a one-shot animation counts as running after `at`. */
export const isFresh = (at: number, windowMs: number): boolean => Date.now() - at < windowMs;
