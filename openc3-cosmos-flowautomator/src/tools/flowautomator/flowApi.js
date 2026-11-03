/*
# Client for the Flow Automator engine's HTTP API (the FLOW_ENGINE
# microservice, routed by Traefik at ROUTE_PREFIX).
*/

import { Api } from '@openc3/js-common/services'

export const ROUTE_PREFIX = '/flow-automator'
const BASE = `${ROUTE_PREFIX}/api`

// COSMOS shows a red error toast for any failed request unless it lists the
// status in Ignore-Errors. 401 is never ignored, so an expired session still
// goes to the login page.
//
// Actions the tool reports itself (save, run, approve ... all catch and show
// their own message): validation problems, conflicts, missing items, engine down.
const handled = {
  Accept: 'application/json',
  'Content-Type': 'application/json',
  'Ignore-Errors': '400,403,404,409,500,502,503,504',
}
// Background reads (polling status and the schedule every second or two, lists):
// while the engine restarts these fail every poll, and the tool already shows
// "Engine offline", so a toast per poll would only bury the screen.
const quiet = {
  Accept: 'application/json',
  'Ignore-Errors': '400,403,404,409,500,502,503,504',
}
const get = (path, params) =>
  Api.get(`${BASE}${path}`, { params, headers: quiet }).then(unwrap)

const unwrap = (response) => response.data
const post = (path, data = {}) =>
  Api.post(`${BASE}${path}`, { data, headers: handled }).then(unwrap)
const put = (path, data) =>
  Api.put(`${BASE}${path}`, { data, headers: handled }).then(unwrap)
const del = (path) =>
  Api.delete(`${BASE}${path}`, { headers: handled }).then(unwrap)

export default {
  status: () => get('/status'),
  listFlows: () => get('/flows'),
  getFlow: (id) =>
    Api.get(`${BASE}/flows/${id}`, { headers: handled }).then(unwrap),
  createFlow: (flow) =>
    Api.post(`${BASE}/flows`, { data: flow, headers: handled }).then(unwrap),
  updateFlow: (id, flow) =>
    Api.put(`${BASE}/flows/${id}`, { data: flow, headers: handled }).then(
      unwrap,
    ),
  deleteFlow: (id) => Api.delete(`${BASE}/flows/${id}`).then(unwrap),
  validate: (flow) =>
    Api.post(`${BASE}/validate`, { data: flow, headers: handled }).then(unwrap),
  arm: (id) =>
    Api.post(`${BASE}/flows/${id}/arm`, { data: {}, headers: handled }).then(
      unwrap,
    ),
  disarm: (id) =>
    Api.post(`${BASE}/flows/${id}/disarm`, {
      data: {},
      headers: handled,
    }).then(unwrap),
  // options: { mode, dry_run, skip_waits, contact: {contact_id} | {aos_in, duration, ...} }
  run: (id, triggerNode, variables, options = {}) =>
    Api.post(`${BASE}/flows/${id}/run`, {
      data: { trigger_node: triggerNode, variables, ...options },
      headers: handled,
    }).then(unwrap),
  listRuns: (flowId, limit = 50) => get('/runs', { flow_id: flowId, limit }),
  // filters: { flow_id, status, trigger, user, activity_id, dry_run, since, until, search, limit }
  searchRuns: (filters) => get('/runs', filters),

  // Version history
  versions: (id) => get(`/flows/${id}/versions`),
  version: (id, version) => get(`/flows/${id}/versions/${version}`),
  diff: (id, from, to = 'current') => get(`/flows/${id}/diff`, { from, to }),
  restoreVersion: (id, version, note) =>
    post(`/flows/${id}/versions/${version}/restore`, { note }),
  deletedFlows: () => get('/flows/deleted'),
  undelete: (id) => post(`/flows/${id}/undelete`),
  getRun: (id) =>
    Api.get(`${BASE}/runs/${id}`, { headers: handled }).then(unwrap),
  stopRun: (id) =>
    Api.post(`${BASE}/runs/${id}/stop`, { data: {}, headers: handled }).then(
      unwrap,
    ),
  approve: (runId, approvalId, decision, value) =>
    Api.post(`${BASE}/runs/${runId}/approve`, {
      data: { approval_id: approvalId, decision, value },
      headers: handled,
    }).then(unwrap),
  stopAll: () => Api.post(`${BASE}/stop_all`, { data: {} }).then(unwrap),
  whoami: () => get('/whoami'),
  loadDemo: () => post('/demo'),
  clearDemo: () => del('/demo'),
  audit: (params) => get('/audit', params),

  // Contacts: satellites, stations, predicted passes and scheduled activities
  schedule: (start, end) => get('/schedule', { start, end }),
  recompute: () => post('/contacts/recompute'),
  saveSatellite: (id, satellite) =>
    id ? put(`/satellites/${id}`, satellite) : post('/satellites', satellite),
  deleteSatellite: (id) => del(`/satellites/${id}`),
  setTle: (id, tle) => post(`/satellites/${id}/tle`, { tle }),
  saveStation: (id, station) =>
    id ? put(`/stations/${id}`, station) : post('/stations', station),
  deleteStation: (id) => del(`/stations/${id}`),
  saveActivity: (id, activity) =>
    id ? put(`/activities/${id}`, activity) : post('/activities', activity),
  deleteActivity: (id) => del(`/activities/${id}`),
  acceptActivity: (id) => post(`/activities/${id}/accept`),
  refreshActivityTemplate: (id) => post(`/activities/${id}/refresh_template`),
  ingestTles: (text, source) => post('/tles', { tle: text, source }),
  tleInputs: () => get('/tle_inputs'),
  saveTleInputs: (settings) => put('/tle_inputs', settings),
  acceptHeldTle: (id) => post(`/satellites/${id}/tle/accept`),
  rejectHeldTle: (id) => post(`/satellites/${id}/tle/reject`),
  rehearseActivity: (id, skipWaits = true) =>
    post(`/activities/${id}/rehearse`, { skip_waits: skipWaits }),
  alerts: () => get('/alerts'),
  saveAlerts: (settings) => put('/alerts', settings),
  testAlert: (webhookId) => post('/alerts/test', { webhook_id: webhookId }),
  saveRule: (id, rule) =>
    id ? put(`/rules/${id}`, rule) : post('/rules', rule),
  deleteRule: (id) => del(`/rules/${id}`),
  pauseScheduler: () => post('/scheduler/pause'),
  resumeScheduler: () => post('/scheduler/resume'),
  // Script Runner's file list, for the Run script node
  listScripts: () =>
    Api.get('/script-api/scripts', { headers: quiet }).then(unwrap),
}

// Pull a readable message (and validation problems) out of an API error
export function errorInfo(error) {
  const data = error?.response?.data || {}
  return {
    message: data.error || error?.message || String(error),
    problems: data.problems || [],
  }
}
