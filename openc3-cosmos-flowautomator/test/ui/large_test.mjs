// Large-flow stress test: engine API timings plus UI rendering of big graphs.
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const n = (id, type, x, y, data = {}) => ({ id, type, position: { x, y }, data })
const e = (s, t, h = 'out') => ({ id: `${s}-${h}-${t}`, source: s, sourceHandle: h, target: t })

// COSMOS alert toasts (the engine's alerts) sit over the toolbar until acknowledged, like an operator would
const ackToasts = async (page) => {
  for (const button of await page.locator('button:has-text("Acknowledge")').all()) {
    await button.click().catch(() => {})
  }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
const consoleErrors = []
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))

await page.goto(`${BASE}/login`)
await page.getByLabel('Password').fill(process.env.COSMOS_PASSWORD)
await page.locator('button:has-text("Login")').click()
await page.waitForURL((u) => !u.pathname.startsWith('/login'))
const token = await page.evaluate(() => localStorage.openc3Token)

const api = async (method, path, body) => {
  const t0 = performance.now()
  const res = await page.request.fetch(`${BASE}/flow-automator/api${path}${path.includes('?') ? '&' : '?'}scope=DEFAULT`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
    timeout: 120000,
  })
  return [res.status(), await res.json(), performance.now() - t0]
}
const waitRun = async (runId, timeout = 120000) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const [, run] = await api('GET', `/runs/${runId}`)
    if (!['pending', 'running'].includes(run.status)) return run
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error('run timed out')
}
const created = []

// A chain of N set_var nodes laid out in a grid (each adds 1 to a counter)
const chain = (count, name) => {
  const nodes = [n('t', 'trigger_manual', 0, 0)]
  const edges = []
  let prev = 't'
  for (let i = 0; i < count; i++) {
    const id = `s${i}`
    nodes.push(n(id, 'set_var', 300 * ((i + 1) % 40), 160 * Math.floor((i + 1) / 40), { name: 'c', expression: 'c + 1' }))
    edges.push(e(prev, id))
    prev = id
  }
  return { name, variables: { c: 0 }, nodes, edges }
}

// This test isn't about alerts (history_test.mjs is): keep COSMOS alert toasts from covering the toolbar
const [, savedAlerts] = await api('GET', '/alerts')
await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })
await page.waitForTimeout(1500)
await ackToasts(page)

try {
  // ---- engine: big chains ----
  for (const size of [500, 2000]) {
    const [status, body, saveMs] = await api('POST', '/flows', chain(size, `Large chain ${size}`))
    created.push(body.flow.id)
    check(`save ${size}-node flow`, status === 200 && body.problems.length === 0, `${saveMs.toFixed(0)} ms`)
    const [, , validateMs] = await api('POST', '/validate', chain(size, 'x'))
    check(`validate ${size}-node flow`, validateMs < 2000, `${validateMs.toFixed(0)} ms`)
    const t0 = performance.now()
    const [, started] = await api('POST', `/flows/${body.flow.id}/run`, {})
    const run = await waitRun(started.run.id)
    const runMs = run.ended_at && run.started_at ? (run.ended_at - run.started_at) * 1000 : NaN
    check(
      `run ${size}-node chain`,
      run.status === 'success' && run.variables.c === size,
      `engine ${runMs.toFixed(0)} ms, wall ${(performance.now() - t0).toFixed(0)} ms, steps ${run.step_count}, kept ${run.steps.length}, dropped ${run.steps_dropped}`,
    )
  }

  // ---- engine: 100-branch parallel ----
  {
    const nodes = [n('t', 'trigger_manual', 0, 0), n('p', 'parallel', 300, 0), n('j', 'join', 900, 0)]
    const edges = [e('t', 'p')]
    for (let i = 0; i < 100; i++) {
      nodes.push(n(`w${i}`, 'wait_time', 600, i * 120, { seconds: 0.5 }))
      edges.push(e('p', `w${i}`), e(`w${i}`, 'j'))
    }
    nodes.push(n('done', 'set_var', 1200, 0, { name: 'joined', expression: 'True' }))
    edges.push(e('j', 'done'))
    const [, body] = await api('POST', '/flows', { name: 'Parallel 100', nodes, edges })
    created.push(body.flow.id)
    const [, started] = await api('POST', `/flows/${body.flow.id}/run`, {})
    const run = await waitRun(started.run.id)
    const runMs = (run.ended_at - run.started_at) * 1000
    check('100 parallel branches of 0.5 s wait', run.status === 'success' && runMs < 3000, `${runMs.toFixed(0)} ms`)
  }

  // ---- engine: long loop (step cap) ----
  {
    const [, body] = await api('POST', '/flows', {
      name: 'Loop 5000',
      nodes: [n('t', 'trigger_manual', 0, 0), n('l', 'loop', 300, 0, { mode: 'count', count: 5000 }), n('s', 'set_var', 600, 0, { name: 'c', expression: 'c + i' })],
      edges: [e('t', 'l'), e('l', 's', 'body'), e('s', 'l')],
      variables: { c: 0 },
    })
    created.push(body.flow.id)
    const [, started] = await api('POST', `/flows/${body.flow.id}/run`, {})
    const run = await waitRun(started.run.id)
    const runMs = (run.ended_at - run.started_at) * 1000
    const [, , getMs] = await api('GET', `/runs/${run.id}`)
    check(
      '5000-iteration loop',
      run.status === 'success' && run.variables.c === (4999 * 5000) / 2 && run.steps.length === 1000,
      `${runMs.toFixed(0)} ms, ${run.step_count} steps, log kept ${run.steps.length}, GET run ${getMs.toFixed(0)} ms`,
    )
  }

  // ---- engine: 200 armed telemetry triggers ----
  {
    const ids = []
    for (let i = 0; i < 200; i++) {
      const [, body] = await api('POST', '/flows', {
        name: `Armed ${i}`,
        nodes: [
          n('tr', 'trigger_tlm', 0, 0, { condition: `INST HEALTH_STATUS TEMP${(i % 4) + 1} > 1000 + ${i}` }),
          n('l', 'log', 300, 0, { message: 'never' }),
        ],
        edges: [e('tr', 'l')],
      })
      ids.push(body.flow.id)
      await api('POST', `/flows/${body.flow.id}/arm`, {})
    }
    created.push(...ids)
    // Watch last_tick advance: ticks should keep up with the 0.25 s period
    const ticks = []
    for (let i = 0; i < 12; i++) {
      const [, status] = await api('GET', '/status')
      ticks.push(status.triggers.last_tick)
      await new Promise((r) => setTimeout(r, 250))
    }
    const [, status] = await api('GET', '/status')
    const unique = new Set(ticks).size
    const span = ticks[ticks.length - 1] - ticks[0]
    check(
      '200 armed telemetry triggers keep polling',
      status.triggers.watching === 200 && !status.triggers.error && span > 2 && unique >= 8,
      `watching ${status.triggers.watching}, ${unique} distinct ticks over ${span.toFixed(2)} s, error ${status.triggers.error}`,
    )
    const [, , listMs] = await api('GET', '/flows')
    check('list 200+ flows', listMs < 3000, `${listMs.toFixed(0)} ms`)
    const t0 = performance.now()
    const [, stop] = await api('POST', '/stop_all', {})
    await page.waitForTimeout(1500)
    await ackToasts(page)
    check('STOP ALL with 200 armed', stop.disarmed === 200, `${(performance.now() - t0).toFixed(0)} ms`)
  }

  // ---- UI: open the big flows ----
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-palette').waitFor({ timeout: 60000 })
  for (const size of [500, 2000]) {
    const t0 = performance.now()
    await page.locator('.fa-flow-select input').click()
    await page.locator('.fa-flow-select input').pressSequentially(`Large chain ${size}`)
    await page.locator('.v-overlay .v-list-item', { hasText: `Large chain ${size}` }).click()
    await page.waitForFunction((count) => document.querySelectorAll('.vue-flow__node').length >= count, size + 1, {
      timeout: 120000,
    })
    const openMs = performance.now() - t0
    await page.screenshot({ path: `${SHOTS}/large_${size}.png` })
    // Interaction: drag a node, then edit a field, timing each
    const t1 = performance.now()
    const node = page.locator('.vue-flow__node[data-id="t"] .fa-header')
    const b = await node.boundingBox()
    await page.mouse.move(b.x + 20, b.y + 5)
    await page.mouse.down()
    await page.mouse.move(b.x + 120, b.y + 60, { steps: 10 })
    await page.mouse.up()
    const dragMs = performance.now() - t1
    const t2 = performance.now()
    await page.locator('.vue-flow__pane').click({ position: { x: 5, y: 5 } })
    await page.locator('.fa-inspector input').first().fill(`Large chain ${size} edited`)
    const editMs = performance.now() - t2
    const t3 = performance.now()
    await page.getByRole('button', { name: 'Save' }).click()
    await page.locator('text=unsaved changes').waitFor({ state: 'detached', timeout: 60000 })
    const saveMs = performance.now() - t3
    check(
      `UI open/drag/edit/save ${size}-node flow`,
      openMs < 15000 && dragMs < 5000 && saveMs < 10000,
      `open ${openMs.toFixed(0)} ms, drag ${dragMs.toFixed(0)} ms, edit ${editMs.toFixed(0)} ms, save ${saveMs.toFixed(0)} ms`,
    )
    // Run from the UI and wait for the run log to show success
    const t4 = performance.now()
    await ackToasts(page)
  await page.getByRole('button', { name: 'Run now' }).click()
    await page.locator('.fa-steps .v-chip:has-text("success")').first().waitFor({ timeout: 60000 })
    const done = await page.locator('.fa-node.fa-status-done').count()
    check(`UI run + highlight ${size}-node flow`, done >= Math.min(size, 999), `${(performance.now() - t4).toFixed(0)} ms, ${done} nodes highlighted`)
  }
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await page.screenshot({ path: `${SHOTS}/large_error.png` }).catch(() => {})
} finally {
  await api('PUT', '/alerts', savedAlerts).catch(() => {})
  await api('POST', '/stop_all', {}).catch(() => {})
  for (const id of created) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
