// A polling run log asks only for the steps it doesn't have yet (B47): from its first unfinished step on
// (a running step still changes), then splices them in. Steps are numbered steps_dropped + position.

// The sequence number of the first step that hasn't finished (or one past the last step)
export function firstOpenStep(run) {
  const steps = run?.steps || []
  const dropped = run?.steps_dropped || 0
  const open = steps.findIndex((step) => !step.ended_at)
  return dropped + (open === -1 ? steps.length : open)
}

// The local run with an update (from GET /runs/<id>?since=) spliced in. A whole run (no steps_from)
// replaces it.
export function mergeRunSteps(local, update) {
  if (update?.steps_from === undefined || update?.steps_from === null) {
    return update
  }
  const localDropped = local?.steps_dropped || 0
  const kept = (local?.steps || []).slice(
    0,
    Math.max(0, update.steps_from - localDropped),
  )
  let steps = kept.concat(update.steps || [])
  let dropped = localDropped
  if ((update.steps_dropped || 0) > dropped) {
    // The run let go of its oldest steps (it keeps the last 1,000): so does the log
    steps = steps.slice(update.steps_dropped - dropped)
    dropped = update.steps_dropped
  }
  const merged = { ...update, steps, steps_dropped: dropped }
  delete merged.steps_from
  return merged
}
