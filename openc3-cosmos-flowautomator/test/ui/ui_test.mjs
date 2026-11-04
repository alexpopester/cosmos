// Headless UI test of the Flow Automator tool against the local COSMOS.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const PASSWORD = process.env.COSMOS_PASSWORD
const OUT = SHOTS
fs.mkdirSync(OUT, { recursive: true })

const results = []
const consoleErrors = []
const failedRequests = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png` })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text())
})
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))
page.on('response', (r) => {
  if (r.url().includes('/flow-automator/') && r.status() >= 400) {
    failedRequests.push(`${r.status()} ${r.request().method()} ${r.url()}`)
  }
})

let token = null
const api = async (method, path, body) => {
  const res = await page.request.fetch(`${BASE}/flow-automator/api${path}?scope=DEFAULT`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
  })
  return [res.status(), await res.json()]
}
const created = []

// Leave the COSMOS as we found it: which flows were armed, and whether the contact scheduler ran
let snapshot = null // taken right after login, below
const takeSnapshot = async () => {
  const [, list] = await api('GET', '/flows')
  const [, status] = await api('GET', '/status')
  return { armed: list.filter((f) => f.armed).map((f) => f.id), paused: !!status.contacts?.paused }
}
const restore = async () => {
  if (!snapshot) return
  for (const id of snapshot.armed) await api('POST', `/flows/${id}/arm`, {}).catch(() => {})
  await api('POST', snapshot.paused ? '/scheduler/pause' : '/scheduler/resume', { reason: 'Restored after a test' }).catch(() => {})
}

try {
  // ---- login ----
  await page.goto(`${BASE}/login`)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.locator('button:has-text("Login")').click()
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30000 })
  token = await page.evaluate(() => localStorage.openc3Token)
  snapshot = await takeSnapshot()
  check('login', !!token)

  // ---- tool loads ----
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-palette').waitFor({ timeout: 60000 })
  await page.locator('text=Engine offline').waitFor({ state: 'detached', timeout: 10000 }).catch(() => {})
  const engineChip = await page.locator('.fa-toolbar .v-chip').first().innerText()
  check('tool loads with engine online', engineChip.includes('armed'), engineChip.replace(/\s+/g, ' '))
  const paletteCount = await page.locator('.fa-palette-item').count()
  check('palette shows all 26 node types', paletteCount === 26, `${paletteCount}`)
  await shot(page, '01_empty')

  // ---- build a flow in the editor ----
  await page.getByRole('button', { name: 'New' }).click()
  await page.locator('.vue-flow__node').first().waitFor()
  check('New flow starts with a Manual trigger', (await page.locator('.vue-flow__node').count()) === 1)

  const canvas = page.locator('.fa-canvas')
  const box = await canvas.boundingBox()
  await page
    .locator('.fa-palette-item', { hasText: 'Send command' })
    .dragTo(canvas, { targetPosition: { x: box.width * 0.55, y: box.height * 0.35 } })
  await page
    .locator('.fa-palette-item', { hasText: 'Wait for telemetry' })
    .dragTo(canvas, { targetPosition: { x: box.width * 0.55, y: box.height * 0.75 } })
  const nodeCount = await page.locator('.vue-flow__node').count()
  check('drag nodes from palette onto canvas', nodeCount === 3, `${nodeCount} nodes`)

  // The last dropped node (wait_tlm) is selected: set its condition in the inspector
  const cond = page.locator('.fa-inspector textarea').first()
  await cond.fill('INST HEALTH_STATUS COLLECTS > start')
  await page.locator('.fa-inspector input[type=number]').first().fill('15')

  // Wire: trigger.out -> send_cmd.in, send_cmd.out -> wait_tlm.in (drag between handles)
  const connect = async (fromNode, handle, toNode) => {
    const src = page.locator(`.vue-flow__node[data-id^="${fromNode}"] .vue-flow__handle[data-handleid="${handle}"]`)
    const dst = page.locator(`.vue-flow__node[data-id^="${toNode}"] .vue-flow__handle[data-handleid="in"]`)
    const a = await src.boundingBox()
    const b = await dst.boundingBox()
    await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2)
    await page.mouse.down()
    await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 5 })
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 })
    await page.mouse.up()
  }
  await connect('trigger_manual', 'out', 'send_cmd')
  await connect('send_cmd', 'out', 'wait_tlm')
  const edgeCount = await page.locator('.vue-flow__edge').count()
  check('wire nodes by dragging between handles', edgeCount === 2, `${edgeCount} edges`)

  // Select send_cmd and set its command
  await page.locator('.vue-flow__node[data-id^="send_cmd"] .fa-header').click()
  await page.locator('.fa-inspector textarea').first().fill('INST COLLECT with TYPE NORMAL, DURATION 1')

  // Flow settings: click the pane, rename, add a starting variable
  await page.locator('.vue-flow__pane').click({ position: { x: 30, y: 30 } })
  await page.locator('.fa-inspector input').first().fill('UI test flow')
  await page.locator('.fa-inspector button:has-text("Add")').click()
  const kv = page.locator('.fa-inspector .fa-mono input')
  await kv.nth(0).fill('start')
  const collects = await (
    await page.request.post(`${BASE}/openc3-api/api?scope=DEFAULT`, {
      headers: { Authorization: token, 'Content-Type': 'application/json-rpc' },
      data: JSON.stringify({ jsonrpc: '2.0', method: 'tlm', params: ['INST HEALTH_STATUS COLLECTS'], id: 1, keyword_params: { scope: 'DEFAULT' } }),
    })
  ).json()
  await kv.nth(1).fill(String(collects.result))
  await page.waitForTimeout(800) // debounced validation
  const problemsBadge = await page.locator('.v-tab:has-text("Problems") .v-badge').count()
  check('validation shows no problems for a complete flow', problemsBadge === 0)
  await shot(page, '02_built')

  // Save
  await page.getByRole('button', { name: 'Save' }).click()
  await page.locator('text=unsaved changes').waitFor({ state: 'detached', timeout: 10000 })
  const [, flows] = await api('GET', '/flows')
  const saved = flows.find((f) => f.name === 'UI test flow')
  if (saved) created.push(saved.id)
  check('save flow to engine', !!saved && saved.node_count === 3, saved ? `${saved.id}` : 'not found')

  // Run now and watch the run log
  await page.getByRole('button', { name: 'Run now' }).click()
  await page.locator('.fa-run-panel .v-chip:has-text("success"), .fa-run-panel .v-chip:has-text("failed")').first().waitFor({ timeout: 30000 })
  const runStatus = await page.locator('.fa-steps .v-chip').first().innerText()
  const stepRows = await page.locator('.fa-steps-table tbody tr').count()
  check('Run now executes and shows run log', runStatus.includes('success') && stepRows === 3, `${runStatus} ${stepRows} steps`)
  const doneNodes = await page.locator('.fa-node.fa-status-done').count()
  check('nodes highlighted with step status', doneNodes === 3, `${doneNodes} done`)
  await shot(page, '03_run_done')

  // Arm / disarm from the toolbar switch
  await page.locator('.fa-toolbar .v-switch input').click()
  await page.locator('.fa-toolbar span.text-body-2', { hasText: /^Armed$/ }).waitFor({ timeout: 10000 })
  let [, f] = await api('GET', `/flows/${saved.id}`)
  check('arm switch arms the flow', f.flow.armed === true)
  await page.locator('.fa-toolbar .v-switch input').click()
  await page.locator('.fa-toolbar span.text-body-2', { hasText: /^Disarmed$/ }).waitFor({ timeout: 10000 })

  // Invalid edit shows a problem on the node and in the Problems tab
  await page.locator('.vue-flow__node[data-id^="wait_tlm"] .fa-header').click()
  await page.locator('.fa-inspector textarea').first().fill('INST HEALTH_STATUS COLLECTS >')
  await page.waitForTimeout(900)
  const problemNode = await page.locator('.fa-node.fa-has-problem').count()
  check('invalid condition flagged on node', problemNode === 1)
  await page.locator('.v-tab:has-text("Problems")').click()
  const problemText = await page.locator('.fa-run-panel .v-list-item').first().innerText().catch(() => '')
  check('Problems tab lists it', problemText.includes('condition'), problemText.replace(/\s+/g, ' ').slice(0, 100))
  await shot(page, '04_problem')
  await page.locator('.fa-inspector textarea').first().fill('INST HEALTH_STATUS COLLECTS > start')
  await page.getByRole('button', { name: 'Save' }).click()
  await page.locator('text=unsaved changes').waitFor({ state: 'detached', timeout: 10000 })

  // ---- a richer flow made through the API, opened in the editor while it runs ----
  const n = (id, type, x, y, data = {}) => ({ id, type, position: { x, y }, data })
  const e = (s, t, h = 'out') => ({ id: `${s}-${h}-${t}`, source: s, sourceHandle: h, target: t })
  const [, child] = await api('POST', '/flows', {
    name: 'UI child',
    nodes: [n('t', 'trigger_manual', 0, 0), n('s', 'set_var', 300, 0, { name: 'doubled', expression: 'x * 2' })],
    edges: [e('t', 's')],
  })
  created.push(child.flow.id)
  const [, rich] = await api('POST', '/flows', {
    name: 'UI showcase',
    variables: { x: 21 },
    nodes: [
      n('t', 'trigger_manual', 0, 200),
      n('tlm', 'trigger_tlm', 0, 420, { condition: 'INST HEALTH_STATUS TEMP1 > 50' }),
      n('p', 'parallel', 280, 200),
      n('loop', 'loop', 560, 60, { mode: 'count', count: 3, variable: 'i' }),
      n('w', 'wait_time', 860, 0, { seconds: 1, label: 'Pause' }),
      n('sub', 'subflow', 560, 360, { flow_id: child.flow.id }),
      n('j', 'join', 860, 300),
      n('sw', 'switch', 1120, 300, { expression: 'INST HEALTH_STATUS COLLECT_TYPE', cases: ["'NORMAL'", "'SPECIAL'"] }),
      n('log', 'log', 1420, 200, { message: 'doubled={{ doubled }}' }),
      n('end', 'end', 1420, 420, { status: 'failed', message: 'unexpected type' }),
    ],
    edges: [
      e('t', 'p'), e('tlm', 'p'), e('p', 'loop'), e('p', 'sub'), e('loop', 'w', 'body'), e('w', 'loop'),
      e('loop', 'j', 'done'), e('sub', 'j'), e('j', 'sw'), e('sw', 'log', 'case_0'), e('sw', 'log', 'case_1'),
      e('sw', 'end', 'default'),
    ],
  })
  created.push(rich.flow.id)
  check('rich flow validates', rich.problems.length === 0, JSON.stringify(rich.problems))
  await page.locator('.fa-flow-select input').click()
    await page.locator('.fa-flow-select input').pressSequentially('UI showcase')
  await page.locator('.v-overlay .v-list-item', { hasText: 'UI showcase' }).click()
  await page.locator('.vue-flow__node').nth(9).waitFor()
  check('open flow from selector', (await page.locator('.vue-flow__node').count()) === 10)
  await page.getByRole('button', { name: 'Run now' }).click() // two triggers: menu
  await page.locator('.v-overlay .v-list-item', { hasText: 'Manual' }).click()
  await page.locator('.fa-node.fa-status-running').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(700)
  const running = await page.locator('.fa-node.fa-status-running').count()
  check('live highlighting of running node', running >= 1, `${running} running`)
  await shot(page, '05_live_run')
  await page.locator('.fa-steps .v-chip:has-text("success"), .fa-steps .v-chip:has-text("failed")').first().waitFor({ timeout: 30000 })
  const richStatus = await page.locator('.fa-steps .v-chip').first().innerText()
  const vars = await page.locator('.fa-vars pre').innerText()
  check('showcase run succeeds via sub-flow + parallel', richStatus.includes('success') && vars.includes('"doubled": 42'), richStatus)
  await shot(page, '06_live_done')

  // Runs tab history
  await page.locator('.v-tab:has-text("Runs")').click()
  const runRows = await page.locator('.fa-run-panel tbody tr').count()
  check('Runs tab lists run history', runRows >= 1, `${runRows} rows`)

  // STOP ALL dialog
  await api('POST', `/flows/${rich.flow.id}/arm`, {})
  await page.getByRole('button', { name: 'Stop all' }).click()
  await page.locator('.v-dialog button:has-text("Stop all")').click()
  await page.waitForTimeout(1000)
  ;[, f] = await api('GET', `/flows/${rich.flow.id}`)
  check('STOP ALL from UI disarms', f.flow.armed === false)
  await shot(page, '07_stop_all')
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await shot(page, 'zz_error').catch(() => {})
} finally {
  await restore()
  for (const id of created) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}

const relevant = consoleErrors.filter((m) => !/favicon|ERR_ABORTED/.test(m))
console.log(`\nConsole errors (${relevant.length}):`)
relevant.slice(0, 20).forEach((m) => console.log('  ' + m.slice(0, 300)))
console.log(`Failed flow-automator requests (${failedRequests.length}):`)
failedRequests.forEach((m) => console.log('  ' + m))
const failed = results.filter(([, ok]) => !ok).length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
