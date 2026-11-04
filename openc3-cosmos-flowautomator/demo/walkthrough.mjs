// A slow, captioned walkthrough of the Flow Automator demo, for screen recording.
//
// Drives a real Chrome window (connected over Chrome's remote debugging, CDP_URL) so OBS can record
// it, or a headless browser (HEADLESS=1) to rehearse. Captions, a visible cursor and a highlight
// ring show what's happening. It loads the demo, so it changes demo items only (flows and rules
// named "Demo: ", DEMOSAT satellites, the demo stations), sends real commands to the demo INST
// target, presses Stop all near the end and then puts back what Stop all changed.
//
//   COSMOS_PASSWORD=... ./walkthrough.sh          # see walkthrough.sh; SPEED=0.5 halves the pauses
import { chromium } from 'playwright'

const BASE = process.env.COSMOS_URL || 'http://localhost:2900'
const PASSWORD = process.env.COSMOS_PASSWORD
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222'
const HEADLESS = process.env.HEADLESS === '1'
const SPEED = Number(process.env.SPEED || 0.75) // multiplies every pause (1 = slower, 0.5 = brisk)
const FULLSCREEN = process.env.FULLSCREEN !== '0'
const VIDEO_DIR = process.env.VIDEO_DIR || '' // headless only: record a video here
const t0 = Date.now()
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(0).padStart(4)}s]`, ...a)

// ---- OBS (optional): start and stop the recording over obs-websocket v5 ---------------------
// OBS_URL (ws://127.0.0.1:4455) and OBS_PASSWORD; walkthrough.sh fills them in with OBS_RECORD=1
import crypto from 'node:crypto'
const OBS_URL = process.env.OBS_URL || ''
const sha256b64 = (s) => crypto.createHash('sha256').update(s).digest('base64')
const obsRequest = (requestType) =>
  new Promise((resolve, reject) => {
    const ws = new WebSocket(OBS_URL)
    const timer = setTimeout(() => {
      ws.close()
      reject(new Error('OBS did not answer'))
    }, 8000)
    ws.onerror = () => reject(new Error(`can't reach OBS at ${OBS_URL}`))
    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data)
      if (msg.op === 0) {
        // Hello: identify, with the challenge answered when OBS wants a password
        const identify = { rpcVersion: 1 }
        if (msg.d.authentication) {
          const { salt, challenge } = msg.d.authentication
          identify.authentication = sha256b64(sha256b64(process.env.OBS_PASSWORD + salt) + challenge)
        }
        ws.send(JSON.stringify({ op: 1, d: identify }))
      } else if (msg.op === 2) {
        ws.send(JSON.stringify({ op: 6, d: { requestType, requestId: requestType } }))
      } else if (msg.op === 7) {
        clearTimeout(timer)
        ws.close()
        const status = msg.d.requestStatus
        status.result ? resolve(msg.d.responseData) : reject(new Error(`OBS ${requestType}: ${status.comment || status.code}`))
      }
    }
  })
let obsRecording = false
const obs = async (requestType) => {
  if (!OBS_URL) return
  try {
    const out = await obsRequest(requestType)
    if (requestType === 'StartRecord') obsRecording = true
    if (requestType === 'StopRecord') obsRecording = false
    console.log(`OBS ${requestType}${out?.outputPath ? `: ${out.outputPath}` : ''}`)
  } catch (e) {
    console.log(`OBS ${requestType} failed: ${e.message}`)
    if (requestType === 'StartRecord') throw e // don't run a walkthrough nobody is recording
  }
}

// ---- browser --------------------------------------------------------------------------------
let browser
let page
let context
if (HEADLESS) {
  browser = await chromium.launch()
  context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    ...(VIDEO_DIR ? { recordVideo: { dir: VIDEO_DIR, size: { width: 1920, height: 1080 } } } : {}),
  })
  page = await context.newPage()
} else {
  browser = await chromium.connectOverCDP(CDP_URL)
  context = browser.contexts()[0]
  page = context.pages()[0] || (await context.newPage())
  await page.bringToFront()
}
page.on('dialog', (d) => d.accept()) // window.confirm for Load Demo and similar
const cdp = HEADLESS ? null : await context.newCDPSession(page)
const setWindow = async (windowState) => {
  if (!cdp || !FULLSCREEN) return
  try {
    const { windowId } = await cdp.send('Browser.getWindowForTarget')
    await cdp.send('Browser.setWindowBounds', { windowId, bounds: { windowState } })
  } catch (e) {
    log('window state', windowState, 'not set:', e.message)
  }
}

// ---- pacing, captions, cursor ---------------------------------------------------------------
const wait = (ms) => page.waitForTimeout(ms * SPEED)

const OVERLAY_CSS = `
#wt-cap{position:fixed;left:50%;transform:translateX(-50%);max-width:1150px;min-width:420px;padding:14px 24px 16px;
  border-radius:10px;background:rgba(8,16,26,.94);color:#fff;font:500 23px/1.4 Roboto,system-ui,sans-serif;
  z-index:2147483647;box-shadow:0 8px 28px rgba(0,0,0,.55);border-left:6px solid #4dacff;pointer-events:none;
  transition:opacity .35s, top .35s, bottom .35s;opacity:0}
#wt-cap.wt-bottom{bottom:52px;top:auto} #wt-cap.wt-top{top:118px;bottom:auto}
#wt-cap small{display:block;font-size:14px;font-weight:700;color:#8cc8ff;margin-bottom:3px;letter-spacing:.08em;text-transform:uppercase}
#wt-cursor{position:fixed;left:-60px;top:-60px;width:28px;height:28px;z-index:2147483647;pointer-events:none;
  transition:left .75s cubic-bezier(.3,.7,.4,1), top .75s cubic-bezier(.3,.7,.4,1);filter:drop-shadow(0 2px 3px rgba(0,0,0,.6))}
#wt-cursor.wt-press{transform:scale(.8)}
#wt-ring{position:fixed;border:3px solid #ffd54a;border-radius:8px;box-shadow:0 0 0 5px rgba(255,213,74,.25),0 0 18px rgba(255,213,74,.5);
  z-index:2147483646;pointer-events:none;transition:all .35s;opacity:0}`
const ensureOverlay = () =>
  page
    .evaluate((css) => {
      if (document.getElementById('wt-cap')) return
      const style = document.createElement('style')
      style.textContent = css
      document.head.appendChild(style)
      const cap = document.createElement('div')
      cap.id = 'wt-cap'
      cap.className = 'wt-bottom'
      const ring = document.createElement('div')
      ring.id = 'wt-ring'
      const cursor = document.createElement('div')
      cursor.id = 'wt-cursor'
      cursor.innerHTML =
        '<svg viewBox="0 0 24 24" width="28" height="28"><path d="M4 2 L4 20 L9 15 L12.5 22 L15.5 20.6 L12 13.8 L19 13.8 Z" fill="#fff" stroke="#000" stroke-width="1.5" stroke-linejoin="round"/></svg>'
      document.body.append(cap, ring, cursor)
    }, OVERLAY_CSS)
    .catch(() => {})

// Show a caption long enough to read it (or at least `seconds`). It goes on the other side of the
// screen from the last thing pointed at, so it doesn't cover what it's talking about.
let lastBox = null
const say = async (title, text, { seconds = 0, top } = {}) => {
  if (top === undefined) top = !!lastBox && lastBox.y + lastBox.height / 2 > 560
  await ensureOverlay()
  log(`${title}: ${text}`)
  await page.evaluate(
    ({ title, text, top }) => {
      const cap = document.getElementById('wt-cap')
      cap.className = top ? 'wt-top' : 'wt-bottom'
      cap.innerHTML = ''
      const small = document.createElement('small')
      small.textContent = title
      const body = document.createElement('span')
      body.textContent = text
      cap.append(small, body)
      cap.style.opacity = '1'
    },
    { title, text, top },
  )
  const words = text.split(/\s+/).length
  await wait(Math.max(seconds * 1000, 1500 + words * 330))
}
const hideCaption = () =>
  page.evaluate(() => {
    const cap = document.getElementById('wt-cap')
    if (cap) cap.style.opacity = '0'
  })

const moveCursor = async (x, y) => {
  await ensureOverlay()
  await page.evaluate(
    ({ x, y }) => {
      const c = document.getElementById('wt-cursor')
      c.style.left = `${x - 4}px`
      c.style.top = `${y - 2}px`
    },
    { x, y },
  )
  await page.mouse.move(x, y, { steps: 10 })
  await wait(800)
}
const ring = (box) =>
  page.evaluate((b) => {
    const r = document.getElementById('wt-ring')
    if (!r) return
    if (!b) {
      r.style.opacity = '0'
      return
    }
    Object.assign(r.style, {
      left: `${b.x - 6}px`,
      top: `${b.y - 6}px`,
      width: `${b.width + 12}px`,
      height: `${b.height + 12}px`,
      opacity: '1',
    })
  }, box)

// Move to an element, highlight it, then click it (or just point at it)
const point = async (locator, { click = true, hold = 900, after = 700 } = {}) => {
  await clearToasts() // a toast must never sit over what we click
  const target = locator.first()
  await target.waitFor({ state: 'visible', timeout: 20000 })
  await target.scrollIntoViewIfNeeded().catch(() => {})
  const box = await target.boundingBox()
  lastBox = box
  await ensureOverlay()
  await moveCursor(box.x + Math.min(box.width / 2, 60), box.y + box.height / 2)
  await ring(box)
  await wait(hold)
  if (click) {
    await page.evaluate(() => document.getElementById('wt-cursor')?.classList.add('wt-press'))
    await target.click()
    await page.evaluate(() => document.getElementById('wt-cursor')?.classList.remove('wt-press'))
  }
  await wait(after)
  await ring(null)
}
const typeInto = async (locator, text) => {
  await point(locator)
  await locator.first().press('Control+a')
  await locator.first().pressSequentially(text, { delay: 85 })
  await wait(600)
}

// COSMOS toasts can sit over the File/View menus
const clearToasts = async () => {
  for (const b of await page.locator('button:visible', { hasText: /^\s*(Dismiss|Acknowledge)\s*$/ }).all()) {
    await b.click({ timeout: 1000 }).catch(() => {})
  }
}
const closeDialog = async () => {
  await page.keyboard.press('Escape')
  await wait(700)
}
const menu = async (name, item) => {
  await clearToasts()
  await point(page.locator('.v-btn', { hasText: name }))
  await point(page.locator('.v-overlay .v-list-item', { hasText: item }))
}
const openFlow = async (name) => {
  if (!(await page.locator('.fa-view-toggle .v-btn--active', { hasText: 'Flows' }).count())) {
    await point(page.locator('.fa-view-toggle button', { hasText: 'Flows' }))
  }
  const input = page.locator('.fa-flow-select input')
  await point(input)
  await input.pressSequentially(name.replace('Demo: ', ''), { delay: 70 })
  await wait(500)
  await point(page.locator('.v-overlay .v-list-item', { hasText: name }))
  await page.locator('.vue-flow__node').first().waitFor()
  await wait(1200)
}
const node = (id) => page.locator(`.vue-flow__node[data-id="${id}"] .fa-header`)
const show = (which) => page.locator('.fa-layout-toggle button', { hasText: which })
const runStatus = () => page.locator('.fa-run-head .v-chip').first().innerText().catch(() => '')
// Wait for the run being viewed to end. With approve, answers its approval cards with Go (the
// approvals section answers its own, with captions)
let explainedDryApproval = false
const waitRunEnd = async (timeoutMs = 120000, { approve = false } = {}) => {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    const s = (await runStatus()).trim()
    if (s && s !== 'running') return s
    const card = page.locator('.fa-approval').first()
    if (approve && (await card.isVisible().catch(() => false))) {
      if (!explainedDryApproval) {
        await point(card, { click: false })
        await say('Approvals still ask', 'In a dry run approvals still ask, so the operator can rehearse them too. This step is marked "ask first".')
        explainedDryApproval = true
      }
      await point(card.getByRole('button', { name: 'Go', exact: true }))
      await wait(800)
      continue
    }
    await page.waitForTimeout(500)
  }
  return 'timeout'
}

// ---- engine API (only for reading state and putting it back) -------------------------------
let token = null
let savedAlerts = null
const api = async (method, path, body) => {
  const res = await page.request.fetch(`${BASE}/flow-automator/api${path}?scope=DEFAULT`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
  })
  return res.json()
}

// Each section recovers on its own, so one problem doesn't end the recording
const section = async (name, fn) => {
  log(`=== ${name}`)
  try {
    await fn()
  } catch (e) {
    log(`!!! ${name} failed: ${e.message.split('\n')[0]}`)
    await page.keyboard.press('Escape').catch(() => {})
    await ring(null).catch(() => {})
  }
}

// =============================================================================================
try {
  await setWindow('fullscreen')
  // Log in unless this browser already has a COSMOS session (the login page then redirects away)
  await page.goto(`${BASE}/`)
  await page.waitForLoadState('networkidle').catch(() => {})
  const loggedIn = await page.evaluate(() => !!localStorage.openc3Token)
  if (!loggedIn) {
    await page.goto(`${BASE}/login`)
    await page.getByLabel('Password').fill(PASSWORD)
    await page.locator('button:has-text("Login")').click()
    await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30000 })
  }
  token = await page.evaluate(() => localStorage.openc3Token)
  // COSMOS alert toasts would cover the screen and block clicks: off for the recording, put back after
  savedAlerts = await api('GET', '/alerts')
  await api('PUT', '/alerts', { ...savedAlerts, cosmos: { ...savedAlerts.cosmos, enabled: false } })
  await page.evaluate(() => localStorage.removeItem('openc3.flowautomator.layout')) // default layout
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-toolbar').waitFor({ timeout: 60000 })
  await wait(1500)
  await obs('StartRecord')
  for (let i = 5; i > 0; i--) {
    await say('Flow Automator walkthrough', `Starting in ${i}…`, { seconds: 1 / SPEED })
  }

  // ---- 1. What it is -----------------------------------------------------------------------
  await section('intro', async () => {
    await say(
      'Flow Automator',
      'A COSMOS plugin for automating operations as flowcharts: triggers, commands, waits, checks and operator approvals, wired together and run by a server-side engine.',
    )
    await point(page.locator('.fa-view-toggle'), { click: false })
    await say('Two views', 'Flows is the editor and live view of runs. Contacts plans satellite passes and schedules flows into them.')
    await point(page.locator('.fa-engine-chip'), { click: false })
    await say('Engine', 'The flow engine runs inside COSMOS as a microservice, so armed flows keep watching telemetry even with this page closed.')
    await point(page.locator('.fa-toolbar button', { hasText: 'Stop all' }), { click: false })
    await say('Stop all', 'Stop all is always here: it disarms every flow, stops every run and pauses the contact scheduler.')
  })

  // ---- 2. Load the demo --------------------------------------------------------------------
  let activityStart = null
  await section('load demo', async () => {
    await say('The demo', 'Everything in this walkthrough comes from the built-in demo. File, Load Demo sets it up on any COSMOS with the demo INST target.')
    await menu('File', 'Load Demo')
    await page.locator('.fa-contacts').waitFor({ timeout: 60000 })
    await wait(2500)
    await clearToasts()
    const status = await api('GET', '/status')
    activityStart = status.contacts?.next_activity?.start || null
    log('first downlink starts at', activityStart, 'in', activityStart && Math.round(activityStart - Date.now() / 1000), 's')
    await say(
      'Loaded',
      'It added two demo satellites, three ground stations, six flows, a scheduling rule, and a station placed under the first satellite so a real pass begins in a few minutes.',
    )
  })

  // ---- 3. Contacts -------------------------------------------------------------------------
  await section('contacts tour', async () => {
    await point(page.locator('.fa-satellite', { hasText: 'DEMOSAT-1' }), { click: false })
    await say('Satellites', 'Satellites are tracked from their TLEs. Passes are predicted in the engine, for every satellite over every station, a week ahead.')
    await point(page.locator('.fa-station', { hasText: 'Demo Pass Site' }), { click: false })
    await say('Ground stations', 'Each station has a location, an elevation mask and a capacity: how many passes it can work at once.')
    await point(page.locator('.fa-timeline'), { click: false })
    await say('Timeline', 'The timeline shows the predicted passes per station. Coloured blocks inside a pass are activities scheduled into it.')
    await point(page.locator('.fa-contacts-center button', { hasText: '24h' }))
    await wait(1500)
    await point(page.locator('.fa-contacts-center button', { hasText: '6h' }))
    await point(page.locator('.fa-rule').first(), { click: false })
    await say('Rules', "The rule 'DEMOSAT-1 downlinks' schedules the Pass downlink flow into every DEMOSAT-1 pass above 20 degrees, starting 60 seconds before AOS.")
    await point(page.locator('.fa-activities tbody tr').first(), { click: false })
    await say('Activities', 'Activities are flows scheduled into a pass: a snapshot of the flow, its parameters, when to start relative to AOS, TCA or LOS, and the mode to run in.')
    await point(page.locator('.fa-rehearse').first(), { click: false })
    await say('Rehearse', 'Rehearse dry-runs an activity with its real pass and parameters, sending nothing, so you can check it before the satellite arrives.')
  })

  await section('schedule an activity', async () => {
    const row = page.locator('.fa-contact-list tbody tr', { hasText: 'DEMOSAT-2' }).first()
    await row.scrollIntoViewIfNeeded()
    await point(row, { click: false })
    await say('Upcoming passes', 'Every predicted pass is listed with its times and peak elevation. Schedule puts a flow template into one.')
    await point(row.locator('.fa-schedule-btn'))
    await page.locator('.fa-activity-dialog').waitFor()
    const template = page.locator('.fa-template-select input')
    await point(template)
    await template.pressSequentially('Pass downlink', { delay: 70 })
    await point(page.locator('.v-overlay .v-list-item', { hasText: 'Demo: Pass downlink' }))
    await page.locator('[data-param="collect_type"]').waitFor()
    await point(page.locator('.fa-activity-dialog [data-param="collect_type"]'), { click: false })
    await say('Parameters', 'Templates declare parameters: a choice, a number, even a telemetry item with a picker. Each scheduled activity fills them in.')
    await point(page.locator('.fa-start-preview'), { click: false })
    await say('Start time', 'The start is anchored to the pass, here 60 seconds before AOS, and previewed in UTC.')
    await point(page.locator('.fa-activity-save'))
    await page.locator('.fa-activity-dialog').waitFor({ state: 'detached', timeout: 10000 }).catch(() => {})
    await wait(1200)
    await say('Scheduled', 'It is now in the activities table and on the timeline, and the engine will start it on its own.')
  })

  // ---- The editor, layout and a dry run: done while waiting for the pass when there's time --
  let editorDone = false
  const editorSections = async () => {
    editorDone = true
    await section('editor tour', async () => {
      await openFlow('Demo: Health check')
      await say('A flow', 'Health check reads a whole packet, summarizes it with functions, branches on the result, and asks before acting.')
      await point(page.locator('.fa-palette'), { click: false, hold: 600 })
      await say('Add nodes', 'In Edit mode the palette lists every node type: triggers, logic, actions, waits, contact nodes and flow control. Drag one onto the canvas, or filter.')
      await typeInto(page.locator('.fa-palette-filter input'), 'wait')
      await wait(1200)
      await page.locator('.fa-palette-filter input').fill('')
      await point(node('i'))
      await say('Node settings', 'Selecting a node opens its settings on the right. Conditions use COSMOS item syntax, here: hottest is over 30.')
      await point(page.locator('.fa-inspector .mdi-satellite-variant').first())
      await page.locator('.v-dialog').last().waitFor()
      await say('Telemetry picker', 'Any field can pick targets, packets and items from COSMOS instead of typing them.')
      await closeDialog()
      await point(node('c'))
      await point(page.locator('.fa-build-command'))
      await page.locator('.v-dialog').last().waitFor()
      await say('Command builder', "Send command nodes use the COSMOS command editor, with every parameter and its limits.")
      await closeDialog()
      await point(page.locator('.fa-edit-toggle button', { hasText: 'View' }))
      await say('View mode', 'When you are only watching, View hides the palette and settings and locks the canvas so nothing is moved by accident.')
      await point(show('Flow'))
      await say('Show: Flow', 'Show switches between the flow alone, the flow with its log, or the log alone. The folded log keeps its tabs and a Show log button.')
      await point(show('Log'))
      await wait(1500)
      await point(show('Both'))
    })
  
    await section('dry run', async () => {
      await point(page.locator('.fa-dry-run-btn'))
      await page.locator('.fa-run-dialog').waitFor()
      await say('Dry run', 'A dry run checks every command against the COSMOS definitions without sending it. Waits can be skipped to rehearse quickly.')
      await point(page.locator('.fa-run-go'))
      await waitRunEnd(90000, { approve: true })
      await wait(1000)
      await point(page.locator('.fa-steps-table'), { click: false })
      await say('Values flow through', 'The log shows the values read from the packet, the summary computed from them, and which way the flow branched. A hot reading would show the command it would have sent.')
    })
  }
  // Roughly how long editorSections takes at this SPEED (fixed waits included)
  const EDITOR_SECONDS = 150 * SPEED + 20

  // Fill time until the live downlink is about to start
  await section('before the pass', async () => {
    if (!activityStart) return
    let left = activityStart - Date.now() / 1000
    if (left > 70) {
      await point(page.locator('.fa-tle-inputs-btn'))
      await page.locator('.v-dialog').last().waitFor()
      await say('TLE inputs', 'TLEs can be pasted, uploaded, or fed by other apps: an API, a COSMOS stash key, files in a bucket, or a telemetry packet. Updates that jump are held for review.')
      await closeDialog()
    }
    left = activityStart - Date.now() / 1000
    if (left > EDITOR_SECONDS + 25) {
      await say('While we wait', `The first DEMOSAT-1 downlink starts in about ${Math.round(left / 60)} minutes. Until then, a look at the flow editor.`)
      await editorSections()
      await point(page.locator('.fa-view-toggle button', { hasText: 'Contacts' }))
      await page.locator('.fa-activities').waitFor()
    }
    left = activityStart - Date.now() / 1000
    if (left > 20) {
      await say('Waiting for the pass', `The first DEMOSAT-1 downlink starts in about ${Math.round(left)} seconds. Let's watch it run.`)
      while (activityStart - Date.now() / 1000 > 8) await page.waitForTimeout(1000)
    }
  })

  // ---- 4. The live downlink ----------------------------------------------------------------
  await section('live downlink', async () => {
    const live = page.locator('.fa-activities tbody tr', { hasText: 'DEMOSAT-1 downlinks' }).first()
    await point(live.locator('.fa-activity-status'), { click: false })
    await say('Running on its own', 'Nobody pressed anything: the scheduler started this downlink 60 seconds before the satellite rises.')
    await live.locator('.fa-activity-status', { hasText: 'running' }).waitFor({ timeout: 90000 }).catch(() => {})
    await point(live.locator('button[title="Open the run"]'))
    await page.locator('.vue-flow__node').first().waitFor()
    await wait(1500)
    await say('Live run', 'Opening the run shows the template with each step lit up as it runs. It is waiting for AOS, the moment the satellite comes over the horizon.')
    await point(page.locator('.v-tab', { hasText: 'Run log' }), { click: false })
    await say('Run log', 'Below, the run log lists every step with its output and details, and the variables on the right, including the contact times.')
    const status = await waitRunEnd(240000, { approve: true })
    await say('Done', `The downlink finished (${status}): it waited for AOS, waited for the first packet, sent the collect, checked it and reported.`)
    await point(page.locator('text=Notifications').first(), { click: false })
    await say('Notify', 'Its Notify step sends a report through alerts: the COSMOS notification bell, or webhooks to Slack or Teams.')
  })

  if (!editorDone) await editorSections()

  // ---- 6. Approvals ------------------------------------------------------------------------
  await section('approvals', async () => {
    await openFlow('Demo: Collect with approvals')
    await point(page.locator('.fa-mode-toggle'), { click: false })
    await say('Modes', 'Every flow runs in crawl, walk or run mode. Crawl asks before every step, walk asks only where marked and for hazardous commands, run asks nothing.')
    await point(page.locator('.fa-run-row button', { hasText: 'Run now' }))
    let shownAllRunning = false
    let minimized = false
    for (let i = 0; i < 12; i++) {
      const card = page.locator('.fa-approval').first()
      const appeared = await card.waitFor({ timeout: 15000 }).then(() => true).catch(() => false)
      if (!appeared) break
      await wait(600)
      const text = await card.innerText()
      if (!minimized) {
        await say('Approval', 'Crawl mode stops before each step. The card shows exactly what the step will do: Go, Go and run the rest, or Abort.')
        await point(page.locator('.fa-approvals-minimize'))
        await say('Minimize', 'Cards can be minimized when they cover the flow. A new approval opens them again.')
        await point(page.locator('.fa-approvals-restore'))
        minimized = true
      }
      if (!shownAllRunning) {
        await point(page.locator('.fa-engine-chip'))
        await say('All running', 'The engine chip opens All running: every run in every flow, and which ones are waiting for an operator. Click a row to jump to it.')
        await point(page.locator('.v-tab', { hasText: 'Run log' }))
        shownAllRunning = true
      }
      if (/HAZARDOUS/.test(text)) {
        await say('Hazardous', 'Hazardous commands and states are shown in red, and they always ask, even after Go and run the rest.')
      }
      if (/Question/.test(text)) {
        await say('Ask operator', 'Ask operator puts a question to the operator; the answer becomes a variable the flow branches on.')
        await point(card.getByRole('button', { name: 'Yes', exact: true }))
      } else {
        await point(card.getByRole('button', { name: 'Go', exact: true }))
      }
      await wait(1200)
    }
    const status = await waitRunEnd(30000)
    await say('Approved', `The run is ${status}. Every decision, and who made it, is in the run log and the audit log.`)
  })

  // ---- 7. Parallel and scripts ---------------------------------------------------------------
  await section('parallel', async () => {
    await openFlow('Demo: Parallel checks')
    await say('Parallel', 'Parallel starts every branch at once, each with its own variables, and Join waits for all of them.')
    await point(page.locator('.fa-run-row button', { hasText: 'Run now' }))
    await waitRunEnd(30000, { approve: true })
    await wait(1000)
    await point(page.locator('.fa-vars'), { click: false })
    await say('Merged', 'At the Join the branches merge: attitude, power and the settled flag all arrive in one set of variables.')
  })

  await section('script result', async () => {
    await openFlow('Demo: Script result')
    await say('Scripts', 'Run script starts a Script Runner script. This one measures the temperatures and hands the result back to the flow.')
    await point(page.locator('.fa-run-row button', { hasText: 'Run now' }))
    await waitRunEnd(90000, { approve: true })
    await wait(1000)
    await point(page.locator('.fa-vars'), { click: false })
    await say('Result', 'The script returned its result with stash_set, and the next step used it.')
  })

  // ---- 8. Triggers, history, audit, alerts ---------------------------------------------------
  await section('telemetry trigger', async () => {
    await openFlow('Demo: Hot temperature watch')
    await point(page.locator('.fa-dry-mode-chip'), { click: false })
    await say('Armed in dry-run mode', 'This flow is armed on live telemetry: when TEMP1 goes over 40 it fires. In dry-run mode it shows what it would do and sends nothing, a safe way to commission a trigger.')
    await menu('View', 'Run History')
    await page.locator('.fa-rh-table').waitFor()
    await say('Run history', 'Run history lists every run of every flow, filterable by flow, status, trigger and dry run.')
    await point(page.locator('.fa-rh-filters .v-select', { hasText: 'Dry runs' }))
    await point(page.locator('.v-overlay .v-list-item', { hasText: 'Only dry runs' }).last())
    await wait(2000)
    await closeDialog()
  })

  await section('versions', async () => {
    await openFlow('Demo: Health check')
    await point(page.locator('.fa-edit-toggle button', { hasText: 'Edit' }))
    await point(node('i'))
    await typeInto(page.locator('.fa-inspector textarea').first(), 'hottest > 35')
    await say('Edit and save', 'Raise the threshold, then save with a note. Every save is a new version.')
    await menu('File', 'Save With Note')
    await typeInto(page.locator('[data-field="save-note"] textarea').first(), 'Raise the hot threshold to 35')
    await point(page.locator('.fa-note-save'))
    await page.locator('.fa-version-chip', { hasText: 'v2' }).waitFor({ timeout: 10000 })
    await point(page.locator('.fa-version-chip'))
    await page.locator('.fa-history-version').first().waitFor()
    await say('History', 'History lists every version with who saved it and why.')
    await point(page.locator('.fa-history-version[data-version="1"]'))
    await wait(1200)
    await say('Compare and restore', 'Picking a version shows exactly what changed, and it can be restored as a new version.')
    await closeDialog()
    await menu('View', 'Audit Log')
    await page.locator('.fa-audit').waitFor()
    await say('Audit log', 'The audit log records who did what: saves, arming, approvals, scheduling, Stop all.')
    await closeDialog()
    await menu('View', 'Alerts')
    await page.locator('.fa-alerts').waitFor()
    await say('Alerts', 'Alerts go to the COSMOS bell and to webhooks such as Slack or Teams, for the events that need someone: approvals waiting, failed runs, missing telemetry.')
    await closeDialog()
  })

  // ---- 9. Stop all, then put things back -----------------------------------------------------
  await section('stop all', async () => {
    await point(page.locator('.fa-toolbar button', { hasText: 'Stop all' }))
    await say('Stop all', 'Stop all asks first, then disarms every flow, stops every run and pauses the scheduler.')
    await point(page.getByRole('button', { name: 'Stop all' }).last())
    await wait(1500)
    await clearToasts()
    await point(page.locator('.fa-view-toggle button', { hasText: 'Contacts' }))
    await point(page.locator('.fa-resume'))
    await say('Resume', 'The scheduler stays paused until someone resumes it.')
    await openFlow('Demo: Hot temperature watch')
    await point(page.locator('.fa-run-row .v-switch'))
    await page.locator('.fa-run-row span', { hasText: /^\s*Armed\s*$/ }).waitFor({ timeout: 10000 }).catch(() => {})
    await say('Re-armed', 'And flows are armed again one by one, on purpose.')
  })

  await say('That was Flow Automator', 'Flows, live runs, approvals, dry runs, scheduled passes, history and audit, all in one COSMOS plugin.', { seconds: 6 })
  await hideCaption()
  await wait(2000)
  await obs('StopRecord')
} finally {
  // Whatever happened, leave the scheduler running and the demo's watch armed
  try {
    if (token) {
      if (savedAlerts) await api('PUT', '/alerts', savedAlerts).catch(() => {})
      await api('POST', '/scheduler/resume', { reason: 'Walkthrough finished' }).catch(() => {})
      const flows = await api('GET', '/flows').catch(() => [])
      const watch = Array.isArray(flows) && flows.find((f) => f.name === 'Demo: Hot temperature watch')
      if (watch && !watch.armed) await api('POST', `/flows/${watch.id}/arm`, {}).catch(() => {})
    }
  } catch {}
  await page.evaluate(() => ['wt-cap', 'wt-ring', 'wt-cursor'].forEach((id) => document.getElementById(id)?.remove())).catch(() => {})
  if (obsRecording) await obs('StopRecord') // stopped early: still end the recording
  await setWindow('normal')
  log('walkthrough finished')
  if (HEADLESS) await context.close()
  await browser.close() // over CDP this only disconnects; the Chrome window stays open
}
