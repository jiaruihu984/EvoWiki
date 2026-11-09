/**
 * The contract tests run in node. The controller reaches the EvoUI renderer script (src/evowiki/deep/evoui/evoui.js, a
 * browser script that registers itself on `window`) through the asset-id scanner; the contract never draws a block, so
 * the script only needs a `window` to register on.
 */
const g = globalThis as { window?: unknown };
if (g.window === undefined) g.window = globalThis;
