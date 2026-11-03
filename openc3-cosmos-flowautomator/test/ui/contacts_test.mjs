// Live test of contact scheduling: a TLE with epoch now and a station under its track give a real
// pass a few minutes out. Satellites, stations and activities are created through the UI, then the
// scheduled flows run against COSMOS at their contact times.
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const SETUP = JSON.parse(process.env.CONTACT_JSON)
const T0 = Date.now() / 1000 // when gen_tle ran (within a second or two)
const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const n = (id, type, x, y, data = {}) => ({ id, type, position: { x, y }, data })
const e = (s, t, h = 'out') => ({ id: `${s}-${h}-${t}`, source: s, sourceHandle: h, target: t })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const shot = (page, name) => page.screenshot({ path: `${SHOTS}/contacts_${name}.png` })

// COSMOS alert toasts (the engine's alerts) sit over the toolbar until acknowledged, like an operator would
const ackToasts = async (page) => {
  for (const button of await page.locator('button:has-text("Acknowledge")').all()) {
    await button.click().catch(() => {})
  }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1700, height: 1050 } })
const consoleErrors = []
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))
page.on('dialog', (d) => d.accept()) // window.confirm on delete
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
const activities = async () => (await api('GET', '/activities'))[1]
const waitFor = async (predicate, timeout, label) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await sleep(500)
  }
  throw new Error(`timed out: ${label}`)
}
const flows = []

// This test isn't about alerts (history_test.mjs is): keep COSMOS alert toasts from covering the toolbar
const [, savedAlerts] = await api('GET', '/alerts')
await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })
await page.waitForTimeout(1500)
await ackToasts(page)

// Leave the COSMOS as we found it: which flows were armed, and whether the contact scheduler ran
const snapshot = await (async () => {
  const [, list] = await api('GET', '/flows')
  const [, status] = await api('GET', '/status')
  return { armed: list.filter((f) => f.armed).map((f) => f.id), paused: !!status.contacts?.paused }
})()
const restore = async () => {
  for (const id of snapshot.armed) await api('POST', `/flows/${id}/arm`, {}).catch(() => {})
  await api('POST', snapshot.paused ? '/scheduler/pause' : '/scheduler/resume', { reason: 'Restored after a test' }).catch(() => {})
}

try {
  // ---- templates (made through the API; the template settings UI is checked below) ----
  const [, pass] = await api('POST', '/flows', {
    name: 'FA pass collect',
    template: true,
    mode: 'run',
    parameters: [
      { name: 'collect_type', type: 'choice', choices: ['NORMAL', 'SPECIAL'], default: 'NORMAL', description: 'Collect type' },
      { name: 'duration', type: 'number', default: 0.1, required: true },
    ],
    nodes: [
      n('t', 'trigger_manual', 0, 100),
      n('l', 'log', 260, 100, { message: 'Pre-pass setup for {{ contact_satellite }} over {{ contact_station }}, AOS in {{ round(time_to_aos()) }}s' }),
      n('w', 'wait_contact', 520, 100, { anchor: 'aos', offset: 0, label: 'Wait for AOS' }),
      n('c', 'send_cmd', 780, 100, { command: 'INST COLLECT with TYPE {{ collect_type }}, DURATION {{ duration }}' }),
      n('v', 'wait_check', 1040, 100, { condition: "tlm('INST HEALTH_STATUS COLLECT_TYPE') == collect_type", timeout: 10 }),
      n('k', 'contact_check', 1300, 100, { min_remaining: 60 }),
      n('ok', 'log', 1560, 40, { message: 'Plenty of contact left: {{ round(time_to_los()) }}s' }),
      n('short', 'log', 1560, 180, { message: 'Contact nearly over' }),
    ],
    edges: [e('t', 'l'), e('l', 'w'), e('w', 'c'), e('c', 'v'), e('v', 'k'), e('k', 'ok', 'ok'), e('k', 'short', 'short')],
  })
  if (pass.problems.length) throw new Error(JSON.stringify(pass.problems))
  flows.push(pass.flow.id)
  const [, crawl] = await api('POST', '/flows', {
    name: 'FA crawl check',
    template: true,
    mode: 'crawl',
    nodes: [
      n('t', 'trigger_manual', 0, 0),
      n('l', 'log', 260, 0, { message: 'In contact, {{ round(time_to_los()) }}s left' }),
      n('c', 'send_cmd', 520, 0, { command: 'INST COLLECT with TYPE NORMAL, DURATION 0.1' }),
    ],
    edges: [e('t', 'l'), e('l', 'c')],
  })
  flows.push(crawl.flow.id)

  // Earlier tests may have pressed Stop all, which pauses the scheduler
  await api('POST', '/scheduler/resume', {})

  // ---- Contacts view: add a satellite and a station through the dialogs ----
  await page.goto(`${BASE}/tools/flowautomator?view=contacts`)
  await page.locator('.fa-contacts').waitFor({ timeout: 60000 })
  check('Contacts view opens from ?view=contacts', (await page.locator('.fa-view-toggle .v-btn--active').innerText()).includes('Contacts'))

  await page.locator('.fa-add-satellite').click()
  await page.locator('[data-field="sat-name"] input').fill('FA-TEST')
  await page.locator('[data-field="sat-target"] input').fill('INST')
  await page.locator('[data-field="sat-tle"] textarea').first().fill('1 99999U nonsense\n2 99999 nonsense')
  await page.locator('.fa-sat-dialog button:has-text("Save")').click()
  await page.locator('.fa-sat-dialog .v-alert').waitFor()
  const tleError = await page.locator('.fa-sat-dialog .v-alert').innerText()
  check('bad TLE is rejected in the dialog', tleError.includes('69 characters'), tleError)
  await page.locator('[data-field="sat-tle"] textarea').first().fill(SETUP.tle)
  await page.locator('.fa-sat-dialog button:has-text("Save")').click()
  await page.locator('.fa-satellite', { hasText: 'FA-TEST' }).waitFor()
  const satText = await page.locator('.fa-satellite', { hasText: 'FA-TEST' }).innerText()
  check('satellite listed with its TLE', satText.includes('99999') && satText.includes('INST'), satText.replace(/\n/g, ' | '))

  await page.locator('.fa-add-station').click()
  await page.locator('[data-field="station-name"] input').fill('Test Site')
  await page.locator('[data-field="station-lat"] input').fill(String(SETUP.lat))
  await page.locator('[data-field="station-lon"] input').fill(String(SETUP.lon))
  await page.locator('[data-field="station-mask"] input').fill('10')
  await page.locator('.fa-station-dialog button:has-text("Save")').click()
  await page.locator('.fa-station', { hasText: 'Test Site' }).waitFor()

  // Passes are predicted in the background
  await waitFor(async () => (await api('GET', '/schedule'))[1].status.predicting === 0, 30000, 'prediction')
  const [, schedule] = await api('GET', '/schedule')
  const sat = schedule.satellites.find((s) => s.name === 'FA-TEST')
  const contact = schedule.contacts.find((c) => c.satellite_id === sat.id && c.aos > Date.now() / 1000)
  const expectedAos = T0 + SETUP.aos_in
  check(
    'engine predicts the pass the generator expects',
    contact && Math.abs(contact.aos - expectedAos) < 5 && Math.abs(contact.max_elevation - SETUP.max_el) < 0.5,
    contact ? `AOS in ${(contact.aos - Date.now() / 1000).toFixed(0)}s, max el ${contact.max_elevation}°, ${contact.duration.toFixed(0)}s` : 'none',
  )
  await page.locator(`.fa-tl-pass[data-contact="${contact.id}"]`).waitFor({ timeout: 5000 })
  check('pass is drawn on the timeline', true)
  await shot(page, '01_contacts')

  // ---- schedule the pass template into the contact through the dialog ----
  await page.locator(`.fa-contact-list tr[data-contact="${contact.id}"] .fa-schedule-btn`).click()
  await page.locator('.fa-template-select input').click()
  await page.locator('.fa-template-select input').pressSequentially('FA pass')
  await page.locator('.v-overlay .v-list-item', { hasText: 'FA pass collect' }).click()
  await page.locator('[data-param="collect_type"]').waitFor()
  const paramsShown = await page.locator('.fa-activity-dialog [data-param]').count()
  check('template parameters appear in the schedule dialog', paramsShown === 2, `${paramsShown} inputs`)
  await page.locator('[data-param="duration"] input').fill('0.2')
  await page.locator('[data-field="offset"] input').fill('-45')
  await page.locator('.fa-act-mode button', { hasText: 'Run' }).click()
  const preview = await page.locator('.fa-start-preview').innerText()
  check('start time preview', preview.includes('Starts') && !preview.includes('passed'), preview)
  await shot(page, '02_schedule_dialog')
  await page.locator('.fa-activity-save').click()
  await page.locator('.fa-activity-dialog').waitFor({ state: 'detached', timeout: 10000 }).catch(() => {})
  await page.locator('.fa-activities tr', { hasText: 'FA pass collect' }).waitFor()

  // ---- a crawl activity in the same contact: overlap warning, approvals later ----
  await page.locator(`.fa-contact-list tr[data-contact="${contact.id}"] .fa-schedule-btn`).click()
  await page.locator('.fa-template-select input').click()
  await page.locator('.fa-template-select input').pressSequentially('FA crawl')
  await page.locator('.v-overlay .v-list-item', { hasText: 'FA crawl check' }).click()
  await page.locator('[data-field="offset"] input').fill('5')
  const crawlMode = await page.locator('.fa-act-mode .v-btn--active').innerText()
  check('schedule dialog takes the template mode', crawlMode.includes('Crawl'), crawlMode)
  await page.locator('.fa-activity-save').click()
  await page.locator('.fa-activities tr', { hasText: 'FA crawl check' }).waitFor()
  await page.waitForTimeout(2500)
  const overlaps = await page.locator('.fa-activities .fa-overlap').count()
  check('activities sharing a contact get an overlap warning', overlaps === 2, `${overlaps} chips`)

  let list = await activities()
  const passAct = list.find((a) => a.template_name === 'FA pass collect')
  const crawlAct = list.find((a) => a.template_name === 'FA crawl check')
  check('activity saved with params and start', passAct.params.duration === '0.2' && Math.abs(passAct.start - (contact.aos - 45)) < 1, JSON.stringify(passAct.params))

  // ---- a new TLE moves the pass a little: activities follow ----
  const [tleStatus] = await api('POST', `/satellites/${sat.id}/tle`, { tle: SETUP.nudged })
  // Prediction runs in the background; wait for it to re-time the activity
  const moved = await waitFor(
    async () => (await activities()).find((a) => a.id === passAct.id && a.message.includes('Re-timed')),
    20000,
    're-time',
  ).catch(async () => (await activities()).find((a) => a.id === passAct.id))
  check(
    'new TLE re-times scheduled activities',
    tleStatus === 200 && moved.status === 'scheduled' && moved.message.includes('Re-timed'),
    `${moved.message}, drift ${moved.drift.toFixed(2)}s`,
  )

  // ---- the template is edited after scheduling ----
  const [, current] = await api('GET', `/flows/${pass.flow.id}`)
  await api('PUT', `/flows/${pass.flow.id}`, { ...current.flow, description: 'edited after scheduling' })
  await page.locator('.fa-activities tr', { hasText: /template v\d+ → v\d+/ }).waitFor({ timeout: 6000 })
  check('template edits are flagged on the activity', true)
  await page.locator('.fa-activities tr', { hasText: /template v\d+ → v\d+/ }).locator('button:has-text("Update")').click()
  await page.waitForTimeout(2500)
  check('Update pulls in the template', (await page.locator('.fa-activities tr', { hasText: /template v\d+ → v\d+/ }).count()) === 0)

  // ---- pause and resume ----
  await page.getByRole('button', { name: 'Pause scheduler' }).click()
  await page.locator('.fa-paused').waitFor()
  await page.locator('.fa-resume').click()
  await page.locator('.fa-paused').waitFor({ state: 'detached' })
  check('pause and resume the scheduler', true)
  await shot(page, '03_scheduled')

  // ---- wait for the contact ----
  const collectsBefore = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  console.log(`      waiting for the start (AOS - 45s) in ${(moved.start - Date.now() / 1000).toFixed(0)}s ...`)
  await waitFor(async () => (await activities()).find((a) => a.id === passAct.id).status === 'running', 240000, 'pass activity start')
  const startedAt = Date.now() / 1000
  check('activity starts at AOS - 45s', Math.abs(startedAt - moved.start) < 3, `${(startedAt - moved.start).toFixed(1)}s from planned`)
  await page.waitForTimeout(1500)
  await shot(page, '04_running')
  let ranAct = await waitFor(
    async () => (await activities()).find((a) => a.id === passAct.id && ['done', 'failed', 'stopped'].includes(a.status)),
    120000,
    'pass activity end',
  )
  const contactNow = (await api('GET', '/schedule'))[1].contacts.find((c) => c.id === moved.contact.id)
  const run = (await api('GET', `/runs/${ranAct.run_id}`))[1]
  const cmdStep = run.steps.find((s) => s.node === 'c')
  check(
    'command waited for AOS, then went out',
    ranAct.status === 'done' && cmdStep && Math.abs(cmdStep.started_at - contactNow.aos) < 2,
    `${ranAct.status}: command ${(cmdStep?.started_at - contactNow.aos).toFixed(2)}s after AOS`,
  )
  const collectsAfter = await rpc('tlm', 'INST HEALTH_STATUS COLLECTS')
  const collectType = await rpc('tlm', 'INST HEALTH_STATUS COLLECT_TYPE')
  check('COSMOS received the command', collectsAfter > collectsBefore && collectType === 'NORMAL', `COLLECTS ${collectsBefore} -> ${collectsAfter}`)
  const pre = run.steps.find((s) => s.node === 'l')
  check('run had contact variables and parameters', pre.message.includes('FA-TEST over Test Site') && run.variables.duration === 0.2, pre.message)
  check('contact check took the ok branch', run.steps.some((s) => s.node === 'ok'), run.steps.find((s) => s.node === 'k')?.message)

  // ---- the crawl activity asks the operator in the Contacts view ----
  await page.locator('.fa-approval').first().waitFor({ timeout: 60000 })
  const card = await page.locator('.fa-approval').first().innerText()
  check('crawl activity asks for approval in the Contacts view', card.includes('FA crawl check'), card.split('\n')[0])
  await shot(page, '05_approval')
  await page.locator('.fa-approval button:has-text("run rest")').first().click()
  ranAct = await waitFor(
    async () => (await activities()).find((a) => a.id === crawlAct.id && ['done', 'failed', 'stopped'].includes(a.status)),
    30000,
    'crawl activity end',
  )
  check('crawl activity completes after approval', ranAct.status === 'done', ranAct.message)
  const crawlRun = (await api('GET', `/runs/${ranAct.run_id}`))[1]
  check('approval cut-off set from LOS', Math.abs(crawlRun.approval_deadline - (contactNow.los - 30)) < 1)

  await page.waitForTimeout(2500)
  const doneChips = await page.locator('.fa-activities .fa-activity-status', { hasText: 'done' }).count()
  check('activities show done', doneChips === 2, `${doneChips}`)
  await shot(page, '06_done')

  // ---- open the run from the activity ----
  await page.locator(`.fa-activities tr[data-activity="${passAct.id}"] button[title="Open the run"]`).click()
  await page.locator('.vue-flow__node').nth(7).waitFor({ timeout: 10000 })
  await page.waitForTimeout(1000)
  const runLog = await page.locator('.fa-steps').innerText()
  check('open run jumps to the template with that run', runLog.includes('Reached AOS'), '')
  await shot(page, '07_run')

  // ---- template settings and Run now with parameters ----
  await page.locator('.vue-flow__pane').click({ position: { x: 50, y: 50 } })
  const switchOn = await page.locator('.fa-template-switch input').isChecked()
  const paramRows = await page.locator('.fa-inspector .fa-param').count()
  check('flow settings show the template switch and parameters', switchOn && paramRows === 2, `${paramRows} parameters`)
  await ackToasts(page)
  await page.getByRole('button', { name: 'Run now' }).click()
  await page.locator('.fa-run-dialog').waitFor()
  await page.locator('.fa-run-dialog [data-param="duration"] input').fill('0.3')
  await page.locator('.fa-run-contact button', { hasText: 'None' }).click()
  await page.locator('.fa-run-go').click()
  await page.locator('.fa-steps', { hasText: 'no contact' }).waitFor({ timeout: 10000 })
  check('Run now asks for parameters; contact nodes explain a manual run has no contact', true)
  await shot(page, '08_manual')
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await shot(page, 'error').catch(() => {})
} finally {
  await restore()
  await api('PUT', '/alerts', savedAlerts).catch(() => {})
  for (const a of await activities().catch(() => []))
    if (flows.includes(a.template_id)) await api('DELETE', `/activities/${a.id}`).catch(() => {})
  const [, schedule] = await api('GET', '/schedule').catch(() => [0, { satellites: [], stations: [] }])
  for (const s of schedule.satellites.filter((x) => x.name === 'FA-TEST')) await api('DELETE', `/satellites/${s.id}`)
  for (const s of schedule.stations.filter((x) => x.name === 'Test Site')) await api('DELETE', `/stations/${s.id}`)
  for (const id of flows) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
