import { WIKI_COPY } from "./copy";
import type { CatalogOut, CellOut, GoalDraftOut, KGridOut, PageOut, PathOut } from "./schema";

export interface Names {
  page(id: string): string | null; concept(id: string): string | null;
}

/**
 * Collects names from whatever is loaded. Page titles from the catalog and from a page itself are authoritative
 * and overwrite earlier page names; draft titles only fill gaps. Concept names keep the
 * first non-empty name, except that a response's names map (addNames) replaces it.
 */
export class NameBook implements Names {
  private readonly pages = new Map<string, string>();
  private readonly concepts = new Map<string, string>();

  addCatalog(c: CatalogOut): void {
    for (const p of [...c.learning, ...c.shelf.pages]) this.set(this.pages, p.page_id, p.title);
  }

  /**
   * A response's `names` map (CatalogOut: concept and page ids → display name) is
   * authoritative for both kinds and replaces what was there. The map does not say which ids are concepts and which
   * are pages, so each name goes to both tables (they are only ever looked up by an id of the right kind). The
   * server's 未命名… placeholders are skipped: they never replace a known name, and an unknown id shows the same text.
   */
  addNames(map: Record<string, string>): void {
    for (const [id, name] of Object.entries(map)) {
      if (name === WIKI_COPY.unnamedConcept || name === WIKI_COPY.unnamedPage) continue;
      this.set(this.concepts, id, name);
      this.set(this.pages, id, name);
    }
  }

  addK(k: KGridOut): void {
    for (const c of k.concepts) this.put(this.concepts, c.id, c.name);
  }

  /**
   * `titleFromServer` false: the title may be a local seed (the draft title while a new page streams), so it only
   * fills a gap.
   */
  addPage(p: PageOut, titleFromServer = true): void {
    if (titleFromServer) this.set(this.pages, p.page_id, p.title);
    else this.put(this.pages, p.page_id, p.title);
    this.cells(p.assumed);
    for (const s of p.sections) this.cells(s.cells);
  }

  addDraft(d: GoalDraftOut): void {
    for (const c of d.concepts) this.put(this.concepts, c.id, c.name);
  }

  /** A learning path's concept names (gap filler). */
  addPath(p: PathOut): void {
    for (const pg of p.pages) for (const c of pg.concepts) this.put(this.concepts, c.concept_id, c.name);
  }

  page(id: string): string | null { return this.pages.get(id) ?? null; }
  concept(id: string): string | null { return this.concepts.get(id) ?? null; }

  private cells(list: CellOut[]): void {
    for (const c of list) this.put(this.concepts, c.concept_id, c.concept_name);
  }

  /** Authoritative: a non-empty name replaces whatever was there. */
  private set(m: Map<string, string>, id: string, name: string): void {
    if (name) m.set(id, name);
  }

  /** Gap filler: only when there is no non-empty name yet. */
  private put(m: Map<string, string>, id: string, name: string): void {
    if (name && !m.get(id)) m.set(id, name);
  }
}
