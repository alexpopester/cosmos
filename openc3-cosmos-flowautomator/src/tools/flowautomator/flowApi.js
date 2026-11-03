/*
# Client for the Flow Automator engine's HTTP API (the FLOW_ENGINE
# microservice, routed by Traefik at ROUTE_PREFIX).
*/

import { Api } from '@openc3/js-common/services'

export const ROUTE_PREFIX = '/flow-automator'
const BASE = `${ROUTE_PREFIX}/api`

// Errors the tool shows itself (validation problems, missing flows)
// instead of the generic network error banner
const handled = {
  Accept: 'application/json',
  'Content-Type': 'application/json',
  'Ignore-Errors': '400,404,409,503',
}

const unwrap = (response) => response.data
const post = (path, data = {}) =>
  Api.post(`${BASE}${path}`, { data, headers: handled }).then(unwrap)
const put = (path, data) =>
  Api.put(`${BASE}${path}`, { data, headers: handled }).then(unwrap)
const del = (path) =>
  Api.delete(`${BASE}${path}`, { headers: handled }).then(unwrap)

export default {
  status: () => Api.get(`${BASE}/status`).then(unwrap),
  listFlows: () => Api.get(`${BASE}/flows`).then(unwrap),
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
  run: (id, triggerNode, variables, mode) =>
    Api.post(`${BASE}/flows/${id}/run`, {
      data: { trigger_node: triggerNode, variables, mode },
      headers: handled,
    }).then(unwrap),
  listRuns: (flowId, limit = 50) =>
    Api.get(`${BASE}/runs`, { params: { flow_id: flowId, limit } }).then(
      unwrap,
    ),
  getRun: (id) =>
    Api.get(`${BASE}/runs/${id}`, { headers: handled }).then(unwrap),
  stopRun: (id) =>
    Api.post(`${BASE}/runs/${id}/stop`, { data: {}, headers: handled }).then(
      unwrap,
    ),
  approve: (runId, approvalId, decision) =>
    Api.post(`${BASE}/runs/${runId}/approve`, {
      data: { approval_id: approvalId, decision },
      headers: handled,
    }).then(unwrap),
  stopAll: () => Api.post(`${BASE}/stop_all`, { data: {} }).then(unwrap),
  whoami: () => Api.get(`${BASE}/whoami`).then(unwrap),
  audit: (params) => Api.get(`${BASE}/audit`, { params }).then(unwrap),

  // Contacts: satellites, stations, predicted passes and scheduled activities
  schedule: (start, end) =>
    Api.get(`${BASE}/schedule`, { params: { start, end } }).then(unwrap),
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
  saveRule: (id, rule) =>
    id ? put(`/rules/${id}`, rule) : post('/rules', rule),
  deleteRule: (id) => del(`/rules/${id}`),
  pauseScheduler: () => post('/scheduler/pause'),
  resumeScheduler: () => post('/scheduler/resume'),
  // Script Runner's file list, for the Run script node
  listScripts: () => Api.get('/script-api/scripts').then(unwrap),
}

// Pull a readable message (and validation problems) out of an API error
export function errorInfo(error) {
  const data = error?.response?.data || {}
  return {
    message: data.error || error?.message || String(error),
    problems: data.problems || [],
  }
}
