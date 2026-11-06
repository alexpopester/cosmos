/*
# Node types for the Flow Automator editor. This mirrors NODE_TYPES in the
# engine (lib/flow_automator/graph.py): keep the type names, output handles
# and data fields in sync.
*/

export const CATEGORIES = [
  { key: 'trigger', label: 'Triggers', color: '#d4a017' },
  { key: 'logic', label: 'Logic', color: '#9c6ade' },
  { key: 'action', label: 'Actions', color: '#3d8fd6' },
  { key: 'wait', label: 'Waits', color: '#2bb3a3' },
  { key: 'contact', label: 'Contacts', color: '#e07b39' },
  { key: 'flow', label: 'Flow control', color: '#8a96a3' },
]

const LIMITS_STATES = [
  'RED',
  'RED_LOW',
  'RED_HIGH',
  'YELLOW',
  'YELLOW_LOW',
  'YELLOW_HIGH',
  'GREEN',
  'GREEN_LOW',
  'GREEN_HIGH',
  'BLUE',
  'STALE',
]

const CONDITION_HINT =
  "COSMOS check syntax or an expression, e.g. INST HEALTH_STATUS TEMP1 > 50 and count < 3. Use tlm_raw('TGT PKT ITEM') for raw values."
const TEMPLATE_HINT = 'Use {{ expression }} to insert variables or telemetry'

// Execution modes, most cautious first (graph.py MODES)
export const MODES = [
  {
    value: 'crawl',
    title: 'Crawl',
    icon: 'mdi-tortoise',
    hint: 'An operator approves every step before it runs',
  },
  {
    value: 'walk',
    title: 'Walk',
    icon: 'mdi-walk',
    hint: "An operator approves hazardous commands and steps marked 'Ask operator first'",
  },
  {
    value: 'run',
    title: 'Run',
    icon: 'mdi-run-fast',
    hint: "No operator approval. Hazardous commands need the node's 'Allow hazardous'",
  },
]

export const NODE_TYPES = {
  trigger_manual: {
    category: 'trigger',
    label: 'Manual',
    icon: 'mdi-gesture-tap-button',
    description: 'Start with Run now, or when called as a sub-flow',
    trigger: true,
    outputs: ['out'],
    fields: [],
    summary: () => 'Run now / sub-flow',
  },
  trigger_tlm: {
    category: 'trigger',
    label: 'Telemetry',
    icon: 'mdi-flash',
    description: 'Fires when a condition goes from false to true',
    trigger: true,
    outputs: ['out'],
    fields: [
      {
        key: 'condition',
        label: 'Condition',
        type: 'condition',
        default: 'INST HEALTH_STATUS TEMP1 > 50',
        hint: CONDITION_HINT,
      },
      {
        key: 'cooldown',
        label: 'Cooldown (s)',
        type: 'number',
        default: 0,
        hint: 'Minimum time between firings',
      },
    ],
    summary: (d) => d.condition,
  },
  trigger_schedule: {
    category: 'trigger',
    label: 'Schedule',
    icon: 'mdi-calendar-clock',
    description: 'Fires every N seconds or on a cron schedule (UTC)',
    trigger: true,
    outputs: ['out'],
    fields: [
      {
        key: 'mode',
        label: 'Mode',
        type: 'select',
        options: ['interval', 'cron'],
        default: 'interval',
      },
      {
        key: 'interval',
        label: 'Every (s)',
        type: 'number',
        default: 60,
        show: (d) => d.mode !== 'cron',
      },
      {
        key: 'cron',
        label: 'Cron (UTC)',
        type: 'text',
        default: '0 * * * *',
        hint: 'minute hour day-of-month month day-of-week',
        show: (d) => d.mode === 'cron',
      },
    ],
    summary: (d) =>
      d.mode === 'cron' ? `cron ${d.cron}` : `every ${d.interval}s`,
  },
  trigger_packet: {
    category: 'trigger',
    label: 'Packet received',
    icon: 'mdi-package-down',
    description: 'Fires each time a telemetry packet arrives',
    trigger: true,
    outputs: ['out'],
    fields: [
      {
        key: 'target',
        packetKey: 'packet',
        label: 'Packet',
        type: 'packet',
        default: 'INST',
      },
      { key: 'packet', type: 'hidden', default: 'ADCS' },
    ],
    summary: (d) => `${d.target} ${d.packet}`,
  },
  trigger_limits: {
    category: 'trigger',
    label: 'Limits',
    icon: 'mdi-alert-octagon',
    description:
      "Fires when an item's limits state changes into a chosen state",
    trigger: true,
    outputs: ['out'],
    fields: [
      {
        key: 'item',
        label: 'Item',
        type: 'item',
        default: 'INST HEALTH_STATUS TEMP1',
        hint: 'TARGET PACKET ITEM',
      },
      {
        key: 'states',
        label: 'States',
        type: 'multiselect',
        options: LIMITS_STATES,
        default: ['RED', 'RED_LOW', 'RED_HIGH'],
      },
    ],
    summary: (d) => `${d.item} → ${(d.states || []).join(', ')}`,
  },
  if: {
    category: 'logic',
    label: 'If',
    icon: 'mdi-call-split',
    description: 'Branch on a condition',
    outputs: ['true', 'false'],
    errors: true,
    fields: [
      {
        key: 'condition',
        label: 'Condition',
        type: 'condition',
        default: 'INST HEALTH_STATUS TEMP1 > 50',
        hint: CONDITION_HINT,
      },
    ],
    summary: (d) => d.condition,
  },
  switch: {
    category: 'logic',
    label: 'Switch',
    icon: 'mdi-source-branch',
    description: 'Pick a branch by comparing a value to cases',
    outputs: (d) => [...(d.cases || []).map((_, i) => `case_${i}`), 'default'],
    outputLabel: (handle, d) =>
      handle.startsWith('case_')
        ? String((d.cases || [])[Number(handle.slice(5))])
        : handle,
    errors: true,
    fields: [
      {
        key: 'expression',
        label: 'Value',
        type: 'condition',
        default: 'INST HEALTH_STATUS COLLECT_TYPE',
      },
      {
        key: 'cases',
        label: 'Cases',
        type: 'list',
        default: ["'NORMAL'", "'SPECIAL'"],
        hint: "Expressions, e.g. 'NORMAL' or 5",
      },
    ],
    summary: (d) => d.expression,
  },
  loop: {
    category: 'logic',
    label: 'Loop',
    icon: 'mdi-repeat',
    description:
      'Repeat the body N times or while a condition holds. Wire the end of the body back into the loop.',
    outputs: ['body', 'done'],
    errors: true,
    fields: [
      {
        key: 'mode',
        label: 'Mode',
        type: 'select',
        options: ['count', 'while'],
        default: 'count',
      },
      {
        key: 'count',
        label: 'Count',
        type: 'number',
        default: 3,
        show: (d) => d.mode !== 'while',
      },
      {
        key: 'condition',
        label: 'While',
        type: 'condition',
        default: 'i < 3',
        show: (d) => d.mode === 'while',
      },
      {
        key: 'variable',
        label: 'Index variable',
        type: 'text',
        default: 'i',
      },
      {
        key: 'max_iterations',
        label: 'Max iterations',
        type: 'number',
        default: 10000,
        show: (d) => d.mode === 'while',
      },
    ],
    summary: (d) =>
      d.mode === 'while' ? `while ${d.condition}` : `${d.count} times`,
  },
  parallel: {
    category: 'logic',
    label: 'Parallel',
    icon: 'mdi-call-split',
    description:
      'Run every outgoing wire at the same time. Bring the branches back together with a Join.',
    outputs: ['out'],
    fields: [
      {
        key: 'isolate',
        label: 'Separate variables per branch',
        type: 'boolean',
        default: false,
        hint: "Each branch works on its own copy; at the Join their changes merge in wire order and branch_results lists each branch's changes",
      },
    ],
    summary: (d) =>
      d.isolate
        ? 'branches run together, own variables'
        : 'branches run together',
  },
  join: {
    category: 'logic',
    label: 'Join',
    icon: 'mdi-call-merge',
    description: 'Wait for all parallel branches, then continue',
    outputs: ['out'],
    fields: [],
    summary: () => 'wait for all branches',
  },
  set_var: {
    category: 'logic',
    label: 'Set variable',
    icon: 'mdi-variable',
    description: 'Store a value for later nodes',
    outputs: ['out'],
    errors: true,
    fields: [
      { key: 'name', label: 'Name', type: 'text', default: 'value' },
      {
        key: 'expression',
        label: 'Value',
        type: 'condition',
        default: 'INST HEALTH_STATUS TEMP1',
        hint: 'An expression: telemetry, other variables, functions like round(), upper(), format(x, ".2f")',
      },
      {
        key: 'more',
        label: 'More variables (in order)',
        type: 'keyvalue',
        default: {},
        hint: 'name = expression; each can use the ones before it',
      },
      {
        key: 'persist',
        label: 'Remember between runs',
        type: 'boolean',
        default: false,
        hint: "These variables start this flow's next runs (e.g. a count of passes not heard). A dry run keeps nothing.",
      },
    ],
    summary: (d) =>
      `${d.name} = ${d.expression}${Object.keys(d.more || {}).length ? ` +${Object.keys(d.more).length}` : ''}`,
  },
  read_packet: {
    category: 'logic',
    label: 'Read packet',
    icon: 'mdi-package-variant',
    description:
      "Read every item of a packet into one variable: hs['TEMP1'], hs['TEMP2'] ...",
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'target',
        packetKey: 'packet',
        label: 'Packet',
        type: 'packet',
        default: 'INST',
      },
      { key: 'packet', type: 'hidden', default: 'HEALTH_STATUS' },
      { key: 'variable', label: 'Variable', type: 'text', default: 'hs' },
      {
        key: 'value_type',
        label: 'Type',
        type: 'select',
        options: ['CONVERTED', 'RAW', 'FORMATTED', 'WITH_UNITS'],
        default: 'CONVERTED',
      },
    ],
    summary: (d) => `${d.variable} = ${d.target} ${d.packet}`,
  },
  send_cmd: {
    category: 'action',
    label: 'Send command',
    icon: 'mdi-send',
    description: 'Send a command',
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'command',
        label: 'Command',
        type: 'template',
        default: 'INST COLLECT with TYPE NORMAL, DURATION 1',
        hint: `TARGET COMMAND with PARAM value, ... ${TEMPLATE_HINT}`,
        builder: true,
      },
      {
        key: 'allow_hazardous',
        label: 'Allow hazardous',
        type: 'boolean',
        default: false,
        hint: 'Run mode: a hazardous command fails unless this is on. Crawl and walk ignore it: an operator approves every hazardous command. Turning it on disarms an armed flow.',
      },
      {
        key: 'allow_raw_values',
        label: 'Allow raw values',
        type: 'boolean',
        default: false,
        hint: "Off: a {{ }} value can't add parameters or break out of quotes (a comma or quote in it fails the step). Only turn on if the value is meant to be command text.",
      },
    ],
    summary: (d) => d.command,
  },
  run_script: {
    category: 'action',
    label: 'Run script',
    icon: 'mdi-script-text-play',
    description: 'Start a script in Script Runner',
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'filename',
        label: 'Script',
        type: 'script',
        default: '',
        hint: 'e.g. INST/procedures/checks.rb',
      },
      {
        key: 'wait',
        label: 'Wait for it to finish',
        type: 'boolean',
        default: true,
      },
      {
        key: 'timeout',
        label: 'Timeout (s, 0 = none)',
        type: 'number',
        default: 0,
        show: (d) => d.wait !== false,
      },
      {
        key: 'allow_errors',
        label: 'Treat "completed with errors" as success',
        type: 'boolean',
        default: false,
        show: (d) => d.wait !== false,
      },
      {
        key: 'environment',
        label: 'Environment',
        type: 'keyvalue',
        default: {},
        hint: TEMPLATE_HINT,
      },
      {
        key: 'result_variable',
        label: 'Result variable',
        type: 'text',
        default: 'script_result',
        hint: "The script hands back a value with stash_set(os.environ['FLOW_RESULT_KEY'], value) (Ruby: ENV['FLOW_RESULT_KEY'])",
        show: (d) => d.wait !== false,
      },
    ],
    summary: (d) => d.filename || '(no script)',
  },
  set_tlm: {
    category: 'action',
    label: 'Set telemetry',
    icon: 'mdi-pencil',
    description: 'Set a telemetry value in the current value table',
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'item',
        label: 'Item',
        type: 'item',
        default: 'INST HEALTH_STATUS TEMP1',
        hint: 'TARGET PACKET ITEM',
      },
      {
        key: 'value',
        label: 'Value',
        type: 'template',
        default: '0',
        hint: TEMPLATE_HINT,
      },
      {
        key: 'value_type',
        label: 'Type',
        type: 'select',
        options: ['CONVERTED', 'RAW', 'FORMATTED'],
        default: 'CONVERTED',
      },
    ],
    summary: (d) => `${d.item} = ${d.value}`,
  },
  log: {
    category: 'action',
    label: 'Log',
    icon: 'mdi-message-text',
    description: 'Write a message to the COSMOS log',
    outputs: ['out'],
    fields: [
      {
        key: 'message',
        label: 'Message',
        type: 'template',
        default: 'Hello from {{ i }}',
        hint: TEMPLATE_HINT,
      },
      {
        key: 'level',
        label: 'Level',
        type: 'select',
        options: ['info', 'warn', 'error'],
        default: 'info',
      },
    ],
    summary: (d) => d.message,
  },
  notify: {
    category: 'action',
    label: 'Notify',
    icon: 'mdi-bell-ring',
    description:
      'Alert operators: the COSMOS notification bell and any webhooks set up in View > Alerts',
    outputs: ['out'],
    fields: [
      {
        key: 'title',
        label: 'Title',
        type: 'template',
        default: '',
        hint: "Defaults to the flow's name",
      },
      {
        key: 'message',
        label: 'Message',
        type: 'template',
        default: 'Pass complete for {{ contact_satellite }}',
        hint: TEMPLATE_HINT,
      },
      {
        key: 'level',
        label: 'Level',
        type: 'select',
        options: ['notification', 'alert'],
        default: 'notification',
        hint: 'Alerts also pop up in every COSMOS tool until read',
      },
    ],
    summary: (d) => `${d.level === 'alert' ? '🔴 ' : ''}${d.message}`,
  },
  wait_time: {
    category: 'wait',
    label: 'Wait',
    icon: 'mdi-timer-sand',
    description: 'Pause for a number of seconds',
    outputs: ['out'],
    fields: [{ key: 'seconds', label: 'Seconds', type: 'number', default: 1 }],
    summary: (d) => `${d.seconds}s`,
  },
  wait_tlm: {
    category: 'wait',
    label: 'Wait for telemetry',
    icon: 'mdi-timer-sync',
    description: 'Wait until a condition is true, or time out',
    outputs: ['ok', 'timeout'],
    errors: true,
    fields: [
      {
        key: 'condition',
        label: 'Condition',
        type: 'condition',
        default: "INST HEALTH_STATUS COLLECT_TYPE == 'NORMAL'",
        hint: CONDITION_HINT,
      },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 10 },
      { key: 'poll', label: 'Poll (s)', type: 'number', default: 0.25 },
    ],
    summary: (d) => `${d.condition} (${d.timeout}s)`,
  },
  wait_check: {
    category: 'wait',
    label: 'Wait check',
    icon: 'mdi-timer-check',
    description: 'Wait until a condition is true; fail if it times out',
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'condition',
        label: 'Condition',
        type: 'condition',
        default: 'INST HEALTH_STATUS TEMP1 < 50',
        hint: CONDITION_HINT,
      },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 10 },
      { key: 'poll', label: 'Poll (s)', type: 'number', default: 0.25 },
    ],
    summary: (d) => `${d.condition} (${d.timeout}s)`,
  },
  wait_packet: {
    category: 'wait',
    label: 'Wait for packet',
    icon: 'mdi-package-down',
    description:
      'Wait until a new packet arrives (its count goes up), e.g. the first telemetry after AOS',
    outputs: ['received', 'timeout'],
    errors: true,
    fields: [
      {
        key: 'target',
        packetKey: 'packet',
        label: 'Packet',
        type: 'packet',
        default: 'INST',
      },
      { key: 'packet', type: 'hidden', default: 'HEALTH_STATUS' },
      { key: 'count', label: 'New packets', type: 'number', default: 1 },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 60 },
    ],
    summary: (d) => `${d.target} ${d.packet} (${d.timeout}s)`,
  },
  wait_contact: {
    category: 'contact',
    label: 'Wait for contact',
    icon: 'mdi-satellite-uplink',
    description:
      "Wait until a time relative to the run's contact (AOS, TCA or LOS). Only in runs of scheduled activities.",
    outputs: ['out'],
    errors: true,
    fields: [
      {
        key: 'anchor',
        label: 'Wait until',
        type: 'select',
        options: ['aos', 'tca', 'los'],
        default: 'aos',
        hint: 'AOS: satellite rises over the mask. TCA: closest approach. LOS: it sets.',
      },
      {
        key: 'offset',
        label: 'Offset (s)',
        type: 'number',
        default: 0,
        hint: 'Negative for before, e.g. -10 is 10 seconds before',
      },
    ],
    summary: (d) => {
      const offset = Number(d.offset) || 0
      return `${String(d.anchor || 'aos').toUpperCase()}${offset ? (offset > 0 ? ' + ' : ' - ') + Math.abs(offset) + 's' : ''}`
    },
  },
  contact_check: {
    category: 'contact',
    label: 'Contact time left',
    icon: 'mdi-timer-sand',
    description:
      'Branch on how much of the contact is left: ok if at least this many seconds, else short',
    outputs: ['ok', 'short'],
    errors: true,
    fields: [
      {
        key: 'min_remaining',
        label: 'Need at least (s)',
        type: 'number',
        default: 60,
      },
    ],
    summary: (d) => `needs ${d.min_remaining}s before LOS`,
  },
  ask: {
    category: 'flow',
    label: 'Ask operator',
    icon: 'mdi-account-question',
    description:
      'Pause and ask the operator for a value (or yes/no); the answer goes into a variable',
    outputs: ['answered', 'timeout'],
    errors: true,
    fields: [
      {
        key: 'prompt',
        label: 'Question',
        type: 'template',
        default: 'How many frames to dump?',
        hint: TEMPLATE_HINT,
      },
      {
        key: 'kind',
        label: 'Answer',
        type: 'select',
        options: ['text', 'number', 'choice', 'confirm'],
        default: 'number',
      },
      {
        key: 'choices',
        label: 'Choices',
        type: 'list',
        default: [],
        show: (d) => d.kind === 'choice',
      },
      {
        key: 'min',
        label: 'Minimum',
        type: 'number',
        default: '',
        show: (d) => d.kind === 'number',
      },
      {
        key: 'max',
        label: 'Maximum',
        type: 'number',
        default: '',
        show: (d) => d.kind === 'number',
      },
      { key: 'variable', label: 'Variable', type: 'text', default: 'answer' },
      {
        key: 'default',
        label: 'Default',
        type: 'text',
        default: '',
        hint: 'Used on timeout, and in dry runs that skip waits',
      },
      {
        key: 'timeout',
        label: 'Timeout (s, 0 = none)',
        type: 'number',
        default: 0,
      },
    ],
    summary: (d) => d.prompt,
  },
  subflow: {
    category: 'flow',
    label: 'Sub-flow',
    icon: 'mdi-sitemap',
    description:
      'Run another flow from its Manual trigger and wait for it. Variables go in and come back out.',
    outputs: ['out'],
    errors: true,
    fields: [
      { key: 'flow_id', label: 'Flow', type: 'flow', default: '' },
      {
        key: 'pass_variables',
        label: 'Copy variables back',
        type: 'boolean',
        default: true,
      },
    ],
    summary: (d, ctx) =>
      (ctx && ctx.flowName && ctx.flowName(d.flow_id)) || '(choose a flow)',
  },
  end: {
    category: 'flow',
    label: 'End',
    icon: 'mdi-stop-circle',
    description: 'Finish the whole run',
    outputs: [],
    fields: [
      {
        key: 'status',
        label: 'Status',
        type: 'select',
        options: ['success', 'failed'],
        default: 'success',
      },
      {
        key: 'message',
        label: 'Message',
        type: 'template',
        default: '',
        hint: TEMPLATE_HINT,
      },
    ],
    summary: (d) => d.status,
  },
}

export function outputsFor(type, data) {
  const spec = NODE_TYPES[type]
  if (!spec) return []
  const outputs =
    typeof spec.outputs === 'function'
      ? spec.outputs(data || {})
      : [...spec.outputs]
  if (spec.errors) outputs.push('error')
  return outputs
}

export function defaultData(type) {
  const data = {}
  for (const field of NODE_TYPES[type].fields) {
    data[field.key] = JSON.parse(JSON.stringify(field.default))
  }
  return data
}

export function categoryColor(type) {
  const spec = NODE_TYPES[type]
  return CATEGORIES.find((c) => c.key === spec?.category)?.color || '#888'
}
