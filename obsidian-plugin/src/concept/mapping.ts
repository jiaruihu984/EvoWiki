/** The namespaced frontmatter key `evowiki: { concept_id: <id> }` of a concept-linked note, read for the assistant (assistant/host.ts). */
const obj = (x: unknown): Record<string, unknown> | null =>
  x !== null && typeof x === "object" && !Array.isArray(x) ? (x as Record<string, unknown>) : null;

/** The binding only. Any other key (mastery, recall, role…) is never read: K and V come from the server. */
export function readConceptId(frontmatter: unknown): string | null {
  const id = obj(obj(frontmatter)?.evowiki)?.concept_id;
  return typeof id === "string" && id.trim() ? id.trim() : null;
}
