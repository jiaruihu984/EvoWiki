/** Goal completion uses ordinary embedded checks; only their placement differs. */
export const isGoalCheck = (section: { id: string; role: string }): boolean =>
  section.role === "check" && section.id.startsWith("goal_check_");
