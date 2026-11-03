// Rules, station capacity and the audit log, through the UI. Needs CONTACT_JSON from gen_tle.py
// (a TLE with epoch now); a second satellite a little behind the first gives overlapping passes.
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const SETUP = JSON.parse(process.env.CONTACT_JSON)
const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const shot = (page, name) => page.screenshot({ path: `${SHOTS}/rules_${name}.png` })

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
const waitFor = async (predicate, timeout, label) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await sleep(400)
  }
  throw new Error(`timed out: ${label}`)
}
const created = { flows: [], sats: [], stations: [] }
const lines = SETUP.tle.split('\n')
const behind = SETUP.nudged.split('\n') // mean anomaly a little different: overlapping passes

try {
  const [, flow] = await api('POST', '/flows', {
    name: 'RULE downlink',
    template: true,
    mode: 'run',
    parameters: [{ name: 'rate', type: 'number', default: 2 }],
    nodes: [
      { id: 't', type: 'trigger_manual', position: { x: 0, y: 0 }, data: {} },
      { id: 'l', type: 'log', position: { x: 260, y: 0 }, data: { message: 'rate {{ rate }}' } },
    ],
    edges: [{ id: 'e', source: 't', sourceHandle: 'out', target: 'l' }],
  })
  created.flows.push(flow.flow.id)
  for (const [name, tle] of [
    ['RULE-A', `${lines[1]}\n${lines[2]}`],
    ['RULE-B', behind.join('\n')],
  ]) {
    const [, body] = await api('POST', '/satellites', { name, tle })
    created.sats.push(body.satellite.id)
  }

  await api('POST', '/scheduler/resume', {}) // earlier tests may have pressed Stop all
  await page.goto(`${BASE}/tools/flowautomator?view=contacts`)
  await page.locator('.fa-contacts').waitFor({ timeout: 60000 })

  // ---- a station with capacity 1, through the dialog ----
  await page.locator('.fa-add-station').click()
  await page.locator('[data-field="station-name"] input').fill('Rule Site')
  await page.locator('[data-field="station-lat"] input').fill(String(SETUP.lat))
  await page.locator('[data-field="station-lon"] input').fill(String(SETUP.lon))
  await page.locator('[data-field="station-mask"] input').fill('5')
  await page.locator('[data-field="station-capacity"] input').fill('1')
  await page.locator('.fa-station-dialog button:has-text("Save")').click()
  await page.locator('.fa-station', { hasText: 'Rule Site' }).waitFor()
  const [, schedule] = await api('GET', '/schedule')
  created.stations.push(schedule.stations.find((s) => s.name === 'Rule Site').id)
  await waitFor(async () => (await api('GET', '/schedule'))[1].status.predicting === 0, 30000, 'prediction')
  const contacts = (await api('GET', '/schedule'))[1].contacts.filter((c) => c.station === 'Rule Site')
  const overlapping = contacts.filter((a) =>
    contacts.some((b) => b.satellite !== a.satellite && a.aos < b.los && b.aos < a.los && !a.partial && !b.partial),
  )
  check('two satellites have overlapping passes over the station', overlapping.length >= 2, `${contacts.length} contacts, ${overlapping.length} overlapping`)

  // ---- a rule for RULE-A through the dialog ----
  await page.locator('.fa-add-rule').click()
  await page.locator('.fa-rule-template input').click()
  await page.locator('.fa-rule-template input').pressSequentially('RULE down')
  await page.locator('.v-overlay .v-list-item', { hasText: 'RULE downlink' }).click()
  await page.locator('[data-field="rule-name"] input').fill('A passes')
  await page.locator('.fa-rule-dialog .v-select', { hasText: 'Satellites' }).click()
  await page.locator('.v-overlay .v-list-item', { hasText: 'RULE-A' }).click()
  await page.keyboard.press('Escape')
  await page.locator('[data-field="rule-min-el"] input').fill('0')
  await page.locator('[data-field="rule-offset"] input').fill('-30')
  await page.locator('.fa-rule-dialog [data-param="rate"] input').fill('8')
  await shot(page, '01_rule_dialog')
  await page.locator('.fa-rule-save').click()
  await page.locator('.fa-rule', { hasText: 'A passes' }).waitFor()
  await page.waitForTimeout(2500)
  const ruleText = await page.locator('.fa-rule', { hasText: 'A passes' }).innerText()
  let activities = (await api('GET', '/activities'))[1]
  const aActs = activities.filter((a) => a.rule_name === 'A passes')
  const aPasses = contacts.filter((c) => c.satellite === 'RULE-A' && !c.partial && c.aos - 30 > Date.now() / 1000 + 10)
  check('rule scheduled every RULE-A pass', aActs.length === aPasses.length && aActs.length > 0, `${aActs.length} activities · ${ruleText.replace(/\n/g, ' ')}`)
  check('rule activities carry its parameters', aActs.every((a) => String(a.params.rate) === '8' && a.offset === -30))
  check('rule chip shown on activities', (await page.locator('.fa-activities .fa-rule-chip', { hasText: 'A passes' }).count()) === aActs.length)

  // ---- a lower-priority rule for RULE-B skips passes where the station is busy ----
  const [, ruleB] = await api('POST', '/rules', { name: 'B passes', template_id: flow.flow.id, satellite_ids: [created.sats[1]], min_elevation: 0, offset: -30 })
  const bPasses = contacts.filter((c) => c.satellite === 'RULE-B' && !c.partial && c.aos - 30 > Date.now() / 1000 + 10)
  check('busy station: lower priority rule skips overlapping passes', ruleB.created < bPasses.length && ruleB.created + ruleB.rule.skipped_full >= bPasses.length - 1, `${ruleB.created} scheduled, skipped ${ruleB.rule.skipped_full} of ${bPasses.length}`)
  activities = (await api('GET', '/activities'))[1]
  check('no station conflicts while rules skip busy passes', activities.every((a) => !a.conflicts.length))

  // ---- forcing it creates conflicts that are flagged ----
  await api('PUT', `/rules/${ruleB.rule.id}`, { skip_full_stations: false })
  await page.waitForTimeout(2500)
  const conflictChips = await page.locator('.fa-activities .fa-conflict').count()
  check('overlapping activities at a capacity-1 station are flagged', conflictChips >= 2, `${conflictChips} station busy chips`)
  await shot(page, '02_conflicts')
  // Capacity 2 clears them
  await api('PUT', `/stations/${created.stations[0]}`, { capacity: 2 })
  await page.waitForTimeout(2500)
  check('raising capacity clears the conflicts', (await page.locator('.fa-activities .fa-conflict').count()) === 0)

  // ---- deleting a rule activity cancels it and the rule leaves it ----
  const victim = (await api('GET', '/activities'))[1].find((a) => a.rule_name === 'A passes' && a.status === 'scheduled')
  await page.locator(`.fa-activities tr[data-activity="${victim.id}"] button[title="Delete"]`).click()
  await page.waitForTimeout(2500)
  const after = (await api('GET', '/activities'))[1].find((a) => a.id === victim.id)
  check('deleting a rule activity cancels it', after.status === 'cancelled' && after.message.includes('Cancelled by'), after.message)
  await api('POST', '/contacts/recompute', {})
  await waitFor(async () => (await api('GET', '/schedule'))[1].status.predicting === 0, 30000, 'recompute')
  const again = (await api('GET', '/activities'))[1].filter((a) => a.contact.id === victim.contact.id && a.status === 'scheduled' && a.rule_name === 'A passes')
  check('the rule does not schedule it again', again.length === 0)

  // ---- audit log ----
  await page.locator('.v-btn', { hasText: 'View' }).first().click()
  await page.locator('.v-overlay .v-list-item', { hasText: 'Audit Log' }).click()
  await page.locator('.fa-audit-table tbody tr').first().waitFor()
  await page.waitForTimeout(800)
  const auditText = await page.locator('.fa-audit-table').innerText()
  check(
    'audit log lists who did what',
    ['added', 'created', 'cancelled', 'Rule Site', 'A passes', 'anonymous'].every((w) => auditText.includes(w)),
    auditText.split('\n').slice(1, 4).join(' | '),
  )
  await page.locator('.fa-audit-search input').fill('Rule Site')
  await page.waitForTimeout(1200)
  const rowTexts = await page.locator('.fa-audit-table tbody tr').allInnerTexts()
  check('audit search filters', rowTexts.length >= 2 && rowTexts.every((t) => t.includes('Rule Site')), `${rowTexts.length} rows`)
  await shot(page, '03_audit')
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await shot(page, 'error').catch(() => {})
} finally {
  const [, rules] = await api('GET', '/rules').catch(() => [0, []])
  for (const r of rules) if (['A passes', 'B passes'].includes(r.name)) await api('DELETE', `/rules/${r.id}`)
  for (const a of (await api('GET', '/activities').catch(() => [0, []]))[1]) await api('DELETE', `/activities/${a.id}`).catch(() => {})
  for (const id of created.sats) await api('DELETE', `/satellites/${id}`).catch(() => {})
  for (const id of created.stations) await api('DELETE', `/stations/${id}`).catch(() => {})
  for (const id of created.flows) await api('DELETE', `/flows/${id}`).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
