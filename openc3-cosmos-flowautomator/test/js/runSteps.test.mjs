// node --test test/js: the run log's incremental polling (B47)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { firstOpenStep, mergeRunSteps } from '../../src/tools/flowautomator/runSteps.js'

const step = (node, ended = true) => ({ node, ended_at: ended ? 1 : null })

test('asks from the first unfinished step', () => {
  assert.equal(firstOpenStep({ steps_dropped: 0, steps: [step('a'), step('b', false), step('c')] }), 1)
  assert.equal(firstOpenStep({ steps_dropped: 10, steps: [step('a'), step('b')] }), 12)
  assert.equal(firstOpenStep(null), 0)
})

test('splices the new steps in after the finished ones', () => {
  const local = { id: 'r', steps_dropped: 0, steps: [step('a'), step('b', false)] }
  const update = { id: 'r', steps_dropped: 0, steps_from: 1, steps: [step('b'), step('c', false)], status: 'running' }
  const merged = mergeRunSteps(local, update)
  assert.deepEqual(merged.steps.map((s) => s.node), ['a', 'b', 'c'])
  assert.equal(merged.steps[1].ended_at, 1) // the running step got its final state
  assert.equal(merged.steps_from, undefined)
})

test('follows the run when it lets go of its oldest steps', () => {
  const local = { steps_dropped: 0, steps: [step('a'), step('b'), step('c')] }
  const update = { steps_dropped: 2, steps_from: 3, steps: [step('d')] }
  const merged = mergeRunSteps(local, update)
  assert.deepEqual(merged.steps.map((s) => s.node), ['c', 'd'])
  assert.equal(merged.steps_dropped, 2)
})

test('a whole run replaces the local one', () => {
  const whole = { steps_dropped: 0, steps: [step('x')] }
  assert.equal(mergeRunSteps({ steps: [step('a')] }, whole), whole)
})
