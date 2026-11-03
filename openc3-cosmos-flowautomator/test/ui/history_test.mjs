// Version history, save conflicts, dry runs, command value safety, alerts and run history, live.
// Set WEBHOOK_URL to a receiver reachable from the engine; run.sh starts one in the operator container
// and checks what it received after this test.
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const WEBHOOK_URL = process.env.WEBHOOK_URL || ''
const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const n = (id, type, x, y, data = {}) => ({ id, type, position: { x, y }, data })
const e = (s, t, h = 'out') => ({ id: `${s}-${h}-${t}`, source: s, sourceHandle: h, target: t })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const shot = (page, name) => page.screenshot({ path: `${SHOTS}/history_${name}.png` })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1700, height: 1050 } })
const consoleErrors = []
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))
page.on('dialog', (d) => d.accept())
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
const waitRun = async (runId) => {
  for (let i = 0; i < 200; i++) {
    const [, run] = await api('GET', `/runs/${runId}`)
    if (!['pending', 'running'].includes(run.status)) return run
    await sleep(150)
  }
  throw new Error('run timed out')
}
const openFlow = async (name) => {
  await page.locator('.fa-flow-select input').click()
  await page.locator('.fa-flow-select input').pressSequentially(name)
  await page.locator('.v-overlay .v-list-item', { hasText: name }).first().click()
  await page.locator('.vue-flow__node').first().waitFor()
  await page.waitForTimeout(500)
}
const flows = []
let alertsBefore = null

try {
  await api('POST', '/scheduler/resume', {})
  const [, saved] = await api('POST', '/flows', {
    name: 'HIST collect',
    mode: 'run',
    template: true,
    parameters: [{ name: 'kind', type: 'string', default: 'NORMAL' }],
    nodes: [
      n('t', 'trigger_manual', 0, 100),
      n('v', 'set_var', 260, 100, { name: 'temp', expression: 'INST HEALTH_STATUS TEMP1', label: 'Read TEMP1' }),
      n('c', 'send_cmd', 520, 100, { command: 'INST COLLECT with TYPE {{ kind }}, DURATION {{ round(min(max(abs(temp) / 20, 0.1), 5), 2) }}', label: 'Collect' }),
      n('w', 'wait_time', 780, 100, { seconds: 20, label: 'Settle' }),
    ],
    edges: [e('t', 'v'), e('v', 'c'), e('c', 'w')],
  })
  flows.push(saved.flow.id)
  const flowId = saved.flow.id
  check('a new flow is version 1', saved.flow.version === 1, `saved by ${saved.flow.saved_by}`)

  // ---- edit in the UI and save with a note -> v2 ----
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-palette').waitFor({ timeout: 60000 })
  await openFlow('HIST collect')
  check('toolbar shows the version', (await page.locator('.fa-version-chip').innerText()).includes('v1'))
  await page.locator('.vue-flow__node[data-id="w"] .fa-header').click()
  await page.locator('.fa-inspector input[type="number"]').first().fill('2')
  await page.locator('.v-btn', { hasText: 'File' }).first().click()
  await page.locator('.v-overlay .v-list-item', { hasText: 'Save With Note' }).click()
  await page.locator('[data-field="save-note"] textarea').first().fill('Shorter settle time')
  await page.locator('.fa-note-save').click()
  await page.locator('.fa-version-chip', { hasText: 'v2' }).waitFor({ timeout: 10000 })
  check('save with a note makes v2', true)

  // ---- someone else saves v3 through the API; our save conflicts ----
  const [, current] = await api('GET', `/flows/${flowId}`)
  await api('PUT', `/flows/${flowId}`, { ...current.flow, description: 'edited elsewhere', base_version: 2 })
  await page.locator('.vue-flow__node[data-id="w"] .fa-header').click()
  await page.locator('.fa-inspector input[type="number"]').first().fill('3')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.locator('.fa-conflict-dialog').waitFor({ timeout: 10000 })
  const conflictText = await page.locator('.fa-conflict-dialog').innerText()
  check('saving over a newer version asks first', conflictText.includes('version 3') && conflictText.includes('Save mine'), conflictText.split('\n')[1])
  await shot(page, '01_conflict')
  await page.locator('.fa-overwrite').click()
  await page.locator('.fa-version-chip', { hasText: 'v4' }).waitFor({ timeout: 10000 })
  check('Save mine writes v4 (v3 stays in history)', true)

  // ---- history dialog: compare and restore ----
  await page.locator('.fa-version-chip').click()
  await page.locator('.fa-history-version').first().waitFor()
  const listText = await page.locator('.fa-history-list').innerText()
  check(
    'history lists every version with who and the note',
    ['v4', 'v3', 'v2', 'v1', 'Shorter settle time', 'Saved over a newer version'].every((w) => listText.includes(w)),
    listText.split('\n').slice(0, 3).join(' | '),
  )
  await page.locator('.fa-history-version[data-version="1"]').click()
  await page.locator('.fa-diff-node').first().waitFor({ timeout: 5000 })
  const diffText = await page.locator('.fa-history-detail').innerText()
  check('comparing v1 with the current flow shows the changed node', diffText.includes('Settle') && diffText.includes('seconds') && diffText.includes('20'), '')
  await shot(page, '02_history')
  await page.locator('.fa-restore-version').click()
  await page.locator('.fa-version-chip', { hasText: 'v5' }).waitFor({ timeout: 10000 })
  const [, restored] = await api('GET', `/flows/${flowId}`)
  check('restoring v1 saves it as v5', restored.flow.nodes.find((x) => x.id === 'w').data.seconds === 20)

  // ---- alerts: a webhook and COSMOS notifications ----
  const [, alerts] = await api('GET', '/alerts')
  alertsBefore = alerts
  if (WEBHOOK_URL) {
    const [status] = await api('PUT', '/alerts', {
      ...alerts,
      approval_escalate_after: 0, // the test checks the approval alert at once
      webhooks: [{ name: 'Test receiver', url: WEBHOOK_URL, format: 'generic', events: ['approval_waiting', 'run_failed'], include_dry_runs: false }],
    })
    check('alerts saved with a webhook', status === 200)
  }

  // ---- dry run from the UI: commands are checked, nothing is sent ----
  const before = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  await page.locator('.fa-dry-run-btn').click()
  await page.locator('.fa-run-dialog').waitFor()
  check('dry run dialog offers parameters and a contact', (await page.locator('.fa-run-dialog [data-param="kind"]').count()) === 1 && (await page.locator('.fa-run-contact').count()) === 1)
  await page.locator('.fa-run-go').click()
  await page.locator('.fa-steps', { hasText: 'DRY RUN' }).waitFor({ timeout: 10000 })
  await page.locator('.fa-steps .v-chip:has-text("success")').first().waitFor({ timeout: 15000 })
  const dryLog = await page.locator('.fa-steps').innerText()
  const after = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  check('dry run checked the command and sent nothing', dryLog.includes('would send INST COLLECT with TYPE NORMAL, DURATION') && after === before, `COLLECTS ${before} -> ${after}`)
  check('dry run skipped the 20s wait', dryLog.includes('skipped a 20s wait'))
  check('telemetry flowed into the command', /DURATION \d/.test(dryLog) && dryLog.includes('temp ='), dryLog.match(/DURATION [\d.]+/)?.[0])
  await shot(page, '03_dry_run')

  // A bad state is caught by COSMOS's command definition in the dry run
  let [, result] = await api('POST', `/flows/${flowId}/run`, { dry_run: true, skip_waits: true, variables: { kind: 'FAST' } })
  let run = await waitRun(result.run.id)
  check('dry run catches a value COSMOS would reject', run.status === 'failed' && run.message.includes("'FAST' is not one of"), run.message)

  // ---- injection through a parameter is refused ----
  ;[, result] = await api('POST', `/flows/${flowId}/run`, { variables: { kind: 'NORMAL, DURATION 9' } })
  run = await waitRun(result.run.id)
  const afterInjection = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  check('a parameter value cannot add command parameters', run.status === 'failed' && run.message.includes("can't go into a command") && afterInjection === before, run.message.slice(0, 90))

  // A real run with the same flow does send
  ;[, result] = await api('POST', `/flows/${flowId}/run`, {})
  run = await waitRun(result.run.id)
  const afterReal = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  check('a real run sends the command with the telemetry value', run.status === 'success' && afterReal === before + 1, `COLLECTS ${before} -> ${afterReal}; ${run.steps.find((s) => s.node === 'c').message}`)
  check('run records who started it and the version', run.trigger.kind === 'manual' && run.trigger.user === 'anonymous' && run.flow_version === 5)

  // ---- crawl run: approval alert to COSMOS notifications and the webhook ----
  const [, crawl] = await api('POST', '/flows', {
    name: 'HIST crawl',
    mode: 'crawl',
    nodes: [n('t', 'trigger_manual', 0, 0), n('l', 'log', 260, 0, { message: 'needs approval' })],
    edges: [e('t', 'l')],
  })
  flows.push(crawl.flow.id)
  ;[, result] = await api('POST', `/flows/${crawl.flow.id}/run`, {})
  await page.locator('.v-snackbar, .v-overlay, .toast, [role="alert"]', { hasText: "'HIST crawl' is waiting for approval" }).first().waitFor({ timeout: 15000 }).catch(() => {})
  const toastVisible = await page.getByText("'HIST crawl' is waiting for approval").first().isVisible().catch(() => false)
  check('an approval pops up as a COSMOS alert', toastVisible)
  await shot(page, '04_alert')
  await sleep(2000) // webhook delivery is in the background; run.sh checks the receiver
  const [, statusNow] = await api('GET', '/status')
  const approval = statusNow.approvals.find((a) => a.run_id === result.run.id)
  await api('POST', `/runs/${result.run.id}/approve`, { approval_id: approval.id, decision: 'abort' })

  // ---- run history ----
  await page.locator('.v-btn', { hasText: 'View' }).first().click()
  await page.locator('.v-overlay .v-list-item', { hasText: 'Run History' }).click()
  await page.locator('.fa-rh-table tbody tr').first().waitFor()
  await page.waitForTimeout(800)
  await page.locator('.fa-rh-filters .v-select', { hasText: 'Dry runs' }).click()
  await page.locator('.v-overlay .v-list-item', { hasText: 'Only dry runs' }).click()
  await page.waitForTimeout(1200)
  const dryRows = await page.locator('.fa-rh-table tbody tr').allInnerTexts()
  check('run history filters to dry runs', dryRows.length >= 2 && dryRows.every((r) => r.includes('dry run')), `${dryRows.length} rows`)
  await shot(page, '05_run_history')
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await shot(page, 'error').catch(() => {})
} finally {
  if (alertsBefore) await api('PUT', '/alerts', { cosmos: alertsBefore.cosmos, base_url: alertsBefore.base_url, webhooks: [] }).catch(() => {})
  await api('POST', '/stop_all', {}).catch(() => {})
  await api('POST', '/scheduler/resume', {}).catch(() => {})
  for (const id of flows) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
