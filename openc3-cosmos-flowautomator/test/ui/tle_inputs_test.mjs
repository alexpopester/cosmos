// TLE hooks, live: HTTP bulk, a COSMOS stash key, a bucket file, held jumps, upload and inputs dialogs.
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const FILE = 'INST/tle/fa_hook_test.tle'
const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// A TLE like orbit.make_tle: same orbit at a later epoch, mean anomaly advanced
const T0 = Math.floor(Date.now() / 1000) - 7200
function checksum(line) {
  let sum = 0
  for (const c of line.slice(0, 68)) sum += c === '-' ? 1 : /\d/.test(c) ? Number(c) : 0
  return String(sum % 10)
}
function tle(catalog, epoch, raan = 200, name = '') {
  const d = new Date(epoch * 1000)
  const start = Date.UTC(d.getUTCFullYear(), 0, 1) / 1000
  const day = (epoch - start) / 86400 + 1
  const ma = (10 + (360 * 15.5 * (epoch - T0)) / 86400) % 360
  const f = (v, w, p) => v.toFixed(p).padStart(w)
  const l1 = `1 ${String(catalog).padStart(5, '0')}U 26001A   ${String(d.getUTCFullYear() % 100).padStart(2, '0')}${day.toFixed(8).padStart(12, '0')}  .00000000  00000-0  00000-0 0  999`
  const l2 = `2 ${String(catalog).padStart(5, '0')} ${f(51.6, 8, 4)} ${f(raan % 360, 8, 4)} 0006703 ${f(130, 8, 4)} ${f(ma, 8, 4)} ${f(15.5, 11, 8)}${'1'.padStart(5)}`
  const lines = [l1 + checksum(l1), l2 + checksum(l2)]
  return (name ? [name, ...lines] : lines).join('\n')
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1700, height: 1050 } })
const consoleErrors = []
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
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
const satellite = async (name) => (await api('GET', '/schedule'))[1].satellites.find((s) => s.name === name)
const waitFor = async (predicate, timeout, label) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await sleep(500)
  }
  throw new Error(`timed out: ${label}`)
}
const [, savedInputs] = await api('GET', '/tle_inputs')
let satId = null

try {
  await api('PUT', '/tle_inputs', { poll_seconds: 5, files: [FILE] })
  const [, created] = await api('POST', '/satellites', { name: 'HOOK-1', tle: tle(91001, T0) })
  satId = created.satellite.id
  check('satellite gets its catalog number from its first TLE', created.satellite.catalog === 91001)

  // ---- HTTP bulk ----
  let [status, body] = await api('POST', '/tles', { tle: `${tle(91001, T0 + 1800)}\n${tle(91999, T0, 200, 'NOBODY')}`, source: 'fds' })
  check('HTTP bulk: matched by catalog, unknown reported', status === 200 && body.counts.applied === 1 && body.counts.unknown === 1, JSON.stringify(body.counts))
  ;[, body] = await api('POST', '/tles', { tle: tle(91001, T0), source: 'fds' })
  check('an older TLE is skipped', body.results[0].result === 'skipped', body.results[0].reason)

  // ---- COSMOS stash (what any COSMOS script's stash_set does) ----
  await rpc('stash_set', 'flow_automator_tles', { tle: tle(91001, T0 + 3600), source: 'stash-pipeline' })
  const viaStash = await waitFor(async () => (await satellite('HOOK-1'))?.tle?.source === 'stash-pipeline', 30000, 'stash')
  const stashResult = await rpc('stash_get', 'flow_automator_tles_result')
  check('stash: picked up and applied, result left for the sender', viaStash && stashResult?.counts?.applied === 1, JSON.stringify(stashResult?.counts))

  // ---- a file in the COSMOS bucket ----
  const wrote = await page.request.post(`${BASE}/script-api/scripts/${FILE}?scope=DEFAULT`, {
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: JSON.stringify({ text: `HOOK-1\n${tle(91001, T0 + 5400).split('\n').slice(-2).join('\n')}\n` }),
  })
  check('test file written to the bucket', wrote.ok(), String(wrote.status()))
  const viaFile = await waitFor(async () => (await satellite('HOOK-1'))?.tle?.source === `file:${FILE}`, 30000, 'file')
  check('bucket file: read and applied', viaFile)

  // ---- a jump is held, then accepted in the UI ----
  ;[, body] = await api('POST', '/tles', { tle: tle(91001, T0 + 6000, 240), source: 'fds' })
  check('a TLE that jumps is held for review', body.results[0].result === 'held', body.results[0].reason.slice(0, 80))
  await page.goto(`${BASE}/tools/flowautomator?view=contacts`)
  await page.locator('.fa-contacts').waitFor({ timeout: 60000 })
  await page.locator('.fa-satellite', { hasText: 'HOOK-1' }).locator('.fa-held-tle').waitFor({ timeout: 10000 })
  check('the satellite shows the held TLE', true)
  await page.locator('.fa-satellite', { hasText: 'HOOK-1' }).click()
  await page.locator('.fa-held-alert').waitFor()
  const heldText = await page.locator('.fa-held-alert').innerText()
  await page.screenshot({ path: `${SHOTS}/tle_01_held.png` })
  await page.locator('.fa-accept-held').click()
  const accepted = await waitFor(async () => {
    const sat = await satellite('HOOK-1')
    return !sat.held_tle && sat.tle.source === 'fds' && Math.abs(sat.tle.epoch - (T0 + 6000)) < 1 && sat
  }, 10000, 'accept')
  check('Accept applies the held TLE', !!accepted, heldText.split('\n')[0])

  // ---- upload dialog ----
  await page.locator('.fa-upload-tles').click()
  await page.locator('[data-field="upload-tles"] textarea').first().fill(`${tle(91001, T0)}\n${tle(91001, T0 + 7000, 240)}`)
  await page.locator('.fa-upload-go').click()
  await page.locator('.fa-upload-results tbody tr').nth(1).waitFor()
  const uploadRows = await page.locator('.fa-upload-results tbody tr').allInnerTexts()
  check('upload shows what happened to each TLE', uploadRows[0].includes('skipped') && uploadRows[1].includes('applied'), uploadRows.map((r) => r.replace(/\s+/g, ' ')).join(' | '))
  await page.screenshot({ path: `${SHOTS}/tle_02_upload.png` })
  await page.keyboard.press('Escape')

  // ---- inputs dialog shows each input's last result ----
  await page.locator('.fa-tle-inputs-btn').click()
  await page.locator('.fa-tle-inputs').waitFor()
  await page.waitForTimeout(800)
  const inputsText = await page.locator('.fa-tle-inputs').innerText()
  check('TLE inputs dialog shows the stash and file results', inputsText.includes('1 applied') && inputsText.includes(FILE), '')
  await page.screenshot({ path: `${SHOTS}/tle_03_inputs.png` })
  await page.keyboard.press('Escape')

  const audit = (await api('GET', '/audit?search=HOOK'))[1].length + (await api('GET', '/audit?search=ingested'))[1].length
  check('the audit log records TLE batches', audit > 0)
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await page.screenshot({ path: `${SHOTS}/tle_error.png` }).catch(() => {})
} finally {
  await api('PUT', '/tle_inputs', { poll_seconds: savedInputs.poll_seconds, files: savedInputs.files, stash_key: savedInputs.stash_key }).catch(() => {})
  if (satId) await api('DELETE', `/satellites/${satId}`).catch(() => {})
  await page.request.delete(`${BASE}/script-api/scripts/${FILE}?scope=DEFAULT`, { headers: { Authorization: token } }).catch(() => {})
  await rpc('stash_delete', 'flow_automator_tles_result').catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
