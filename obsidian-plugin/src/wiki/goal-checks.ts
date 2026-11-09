/** Goal completion uses ordinary embedded checks; only their placement differs. */
import type { SectionOut } from "./schema";

export const isGoalCheck = (section: { id: string; role: string }): boolean =>
  section.role === "check" && section.id.startsWith("goal_check_");

/** Explicitly marked mastery hides a check; an answer alone keeps its question and result visible. */
export const isMasteredCheck = (section: Pick<SectionOut, "role" | "pinned" | "cells">): boolean =>
  section.role === "check" && !section.pinned && section.cells.length > 0 && section.cells.every((cell) => cell.declared === "know");
