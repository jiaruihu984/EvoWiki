/**
 * Whether a block may be drawn, inside the sandboxed frame: the spec's rules (D1's JS validator, which mirrors validate.py)
 * plus the guard (src/evowiki/deep/evoui/guard.js, one script shared with the server's render check) and the self-test.
 * Isolation is the main defence; these stay as they are until isolation has passed its security tests.
 */
import "../../../../../src/evowiki/deep/evoui/guard.js";
import { evoEngine } from "./engine";
import { engineIsSafe } from "./engine-safety";

export interface EvouiBlock {
  type: "evoui"; title: string; question: string; state: Record<string, unknown>; derived: Record<string, string>;
  body: unknown; fallback: string; probes?: unknown;
}

type Tok = { t: "num"; v: number } | { t: "str"; v: string } | { t: "id"; v: string } | { t: "op"; v: string };
interface EvoUIGuard {
  lexExpr(this: void, s: string): Tok[] | null;
  expressionProblem(this: void, src: string, costOut?: { cost: number; toks?: Tok[][] }): string | null;
  taintProblem(this: void, block: unknown, toks: Tok[][]): string | null;
  adapterProblem(this: void, block: unknown): string | null;
  MAX_EXPR_COST: number;
  MAX_BLOCK_COST: number;
}

const guard = (window as unknown as { EvoUIGuard: EvoUIGuard }).EvoUIGuard;
export const { lexExpr, expressionProblem, taintProblem, adapterProblem, MAX_EXPR_COST, MAX_BLOCK_COST } = guard;

export type Validated = { ok: true; block: EvouiBlock } | { ok: false; errors: string[] };

/** Pure: it draws nothing and throws nothing. */
export function validateBlock(block: unknown): Validated {
  if (!engineIsSafe()) return { ok: false, errors: ["renderer failed the safety self-test"] };
  const problem = adapterProblem(block);
  if (problem !== null) return { ok: false, errors: [problem] };
  const engine = evoEngine();
  if (engine === null) return { ok: false, errors: ["renderer missing"] };
  let errors: string[];
  try { errors = engine.validate(block); } catch (e) { errors = [e instanceof Error ? e.message : String(e)]; }
  return errors.length > 0 ? { ok: false, errors } : { ok: true, block: block as EvouiBlock };
}
