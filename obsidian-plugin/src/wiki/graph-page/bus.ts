/**
 * "The graph changed": the 知识图谱 page's store announces every graph / path change it applies (an edit, 拆成多页 and its
 * undo, a read), and 今天's goal rows and thumbnail read GET /wiki/graph again at once instead of waiting for their
 * freshness window. One signal for the whole plugin; subscribers must not announce what they read themselves.
 */
const subs = new Set<() => void>();

export const graphBus = {
  subscribe(fn: () => void): () => void { subs.add(fn); return () => { subs.delete(fn); }; },
  emit(): void { for (const fn of [...subs]) fn(); },
};
