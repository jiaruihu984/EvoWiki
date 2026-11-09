/**
 * Where learning goes on, as the server says it (GET /wiki/progress, wikiapp/progress.py: one source of progress). The
 * plugin never works it out: 今天's 下一步, a goal row, a page's end and the chat goal run all render a `StepOut`.
 */
import { WIKI_COPY } from "./copy";
import type { GoalProgressOut, StepOut } from "./schema";
import type { WikiState } from "./state";
import { TODAY_COPY } from "./today-copy";

/** 今天's step (the goal the learner was last on, else the first not done); null before the read lands. */
export function todayStep(s: WikiState): StepOut | null {
  return s.progress.kind === "ready" ? s.progress.value.today : null;
}

/** A goal's progress by its id (a map id, or a lone path's id). */
export function goalOf(s: WikiState, goalId: string): GoalProgressOut | undefined {
  return s.progress.kind === "ready" ? s.progress.value.goals.find((g) => g.goal_id === goalId) : undefined;
}

/** There are goals and every one is done (today's step is then none): 今天的学习已完成. */
export function allGoalsDone(s: WikiState): boolean {
  return s.progress.kind === "ready" && s.progress.value.goals.length > 0 && s.progress.value.goals.every((g) => g.state === "done");
}

/** Nothing left to do on this step (the goal is done, or there is no goal). */
export const finished = (step: StepOut): boolean => step.kind === "goal_done" || step.kind === "none";

/** What a step's button says: the next block by its name (a block's own diagnosis too), else 继续学习 (the opening diagnosis too). */
export function stepLabel(step: StepOut): string {
  return (step.kind === "enter_block" || step.kind === "diagnosis") && step.region_name !== "" ? WIKI_COPY.endNextBlock(step.region_name) : TODAY_COPY.continueStep;
}
