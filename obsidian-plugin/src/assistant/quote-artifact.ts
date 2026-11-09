/**
 * The artifact id of the section a quote was selected from, taken from the page as last rendered (what the user
 * was shown), never fetched. The quote's heading must name exactly one section of that open page; otherwise nothing.
 */
import type { PageOut } from "../wiki/schema";

export function quoteArtifactId(page: Pick<PageOut, "page_id" | "sections"> | null, pageId: string, heading: string | null): string | null {
  if (page === null || page.page_id !== pageId || heading === null || heading.trim() === "") return null;
  const hit = page.sections.filter((s) => s.heading.trim() === heading.trim());
  return hit.length === 1 ? hit[0]!.artifact_id : null;
}
