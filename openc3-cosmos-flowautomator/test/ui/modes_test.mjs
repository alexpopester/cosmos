// Live test of crawl / walk / run modes: engine API with real COSMOS hazard checks, then the approval UI.
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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
  const res = await page.request.fetch(`${BASE}/flow-automator/api${path}${path.includes('?') ? '&' : '?'}scope=DEFAULT`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
  })
  return [res.status(), await res.json()]
}
const rpc = async (method, ...params) => {
  const res = await page.request.post(`${BASE}/openc3-api/api?scope=DEFAULT`, {
    headers: { Authorization: token, 'Content-Type': 'application/json-rpc' },
    data: JSON.stringify({ jsonrpc: '2.0', method, params, id: 1, keyword_params: { scope: 'DEFAULT' } }),
  })
  return (await res.json()).result
}
const approvalsFor = async (runId) => {
  const [, status] = await api('GET', '/status')
  return status.approvals.filter((a) => a.run_id === runId)
}
const waitApproval = async (runId, timeout = 10000) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const list = await approvalsFor(runId)
    if (list.length) return list[0]
    await sleep(150)
  }
  return null
}
const waitRun = async (runId) => {
  for (let i = 0; i < 200; i++) {
    const [, run] = await api('GET', `/runs/${runId}`)
    if (!['pending', 'running'].includes(run.status)) return run
    await sleep(150)
  }
  throw new Error('run timed out')
}
const created = []
const make = async (name, nodes, edges, extra = {}) => {
  const [, body] = await api('POST', '/flows', { name, nodes, edges, ...extra })
  if (body.problems.length) throw new Error(JSON.stringify(body.problems))
  created.push(body.flow.id)
  return body.flow.id
}

// This test isn't about alerts (history_test.mjs is): keep COSMOS alert toasts from covering the toolbar
const [, savedAlerts] = await api('GET', '/alerts')
await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })
await page.waitForTimeout(1500)
await ackToasts(page)

try {
  // ---- engine with real COSMOS commands ----
  const cmdFlow = (mode, extra = {}) => [
    [
      n('t', 'trigger_manual', 0, 0),
      n('a', 'send_cmd', 300, 0, { command: 'INST COLLECT with TYPE NORMAL, DURATION 0.1' }),
      n('b', 'send_cmd', 600, 0, { command: 'INST COLLECT with TYPE SPECIAL, DURATION 0.1' }), // SPECIAL is a hazardous state
      n('c', 'send_cmd', 900, 0, { command: 'INST CLEAR', ...extra }), // hazardous command
    ],
    [e('t', 'a'), e('a', 'b'), e('b', 'c')],
    { mode },
  ]

  // run mode: blocked at the first hazardous command
  let id = await make('Modes run', ...cmdFlow('run'))
  let [, started] = await api('POST', `/flows/${id}/run`, {})
  let run = await waitRun(started.run.id)
  check('run mode blocks hazardous TYPE SPECIAL', run.status === 'failed' && run.message.includes('Hazardous'), run.message)

  // walk mode: only the two hazardous commands ask; approving sends them
  const before = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  id = await make('Modes walk', ...cmdFlow('walk'))
  ;[, started] = await api('POST', `/flows/${id}/run`, {})
  let approval = await waitApproval(started.run.id)
  check('walk skips safe command, asks at hazardous state', approval?.node === 'b' && approval.reason.includes('HAZARDOUS'), approval?.preview)
  await api('POST', `/runs/${started.run.id}/approve`, { approval_id: approval.id, decision: 'go' })
  approval = await waitApproval(started.run.id)
  check('walk asks at hazardous command', approval?.node === 'c', approval?.preview)
  await api('POST', `/runs/${started.run.id}/approve`, { approval_id: approval.id, decision: 'go' })
  run = await waitRun(started.run.id)
  const after = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  const type = await rpc('tlm', 'INST HEALTH_STATUS COLLECT_TYPE')
  check('approved hazardous commands were really sent', run.status === 'success', `COLLECTS ${before} -> ${after} (CLEAR resets), last type ${type}`)

  // crawl mode with per-run override, abort at second step
  ;[, started] = await api('POST', `/flows/${id}/run`, { mode: 'crawl' })
  approval = await waitApproval(started.run.id)
  check('crawl asks at the first (safe) step', approval?.node === 'a' && approval.reason.includes('crawl'), approval?.preview)
  await api('POST', `/runs/${started.run.id}/approve`, { approval_id: approval.id, decision: 'go' })
  approval = await waitApproval(started.run.id)
  await api('POST', `/runs/${started.run.id}/approve`, { approval_id: approval.id, decision: 'abort' })
  run = await waitRun(started.run.id)
  check('abort stops a crawl run', run.status === 'stopped' && run.message.includes('Aborted'), run.message)

  // ---- UI: crawl run approved from the approval panel ----
  const uiId = await make(
    'Modes UI crawl',
    [
      n('t', 'trigger_manual', 0, 100),
      n('w', 'wait_time', 300, 100, { seconds: 0.2, label: 'Settle' }),
      n('c', 'send_cmd', 600, 100, { command: 'INST COLLECT with TYPE NORMAL, DURATION 0.1' }),
      n('h', 'send_cmd', 900, 100, { command: 'INST COLLECT with TYPE SPECIAL, DURATION 0.1', label: 'Special collect' }),
    ],
    [e('t', 'w'), e('w', 'c'), e('c', 'h')],
    { mode: 'crawl' },
  )
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-palette').waitFor({ timeout: 60000 })
  await page.locator('.fa-flow-select input').click()
  await page.locator('.fa-flow-select input').pressSequentially('Modes UI crawl')
  await page.locator('.v-overlay .v-list-item', { hasText: 'Modes UI crawl' }).click()
  await page.locator('.vue-flow__node').nth(3).waitFor()
  const crawlActive = await page.locator('.fa-mode-toggle .v-btn--active').innerText()
  check('toolbar shows the flow mode', crawlActive.includes('Crawl'), crawlActive)

  await ackToasts(page)
  await page.getByRole('button', { name: 'Run now' }).click()
  await page.locator('.fa-approval').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)
  const firstCard = await page.locator('.fa-approval').first().innerText()
  const waitingNodes = await page.locator('.fa-node.fa-status-waiting').count()
  const chip = await page.locator('.fa-approvals-chip').innerText().catch(() => '')
  check('approval panel appears at first step', firstCard.includes('Settle') && firstCard.includes('Wait 0.2s'), firstCard.split('\n')[0])
  check('waiting node pulses on the canvas', waitingNodes === 1, `${waitingNodes} waiting`)
  check('toolbar shows approvals waiting', chip.includes('1 awaiting approval'), chip)
  const engineChip = await page.locator('.fa-toolbar .v-chip', { hasText: /armed|offline/ }).innerText()
  check('engine stays online while approvals are pending', !engineChip.includes('offline'), engineChip)
  await page.screenshot({ path: `${SHOTS}/modes_01_waiting.png` })

  await page.locator('.fa-approval .fa-approval-go').first().click()
  await page.locator('.fa-approval', { hasText: 'INST COLLECT with TYPE NORMAL' }).waitFor({ timeout: 10000 })
  check('Go advances to the next step', true)
  await page.locator('.fa-approval .fa-approval-go').first().click()
  await page.locator('.fa-approval-hazardous').waitFor({ timeout: 10000 })
  const hazardText = await page.locator('.fa-approval-hazardous').innerText()
  check('hazardous step gets a hazard-styled card', hazardText.includes('HAZARDOUS') && hazardText.includes('TYPE SPECIAL'), hazardText.split('\n')[0])
  await page.screenshot({ path: `${SHOTS}/modes_02_hazardous.png` })
  // A hazardous Go asks once more, showing exactly what will be sent
  await page.locator('.fa-approval-hazardous .fa-approval-go').first().click()
  const confirmText = await page.locator('.v-dialog', { hasText: 'Send a hazardous command?' }).innerText()
  check('hazardous Go asks to confirm the exact command', confirmText.includes('TYPE SPECIAL'), confirmText.split('\n')[0])
  await page.locator('.fa-confirm-hazardous').click()
  await page.locator('.fa-steps .v-chip:has-text("success")').first().waitFor({ timeout: 15000 })
  const runLog = await page.locator('.fa-steps').innerText()
  check('crawl run completes after approvals', runLog.includes('crawl mode'), '')
  check('approval panel clears', (await page.locator('.fa-approval').count()) === 0)
  await page.screenshot({ path: `${SHOTS}/modes_03_done.png` })

  // Go & run rest from the UI: routine steps stop asking, the hazardous one still asks
  await ackToasts(page)
  await page.getByRole('button', { name: 'Run now' }).click()
  await page.locator('.fa-approval').first().waitFor({ timeout: 10000 })
  await page.locator('.fa-approval button:has-text("run rest")').first().click()
  await page.locator('.fa-approval-hazardous').waitFor({ timeout: 10000 })
  const onlyHazard = await page.locator('.fa-approval').count()
  await page.locator('.fa-approval-hazardous .fa-approval-go').first().click()
  await page.locator('.fa-confirm-hazardous').click()
  await page.locator('.fa-steps', { hasText: 'running the rest' }).waitFor({ timeout: 10000 })
  await page.locator('.fa-steps .v-chip:has-text("success")').first().waitFor({ timeout: 15000 })
  check('Go & run rest skips routine prompts but still asks for hazardous', onlyHazard === 1)

  // Mode toggle + per-node toggle save
  await page.waitForTimeout(1000)
  await ackToasts(page)
  await page.locator('.fa-mode-toggle button', { hasText: 'Walk' }).click()
  await page.locator('.vue-flow__node[data-id="w"] .fa-header').click()
  await page.locator('.fa-inspector .v-switch', { hasText: 'Ask operator first' }).locator('input').check()
  await page.getByRole('button', { name: 'Save' }).click()
  await page.locator('text=unsaved changes').waitFor({ state: 'detached', timeout: 10000 })
  const [, saved] = await api('GET', `/flows/${uiId}`)
  const wNode = saved.flow.nodes.find((x) => x.id === 'w')
  check('mode toggle and Ask-operator switch save', saved.flow.mode === 'walk' && wNode.data.require_approval === true, `${saved.flow.mode}`)
  const markIcons = await page.locator('.fa-node .mdi-hand-back-right-outline').count()
  check('marked node shows the ask-operator icon', markIcons === 1)
  await page.screenshot({ path: `${SHOTS}/modes_04_walk.png` })

  // Run now with unsaved edits asks first instead of saving them silently
  await page.locator('.fa-inspector .v-switch', { hasText: 'Ask operator first' }).locator('input').uncheck()
  await page.getByRole('button', { name: 'Run now' }).click()
  const asked = await page
    .locator('.v-dialog', { hasText: 'Save changes first?' })
    .waitFor({ timeout: 5000 })
    .then(() => true, () => false)
  const offersSaved = await page.locator('.fa-use-saved').isVisible()
  check('Run now with unsaved edits asks to save first', asked && offersSaved)
  await page.locator('.v-dialog button', { hasText: 'Cancel' }).click()
  const [, afterCancel] = await api('GET', `/flows/${uiId}`)
  check('cancelling saves nothing', afterCancel.flow.version === saved.flow.version, `v${afterCancel.flow.version}`)
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await page.screenshot({ path: `${SHOTS}/modes_error.png` }).catch(() => {})
} finally {
  await api('PUT', '/alerts', savedAlerts).catch(() => {})
  for (const id of created) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
