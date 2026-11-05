// Live test of the editor fixes from docs/EVALUATION-2026-11-05.md:
//   B13a removing a Switch case keeps the other cases' wires on their own targets
//   B13b starting variables keep what is typed (text, decimals, negatives)
//   B14  the Armed switch follows the engine, and a failed disarm still shows Armed
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
const created = []
const make = async (name, nodes, edges, extra = {}) => {
  const [, body] = await api('POST', '/flows', { name, nodes, edges, ...extra })
  if (body.problems.length) throw new Error(JSON.stringify(body.problems))
  created.push(body.flow.id)
  return body.flow.id
}
const [, savedAlerts] = await api('GET', '/alerts')
await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })

const open = async (id, nodes) => {
  await page.goto(`${BASE}/tools/flowautomator?flow=${id}`)
  await page.locator('.vue-flow__node').nth(nodes - 1).waitFor({ timeout: 30000 })
  const edit = page.locator('.fa-edit-toggle button:has-text("Edit")')
  if (await edit.count()) await edit.click()
}
const save = async () => {
  await page.getByRole('button', { name: 'Save' }).click()
  await page.locator('text=unsaved changes').waitFor({ state: 'detached', timeout: 10000 })
}

try {
  // ---- B13a: removing the middle Switch case ----
  const swId = await make(
    'Editor test switch',
    [
      n('t', 'trigger_manual', 0, 200),
      n('sw', 'switch', 300, 200, { expression: "'IDLE'", cases: ["'SAFE'", "'BURN'", "'IDLE'"] }),
      n('safe', 'log', 650, 50, { message: 'safe' }),
      n('burn', 'log', 650, 200, { message: 'burn' }),
      n('idle', 'log', 650, 350, { message: 'idle' }),
    ],
    [e('t', 'sw'), e('sw', 'safe', 'case_0'), e('sw', 'burn', 'case_1'), e('sw', 'idle', 'case_2')],
  )
  await open(swId, 5)
  await page.locator('.vue-flow__node[data-id="sw"] .fa-header').click()
  const caseRows = page.locator('.fa-inspector .d-flex:has(> .v-text-field.fa-mono)')
  await caseRows.nth(1).locator('button').click() // remove 'BURN'
  await page.waitForTimeout(500)
  await save()
  let [, got] = await api('GET', `/flows/${swId}`)
  const wires = Object.fromEntries(
    got.flow.edges.filter((x) => x.source === 'sw').map((x) => [x.sourceHandle, x.target]),
  )
  check(
    'removing a Switch case keeps the other cases on their own targets',
    JSON.stringify(got.flow.nodes.find((x) => x.id === 'sw').data.cases) === JSON.stringify(["'SAFE'", "'IDLE'"]) &&
      wires.case_0 === 'safe' &&
      wires.case_1 === 'idle' &&
      !Object.values(wires).includes('burn'),
    JSON.stringify(wires),
  )

  // ---- B13b: typing starting variables ----
  const varsId = await make('Editor test variables', [n('t', 'trigger_manual', 0, 0), n('l', 'log', 300, 0, { message: 'x' })], [e('t', 'l')])
  await open(varsId, 2)
  await page.locator('.vue-flow__pane').click({ position: { x: 30, y: 30 } })
  const add = page.locator('.fa-inspector button:has-text("Add")')
  const kv = page.locator('.fa-inspector .fa-mono input')
  const typed = [
    ['name', "'hello'"],
    ['n', '1.5'],
    ['m', '-3'],
    ['flag', 'True'],
  ]
  for (let i = 0; i < typed.length; i++) {
    await add.click()
    // Value first on one row: it used to be dropped until the row had a name
    if (i === 1) {
      await kv.nth(i * 2 + 1).pressSequentially(typed[i][1], { delay: 40 })
      await kv.nth(i * 2).pressSequentially(typed[i][0], { delay: 40 })
    } else {
      await kv.nth(i * 2).pressSequentially(typed[i][0], { delay: 40 })
      await kv.nth(i * 2 + 1).pressSequentially(typed[i][1], { delay: 40 })
    }
  }
  const shown = []
  for (let i = 0; i < typed.length * 2; i++) shown.push(await kv.nth(i).inputValue())
  check('variable inputs still show what was typed', JSON.stringify(shown) === JSON.stringify(typed.flat()), JSON.stringify(shown))
  await save()
  ;[, got] = await api('GET', `/flows/${varsId}`)
  const vars = got.flow.variables
  check(
    'typed variables are saved with their types',
    vars.name === 'hello' && vars.n === 1.5 && vars.m === -3 && vars.flag === true,
    JSON.stringify(vars),
  )

  // ---- B14: Armed switch follows the engine; a failed disarm still shows Armed ----
  const armLabel = (text) => page.locator('.fa-toolbar span.text-body-2', { hasText: new RegExp(`^${text}$`) })
  await api('POST', `/flows/${varsId}/arm`, {})
  await armLabel('Armed').waitFor({ timeout: 5000 }).then(
    () => check('switch shows Armed after another client arms the flow', true),
    () => check('switch shows Armed after another client arms the flow', false),
  )
  await api('POST', `/flows/${varsId}/disarm`, {})
  await armLabel('Disarmed').waitFor({ timeout: 5000 }).then(
    () => check('switch shows Disarmed after another client disarms the flow', true),
    () => check('switch shows Disarmed after another client disarms the flow', false),
  )
  await api('POST', `/flows/${varsId}/arm`, {})
  await armLabel('Armed').waitFor({ timeout: 5000 })
  await page.route('**/flow-automator/api/flows/*/disarm*', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"Injected failure"}' }),
  )
  await page.locator('.fa-toolbar .v-switch input').click()
  await page.waitForTimeout(1500)
  const stillArmed = await armLabel('Armed').count()
  const toast = await page.locator('text=Still armed').count()
  check('a failed disarm still shows Armed, with a warning', stillArmed === 1 && toast > 0, `armed=${stillArmed} toast=${toast}`)
  await page.unroute('**/flow-automator/api/flows/*/disarm*')
  await api('POST', `/flows/${varsId}/disarm`, {})
  await page.screenshot({ path: `${SHOTS}/editor_fixes.png` })

  // ---- B36: "Run vN" asks once, and the run dialog describes vN, not the draft ----
  const tplId = await make(
    'Editor test template',
    [n('t', 'trigger_manual', 0, 0), n('l', 'log', 300, 0, { message: 'rate {{ rate }}' })],
    [e('t', 'l')],
    { template: true, parameters: [{ name: 'rate', type: 'number', default: 2 }] },
  )
  await open(tplId, 2)
  await page.locator('.vue-flow__node[data-id="l"] .fa-header').click()
  await page.locator('.fa-inspector textarea').first().fill('changed in the draft')
  let prompts = 0
  const countPrompt = () => page.locator('.v-dialog', { hasText: 'Save changes first?' }).count()
  await page.getByRole('button', { name: 'Run now' }).click()
  await page.locator('.v-dialog', { hasText: 'Save changes first?' }).waitFor({ timeout: 5000 })
  prompts += 1
  await page.locator('.fa-use-saved').click()
  await page.locator('.fa-run-dialog').waitFor({ timeout: 5000 })
  const previousNote = await page.locator('.fa-run-previous').count()
  await page.locator('.fa-run-dialog .fa-run-dry input').uncheck().catch(() => {})
  await page.locator('.fa-run-go').click()
  await page.waitForTimeout(1000)
  prompts += await countPrompt()
  const [, runs] = await api('GET', `/runs?flow_id=${tplId}`)
  check(
    '"Run vN" asks once and runs the saved version',
    prompts === 1 && previousNote === 1 && runs.length === 1 && runs[0].flow_version === 1,
    `prompts=${prompts} note=${previousNote} runs=${runs.length} v${runs[0]?.flow_version}`,
  )

  // ---- B39: leaving for another COSMOS tool with unsaved edits asks first ----
  const dialogs = []
  const onDialog = (dialog) => {
    dialogs.push(dialog.message())
    dialog.dismiss()
  }
  page.on('dialog', onDialog)
  await page.evaluate(() => window.history.pushState({}, '', '/tools/cmdsender'))
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent('popstate')))
  await page.waitForTimeout(1500)
  page.off('dialog', onDialog)
  check(
    'leaving for another tool with unsaved edits asks first, and Cancel stays',
    dialogs.some((m) => m.includes("aren't saved")) && (await page.locator('.fa-palette').count()) === 1,
    JSON.stringify(dialogs),
  )

  // ---- B38: a hung engine shows "not answering" and locks approval cards ----
  const askId = await make(
    'Editor test hung',
    [n('t', 'trigger_manual', 0, 0), n('c', 'log', 300, 0, { message: 'x', require_approval: true })],
    [e('t', 'c')],
    { mode: 'crawl' },
  )
  await page.goto(`${BASE}/tools/flowautomator?flow=${askId}`)
  await page.locator('.vue-flow__node').nth(1).waitFor({ timeout: 30000 })
  const [, started] = await api('POST', `/flows/${askId}/run`, {})
  await page.locator('.fa-approval').first().waitFor({ timeout: 10000 })
  await page.route('**/flow-automator/api/status*', () => {}) // never answers
  const deadline = Date.now() + 15000
  let chip = ''
  while (Date.now() < deadline) {
    chip = await page.locator('.fa-engine-chip').innerText()
    if (chip.includes('not answering')) break
    await page.waitForTimeout(500)
  }
  const goDisabled = await page.locator('.fa-approval-go').first().isDisabled()
  const staleNote = await page.locator('.fa-approval-stale').count()
  check(
    'a hung engine shows "not answering" and approval cards are locked',
    chip.includes('not answering') && goDisabled && staleNote > 0,
    `${chip.replace(/\s+/g, ' ')} go-disabled=${goDisabled} stale=${staleNote}`,
  )
  await page.unroute('**/flow-automator/api/status*')
  await api('POST', `/runs/${started.run.id}/stop`, {}).catch(() => {})
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await page.screenshot({ path: `${SHOTS}/editor_fixes_error.png` }).catch(() => {})
} finally {
  await api('PUT', '/alerts', savedAlerts).catch(() => {})
  for (const id of created) {
    await api('POST', `/flows/${id}/disarm`, {}).catch(() => {})
    await api('DELETE', `/flows/${id}`).catch(() => {})
  }
  await browser.close()
}

const relevant = consoleErrors.filter((m) => !/favicon|ERR_ABORTED|500 \(Internal Server Error\)/.test(m))
console.log(`\nConsole errors (${relevant.length}):`)
relevant.slice(0, 20).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
