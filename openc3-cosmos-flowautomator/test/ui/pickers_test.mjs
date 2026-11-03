// Telemetry picker, command builder, Ask operator, Read packet, Wait for packet and script results, live.
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
const shot = (page, name) => page.screenshot({ path: `${SHOTS}/pickers_${name}.png` })

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
const waitRun = async (runId, tries = 300) => {
  for (let i = 0; i < tries; i++) {
    const [, run] = await api('GET', `/runs/${runId}`)
    if (!['pending', 'running'].includes(run.status)) return run
    await sleep(150)
  }
  throw new Error('run timed out')
}
// Pick from a COSMOS v-autocomplete inside a container
const choose = async (container, testId, text) => {
  const input = page.locator(`${container} [data-test="${testId}"] input`)
  await input.click()
  await input.fill(text)
  await page.locator('.v-overlay .v-list-item', { hasText: new RegExp(`^${text}$`) }).first().click()
  await page.waitForTimeout(500)
}
const flows = []
const SCRIPT = 'INST/procedures/flow_result_test.py'

try {
  await api('POST', '/scheduler/resume', {})
  await page.goto(`${BASE}/tools/flowautomator`)
  await page.locator('.fa-palette').waitFor({ timeout: 60000 })
  const palette = await page.locator('.fa-palette').innerText()
  check('palette has the new nodes', ['Read packet', 'Wait for packet', 'Ask operator', 'Notify'].every((l) => palette.includes(l)))

  // ---- telemetry picker in an If condition ----
  await page.getByRole('button', { name: 'New' }).click()
  await page.locator('.fa-palette-item', { hasText: /^\s*If\s*$/ }).dblclick()
  await page.locator('.vue-flow__node').nth(1).waitFor()
  await page.locator('.vue-flow__node').nth(1).locator('.fa-header').click()
  await page.locator('.fa-field-condition textarea').first().fill('')
  await page.locator('.fa-field-condition .v-field__append-inner .v-icon').click()
  await page.locator('.fa-tlm-picker').waitFor()
  await choose('.fa-tlm-picker', 'select-target', 'INST')
  await choose('.fa-tlm-picker', 'select-packet', 'HEALTH_STATUS')
  await choose('.fa-tlm-picker', 'select-item', 'TEMP2')
  await page.locator('.fa-tlm-pick').click()
  const condition = await page.locator('.fa-field-condition textarea').first().inputValue()
  check('telemetry picker inserts the item into a condition', condition.trim() === 'INST HEALTH_STATUS TEMP2', condition)
  await shot(page, '01_condition')

  // ---- command builder ----
  await page.locator('.fa-palette-item', { hasText: 'Send command' }).dblclick()
  await page.locator('.vue-flow__node').nth(2).waitFor()
  await page.locator('.vue-flow__node').nth(2).locator('.fa-header').click()
  await page.locator('.fa-build-command').click()
  await page.locator('.fa-command-builder').waitFor()
  await page.waitForTimeout(1500)
  const startTarget = await page.locator('.fa-command-builder [data-test="select-target"]').innerText()
  const startPacket = await page.locator('.fa-command-builder [data-test="select-packet"]').innerText()
  const startParams = await page.locator('.fa-command-builder .parameters-table').innerText()
  check(
    "command builder starts from the node's command",
    startTarget.includes('INST') && startPacket.includes('COLLECT') && startParams.includes('DURATION'),
    `${startTarget.split('\n').pop()} ${startPacket.split('\n').pop()}`,
  )
  await choose('.fa-command-builder', 'select-packet', 'SETPARAMS')
  await page.waitForTimeout(800)
  await shot(page, '02_builder')
  await page.locator('.fa-command-use').click()
  await page.locator('.fa-command-builder').waitFor({ state: 'detached', timeout: 5000 }).catch(() => {})
  const command = await page.locator('.fa-field-command textarea').first().inputValue()
  check('command builder writes the command into the node', command.startsWith('INST SETPARAMS with VALUE1'), command.slice(0, 80))

  // ---- packet picker on Wait for packet ----
  await page.locator('.fa-palette-item', { hasText: 'Wait for packet' }).dblclick()
  await page.locator('.vue-flow__node').nth(3).waitFor()
  await page.locator('.vue-flow__node').nth(3).locator('.fa-header').click()
  await page.locator('.fa-field-packet .v-field__append-inner .v-icon').click()
  await page.locator('.fa-tlm-picker').waitFor()
  await choose('.fa-tlm-picker', 'select-target', 'INST')
  await choose('.fa-tlm-picker', 'select-packet', 'ADCS')
  await page.locator('.fa-tlm-pick').click()
  const packetValue = await page.locator('.fa-field-packet input').inputValue()
  check('packet picker fills target and packet', packetValue === 'ADCS', packetValue)
  await page.locator('.v-btn', { hasText: 'File' }).first().click()
  await page.keyboard.press('Escape')

  // ---- live: Read packet, several variables, Wait for packet, values into a command (dry run) ----
  const [, live] = await api('POST', '/flows', {
    name: 'PICK live data',
    mode: 'run',
    nodes: [
      n('t', 'trigger_manual', 0, 0),
      n('p', 'read_packet', 260, 0, { target: 'INST', packet: 'HEALTH_STATUS', variable: 'hs' }),
      n('v', 'set_var', 520, 0, {
        name: 'avg',
        expression: "(hs['TEMP1'] + hs['TEMP2']) / 2",
        more: { label: "upper(format(avg, '.1f') + 'c')", items: "len(keys(hs))" },
      }),
      n('w', 'wait_packet', 780, 0, { target: 'INST', packet: 'HEALTH_STATUS', timeout: 10 }),
      n('c', 'send_cmd', 1040, 0, { command: "INST SETPARAMS with VALUE1 {{ round(abs(avg)) % 6 }}, VALUE2 {{ items % 6 }}" }),
    ],
    edges: [e('t', 'p'), e('p', 'v'), e('v', 'w'), e('w', 'c', 'received')],
  })
  flows.push(live.flow.id)
  let [, result] = await api('POST', `/flows/${live.flow.id}/run`, { dry_run: true })
  let run = await waitRun(result.run.id)
  const steps = Object.fromEntries(run.steps.map((s) => [s.node, s.message]))
  check('Read packet reads the whole packet', /hs = INST HEALTH_STATUS \(\d+ items\)/.test(steps.p), steps.p)
  check('Set variable sets several values with functions', steps.v.includes('label =') && steps.v.includes('C'), steps.v.slice(0, 100))
  check('Wait for packet sees a new packet arrive', run.status === 'success' && steps.w.includes('received after'), steps.w)
  check('telemetry-derived values reach the command', /would send INST SETPARAMS with VALUE1 [0-5], VALUE2 [0-5]/.test(steps.c), steps.c)

  // The dry run checks ranges from the COSMOS command definition
  const [, outOfRange] = await api('POST', '/flows', {
    name: 'PICK range',
    nodes: [n('t', 'trigger_manual', 0, 0), n('c', 'send_cmd', 260, 0, { command: 'INST SETPARAMS with VALUE1 {{ 2 * 3 }}' })],
    edges: [e('t', 'c')],
  })
  flows.push(outOfRange.flow.id)
  ;[, result] = await api('POST', `/flows/${outOfRange.flow.id}/run`, { dry_run: true })
  run = await waitRun(result.run.id)
  check('dry run catches a value outside the command\'s range', run.status === 'failed' && run.message.includes('VALUE1 = 6 is outside 0..5'), run.message)

  // ---- live: a script hands a value back ----
  const scriptText = [
    'import os',
    "temp = tlm('INST HEALTH_STATUS TEMP1')",
    "stash_set(os.environ['FLOW_RESULT_KEY'], {'temp': round(temp, 2), 'ok': True, 'flow': os.environ['FLOW_NAME']})",
  ].join('\n')
  const saved = await page.request.post(`${BASE}/script-api/scripts/${SCRIPT}?scope=DEFAULT`, {
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    data: JSON.stringify({ text: scriptText }),
  })
  check('test script saved in Script Runner', saved.ok(), String(saved.status()))
  const [, scripted] = await api('POST', '/flows', {
    name: 'PICK script result',
    mode: 'run',
    nodes: [
      n('t', 'trigger_manual', 0, 0),
      n('s', 'run_script', 260, 0, { filename: SCRIPT, wait: true, timeout: 60, result_variable: 'measured' }),
      n('l', 'log', 520, 0, { message: "temp {{ measured['temp'] }} ok {{ measured['ok'] }} from {{ measured['flow'] }}" }),
    ],
    edges: [e('t', 's'), e('s', 'l')],
  })
  flows.push(scripted.flow.id)
  ;[, result] = await api('POST', `/flows/${scripted.flow.id}/run`, {})
  run = await waitRun(result.run.id, 600)
  const logStep = run.steps.find((s) => s.node === 'l')
  check(
    'a script hands a value back to the flow',
    run.status === 'success' && /temp -?[\d.]+ ok True from PICK script result/.test(logStep?.message || ''),
    logStep?.message || run.message,
  )

  // ---- live: Ask operator in the UI ----
  const [, asking] = await api('POST', '/flows', {
    name: 'PICK ask',
    mode: 'run',
    nodes: [
      n('t', 'trigger_manual', 0, 0),
      n('q', 'ask', 260, 0, { prompt: 'Duration for the collect?', kind: 'number', variable: 'secs', min: 0, max: 10, label: 'How long' }),
      n('c', 'send_cmd', 520, 0, { command: 'INST COLLECT with TYPE NORMAL, DURATION {{ secs }}' }),
    ],
    edges: [e('t', 'q'), e('q', 'c', 'answered')],
  })
  flows.push(asking.flow.id)
  await page.locator('.fa-flow-select input').click()
  await page.locator('.fa-flow-select input').pressSequentially('PICK ask')
  await page.locator('.v-overlay .v-list-item', { hasText: 'PICK ask' }).first().click()
  await page.waitForTimeout(800)
  if (await page.locator('.v-dialog', { hasText: 'Discard' }).count()) await page.locator('.v-dialog button:has-text("Discard")').click()
  await page.locator('.vue-flow__node').nth(2).waitFor()
  await page.locator('.fa-dry-run-btn').click()
  await page.locator('.fa-run-dialog').waitFor()
  await page.locator('.fa-run-skip input').uncheck()
  await page.locator('.fa-run-go').click()
  await page.locator('.fa-ask').waitFor({ timeout: 10000 })
  const question = await page.locator('.fa-approval').first().innerText()
  check('Ask operator shows its question in the approval panel', question.includes('Duration for the collect?'), question.split('\n')[0])
  await page.locator('.fa-ask-input input').fill('42')
  await page.locator('.fa-ask-submit').click()
  await page.waitForTimeout(800)
  check('an answer outside the limits is refused', (await page.locator('.fa-ask').count()) === 1)
  await page.locator('.fa-ask-input input').fill('3')
  await shot(page, '03_ask')
  await page.locator('.fa-ask-submit').click()
  await page.locator('.fa-steps', { hasText: 'answered 3' }).waitFor({ timeout: 10000 })
  const askLog = await page.locator('.fa-steps').innerText()
  check('the answer flows into the command', askLog.includes('would send INST COLLECT with TYPE NORMAL, DURATION 3'), '')
  await shot(page, '04_answered')

  // ---- template parameters: item and command types with pickers ----
  const [, tmpl] = await api('POST', '/flows', {
    name: 'PICK params',
    mode: 'run',
    template: true,
    parameters: [
      { name: 'sensor', type: 'item', default: 'INST HEALTH_STATUS TEMP1' },
      { name: 'setup', type: 'command', default: 'INST CLEAR' },
    ],
    nodes: [
      n('t', 'trigger_manual', 0, 0),
      n('l', 'log', 260, 0, { message: '{{ sensor }} = {{ tlm(sensor) }}' }),
      n('c', 'send_cmd', 520, 0, { command: '{{ setup }}', allow_raw_values: true }),
    ],
    edges: [e('t', 'l'), e('l', 'c')],
  })
  flows.push(tmpl.flow.id)
  await page.locator('.fa-flow-select input').click()
  await page.locator('.fa-flow-select input').pressSequentially('PICK params')
  await page.locator('.v-overlay .v-list-item', { hasText: 'PICK params' }).first().click()
  await page.locator('.vue-flow__node').nth(2).waitFor()
  await page.locator('.fa-dry-run-btn').click()
  await page.locator('.fa-run-dialog [data-param="sensor"]').waitFor()
  await page.locator('.fa-run-dialog [data-param="sensor"] .v-field__append-inner .v-icon').click()
  await page.locator('.fa-tlm-picker').waitFor()
  await choose('.fa-tlm-picker', 'select-target', 'INST')
  await choose('.fa-tlm-picker', 'select-packet', 'HEALTH_STATUS')
  await choose('.fa-tlm-picker', 'select-item', 'TEMP3')
  await page.locator('.fa-tlm-pick').click()
  const sensor = await page.locator('.fa-run-dialog [data-param="sensor"] input').inputValue()
  check('item parameter has a telemetry picker', sensor === 'INST HEALTH_STATUS TEMP3', sensor)
  await page.locator('.fa-run-go').click()
  await page.locator('.fa-steps', { hasText: 'INST HEALTH_STATUS TEMP3 =' }).waitFor({ timeout: 10000 })
  const paramLog = await page.locator('.fa-steps').innerText()
  check('item and command parameters work in the flow', paramLog.includes('would send INST CLEAR'), '')
} catch (error) {
  check('unexpected error', false, error.message.split('\n')[0])
  await shot(page, 'error').catch(() => {})
} finally {
  await api('POST', '/stop_all', {}).catch(() => {})
  await api('POST', '/scheduler/resume', {}).catch(() => {})
  for (const id of flows) await api('DELETE', `/flows/${id}`).catch(() => {})
  await page.request.delete(`${BASE}/script-api/scripts/${SCRIPT}?scope=DEFAULT`, { headers: { Authorization: token } }).catch(() => {})
  await browser.close()
}
console.log(`\nConsole errors (${consoleErrors.length})`)
consoleErrors.slice(0, 10).forEach((m) => console.log('  ' + m.slice(0, 300)))
const failed = results.filter(([, ok]) => !ok).length
console.log(`${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
