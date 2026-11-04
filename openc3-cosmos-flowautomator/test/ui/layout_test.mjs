// Layout of the Flows view: View/Edit mode, folding side panels, flow / split / log-only layouts,
// the log splitter, the All running tab, readable zoom on wide flows and a wrapping toolbar.
// Makes its own flow (deleted after) and stops the dry run it starts.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const SHOTS = process.env.SHOTS || 'shots'
const PASSWORD = process.env.COSMOS_PASSWORD
fs.mkdirSync(SHOTS, { recursive: true })

const results = []
const check = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}
const shot = (name) => page.screenshot({ path: `${SHOTS}/layout_${name}.png` })
const LAYOUT_KEY = 'openc3.flowautomator.layout'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
const consoleErrors = []
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))

let token = null
const api = async (method, path, body) => {
  const res = await page.request.fetch(`${BASE}/flow-automator/api${path}?scope=DEFAULT`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
  })
  return res.json()
}
const box = (selector) => page.locator(selector).first().boundingBox()

let flowId = null
let runId = null
let savedAlerts = null
try {
  await page.goto(`${BASE}/login`)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.locator('button:has-text("Login")').click()
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30000 })
  token = await page.evaluate(() => localStorage.openc3Token)
  await page.evaluate((key) => localStorage.removeItem(key), LAYOUT_KEY)

  // Keep COSMOS alert toasts off the toolbar while this runs
  savedAlerts = await api('GET', '/alerts')
  await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })

  // A wide flow (fitting it all would make text unreadable) whose 5th step asks for approval
  const nodes = [{ id: 't', type: 'trigger_manual', position: { x: 0, y: 100 }, data: {} }]
  const edges = []
  for (let i = 1; i <= 9; i++) {
    nodes.push({
      id: `l${i}`,
      type: 'log',
      position: { x: i * 280, y: 100 },
      data: { message: `step ${i}`, label: `Step ${i}`, require_approval: i === 5 },
    })
    edges.push({ source: i === 1 ? 't' : `l${i - 1}`, sourceHandle: 'out', target: `l${i}` })
  }
  const created = await api('POST', '/flows', { name: 'LAYOUT wide flow', mode: 'walk', nodes, edges })
  flowId = created.flow.id
  const started = await api('POST', `/flows/${flowId}/run`, { dry_run: true, mode: 'walk' })
  runId = started.run?.id
  check('dry run started', !!runId, started.result)

  await page.goto(`${BASE}/tools/flowautomator?flow=${flowId}`)
  await page.locator('.vue-flow__node').first().waitFor({ timeout: 60000 })
  await page.waitForTimeout(1500)
  await shot('01_edit_split')
  const zoom = await page.evaluate(
    () => new DOMMatrix(getComputedStyle(document.querySelector('.vue-flow__transformationpane')).transform).a,
  )
  check('wide flow opens at a readable zoom', zoom >= 0.6, String(zoom))
  check(
    'edit mode (the default) shows palette and settings',
    (await page.locator('.fa-palette').isVisible()) && (await page.locator('.fa-side').isVisible()),
  )

  await page.locator('.fa-palette-filter input').fill('wait')
  const filtered = await page.locator('.fa-palette-item').count()
  check('palette filter narrows the list', filtered > 0 && filtered < 26, `${filtered}`)
  await page.locator('.fa-palette-filter input').fill('')

  await page.locator('.fa-palette-hide').click()
  await page.locator('.fa-inspector-hide').click()
  check('side panels fold to rails', (await page.locator('.fa-rail').count()) === 2)
  await shot('02_rails')
  await page.locator('.fa-rail-left').click()
  check('rail reopens the palette', await page.locator('.fa-palette').isVisible())

  await page.locator('.fa-edit-toggle button:has-text("View")').click()
  check('view mode hides palette, settings and rails', (await page.locator('.fa-palette, .fa-side, .fa-rail').count()) === 0)
  check('view mode says the canvas is locked', await page.locator('.fa-view-badge').isVisible())
  const nodeClass = await page.locator('.vue-flow__node').first().getAttribute('class')
  check('nodes are not draggable in view mode', !nodeClass.includes('draggable'), nodeClass)
  await shot('03_view_split')

  // Approval cards cover the canvas: they minimize to a pill, and reopen for a new approval
  await page.locator('.fa-approvals-minimize').click()
  check('approval cards minimize to a pill', (await page.locator('.fa-approval').isVisible()) === false && (await page.locator('.fa-approvals-restore').isVisible()))
  await page.locator('.fa-approvals-restore').click()
  check('the pill brings the cards back', await page.locator('.fa-approval').first().isVisible())

  await page.locator('.fa-layout-toggle button:has-text("Flow")').click()
  check('flow only folds the log body away', !(await page.locator('.fa-run-panel-body').isVisible()))
  // The folded log bar and its Show log button must be on screen, not under the COSMOS footer
  const showLog = await box('.fa-show-log')
  const footer = await box('#footer')
  check('Show log button is visible above the footer', showLog && (!footer || showLog.y + showLog.height <= footer.y), JSON.stringify([showLog, footer]))
  await shot('04_flow_only')
  await page.locator('.fa-show-log').click()
  check('Show log brings the log back', await page.locator('.fa-run-panel-body').isVisible())
  await page.locator('.fa-layout-toggle button:has-text("Flow")').click()
  await page.locator('.v-tab:has-text("Problems")').click()
  check('clicking a log tab brings the log back', await page.locator('.fa-run-panel-body').isVisible())

  await page.locator('.fa-layout-toggle button:has-text("Log")').click()
  check('log only hides the canvas', !(await page.locator('.fa-canvas').isVisible()))
  await page.locator('.fa-engine-chip').click()
  const row = page.locator('.fa-running-row', { hasText: 'LAYOUT wide flow' })
  await row.waitFor({ timeout: 5000 })
  check('engine chip opens All running with the run', (await row.innerText()).includes('awaiting approval'))
  await page.locator('.fa-approvals').waitFor({ timeout: 5000 })
  const card = await box('.fa-approvals')
  const rowBox = await row.boundingBox()
  check('in log only the approval sits above the log, not over it', card.y + card.height <= rowBox.y)
  await shot('05_log_only_running')

  await page.locator('.fa-layout-toggle button:has-text("Both")').click()
  const before = await box('.fa-bottom')
  const splitter = await box('.fa-splitter')
  await page.mouse.move(splitter.x + 200, splitter.y + 3)
  await page.mouse.down()
  await page.mouse.move(splitter.x + 200, splitter.y - 150, { steps: 5 })
  await page.mouse.up()
  const after = await box('.fa-bottom')
  check('dragging the splitter resizes the log', after.height > before.height + 100, `${before.height} -> ${after.height}`)

  await page.reload()
  await page.locator('.fa-toolbar').waitFor()
  await page.waitForTimeout(1500)
  check(
    'view mode and log height are remembered after reload',
    (await page.locator('.fa-palette').count()) === 0 && Math.abs((await box('.fa-bottom')).height - after.height) < 3,
  )

  await page.locator('.fa-edit-toggle button:has-text("Edit")').click()
  // A laptop screen with both side panels open: everything still on screen and the log tabs fit
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.waitForTimeout(800)
  await shot('06_laptop')
  const tabsScroll = await page.locator('.fa-run-panel-head .v-slide-group__prev--disabled, .fa-run-panel-head .v-slide-group__next:not(.v-slide-group__next--disabled)').count()
  const tabsFit = await page.evaluate(() => {
    const c = document.querySelector('.fa-run-panel-head .v-slide-group__content')
    const w = document.querySelector('.fa-run-panel-head .v-slide-group__container')
    return c && w ? c.scrollWidth <= w.clientWidth + 1 : false
  })
  check('log tabs fit at 1366px with both side panels open', tabsFit, `scroll arrows: ${tabsScroll}`)
  const footerLap = await box('#footer')
  const bottom = await box('.fa-bottom')
  check('log panel ends above the footer at 1366x768', !footerLap || bottom.y + bottom.height <= footerLap.y + 1)

  await page.setViewportSize({ width: 1100, height: 900 })
  await page.waitForTimeout(500)
  await shot('07_narrow')
  const stopAll = await box('.fa-toolbar button:has-text("Stop all")')
  check('toolbar wraps: Stop all is on screen at 1100px', stopAll && stopAll.x + stopAll.width <= 1100)
  check('no page errors', consoleErrors.length === 0, consoleErrors.join(' | '))
} finally {
  if (runId) await api('POST', `/runs/${runId}/stop`, {}).catch(() => {})
  if (flowId) await api('DELETE', `/flows/${flowId}`).catch(() => {})
  if (savedAlerts) await api('PUT', '/alerts', savedAlerts).catch(() => {})
  await page.evaluate((key) => localStorage.removeItem(key), LAYOUT_KEY).catch(() => {})
  await browser.close()
}
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
