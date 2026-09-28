/**
 * Research-list state: accumulating `/api/runs` pages and merging follow-up
 * chains over the loaded set (M9).
 *
 * The endpoint is a keyset page, newest first, with an opaque `nextCursor` —
 * not the whole history. The host used to clamp the list to its newest window
 * and the sidebar treated that one response as everything, so older research
 * vanished silently; and a page boundary can fall inside a follow-up chain, in
 * which case the row must not be titled after one of its intermediate turns.
 * This module is the pure half of both: `mergeRunPage` folds a page into what
 * is already loaded, `groupRunChains` folds the loaded turns into one row per
 * research and names the chain roots a page boundary left out.
 */

/**
 * Nothing loaded, nothing to ask for. Shaped like {@link mergeRunPage}'s output
 * so a caller can hand it straight to the view.
 */
export function emptyRunList() {
  return { runs: [], cursor: null, hasMore: false };
}

/**
 * Fold one server page into the accumulated state.
 *
 * Pages arrive newest-first and the server keeps them non-overlapping, so the
 * merge appends; a run seen twice (a repeated request, an overlapping window)
 * is skipped rather than doubling a row. A page that omits pagination metadata
 * keeps the cursor already held, so an answer from an older host cannot look
 * like the end of the history.
 */
export function mergeRunPage(state, page) {
  const seen = new Set(state.runs.map((run) => run.runId));
  const runs = state.runs.slice();
  for (const run of page?.runs ?? []) {
    if (!run || typeof run.runId !== 'string' || seen.has(run.runId)) continue;
    seen.add(run.runId);
    runs.push(run);
  }
  return {
    runs,
    cursor: page && 'nextCursor' in page ? page.nextCursor ?? null : state.cursor,
    hasMore: page && 'hasMore' in page ? Boolean(page.hasMore) : state.hasMore,
  };
}

/**
 * Merge follow-up turns into one entry per research, over the runs loaded so far.
 *
 * A run no loaded run names as its parent is that chain's newest loaded turn,
 * and is what the sidebar draws. The entry is titled by the chain's first turn
 * (the root's prompt) — the same rule the un-paged list used, so a research does
 * not change its name as more pages load. When the page boundary cut the chain,
 * the oldest loaded turn stands in for the root and its missing parent is
 * reported in `unresolvedParents` so the owner can fetch the rest.
 *
 * Report rows keep pointing at the chain's newest turn, so a report opened from
 * the history surface lands on the conversation it belongs to.
 */
export function groupRunChains(runs) {
  const present = runs.filter((run) => run && typeof run.runId === 'string');
  const byId = new Map(present.map((run) => [run.runId, run]));
  //: Run ids some loaded run names as its parent — never top-level rows.
  const parents = new Set(present.map((run) => run.parentRunId).filter(Boolean));
  const nextByParent = new Map(
    present.filter((run) => run.parentRunId).map((run) => [run.parentRunId, run.runId]),
  );

  const climb = (start) => {
    const seen = new Set();
    let node = start;
    while (node.parentRunId && byId.has(node.parentRunId) && !seen.has(node.runId)) {
      seen.add(node.runId);
      node = byId.get(node.parentRunId);
    }
    return node;
  };
  const descend = (start) => {
    const seen = new Set();
    let leaf = start.runId;
    while (nextByParent.has(leaf) && !seen.has(leaf)) {
      seen.add(leaf);
      leaf = nextByParent.get(leaf);
    }
    return leaf;
  };

  const rows = present
    .filter((run) => !parents.has(run.runId))
    .map((run) => {
      const root = climb(run);
      return {
        run,
        title: root.prompt,
        unresolvedParentId:
          root.parentRunId && !byId.has(root.parentRunId) ? root.parentRunId : null,
      };
    });
  const reports = present
    .filter((run) => run.hasReport)
    .map((run) => ({ run, latestRunId: descend(run) }));
  return {
    rows,
    reports,
    unresolvedParents: [...new Set(rows.map((row) => row.unresolvedParentId).filter(Boolean))],
  };
}
