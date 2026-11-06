<!--
# Bottom panel: the flow's validation problems, its run history and the
# step-by-step log of the run being viewed.
-->

<template>
  <div class="fa-run-panel" :class="{ 'fa-run-panel-collapsed': collapsed }">
    <div class="fa-run-panel-head">
      <v-tabs
        v-model="tab"
        density="compact"
        class="flex-grow-1"
        show-arrows
        @click="collapsed && $emit('expand')"
      >
        <v-tab value="running" class="fa-tab-running">
          <v-icon start size="16">mdi-run-fast</v-icon>
          All running
          <v-badge
            v-if="allRunning.length"
            :content="allRunning.length"
            color="primary"
            inline
          />
        </v-tab>
        <v-tab value="runs">
          Runs
          <v-badge
            v-if="activeCount"
            :content="activeCount"
            color="primary"
            inline
          />
        </v-tab>
        <v-tab value="steps" :disabled="!run"> Run log </v-tab>
        <v-tab value="problems">
          Problems
          <v-badge
            v-if="problems.length"
            :content="problems.length"
            color="error"
            inline
          />
        </v-tab>
      </v-tabs>
      <div class="fa-run-panel-actions">
        <slot name="actions" />
      </div>
    </div>
    <div v-show="!collapsed" class="fa-run-panel-body">
      <div v-if="tab === 'running'">
        <div
          v-if="!allRunning.length"
          class="text-medium-emphasis text-body-2 pa-3"
        >
          Nothing is running in any flow. Finished runs are in View > Run
          History.
        </div>
        <v-table v-else density="compact" fixed-header hover>
          <thead>
            <tr>
              <th>Flow</th>
              <th>Status</th>
              <th>Running for</th>
              <th>Started by</th>
              <th>Mode</th>
              <th>Message</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in allRunning"
              :key="r.id"
              :class="{ 'fa-row-selected': run && r.id === run.id }"
              class="fa-clickable fa-running-row"
              title="Open this flow and follow the run"
              @click="$emit('open-run', r.flow_id, r.id)"
            >
              <td class="fa-flow-name">
                {{ r.flow_name }}
                <v-chip
                  v-if="r.flow_id === flowId"
                  size="x-small"
                  label
                  class="ml-1"
                >
                  open
                </v-chip>
              </td>
              <td class="text-no-wrap">
                <v-chip
                  :color="waitingCount(r) ? 'amber' : statusColor(r.status)"
                  size="small"
                  label
                  variant="flat"
                >
                  {{ waitingCount(r) ? 'awaiting approval' : r.status }}
                </v-chip>
                <v-chip
                  v-if="r.dry_run"
                  size="small"
                  label
                  color="info"
                  class="ml-1"
                >
                  dry run
                </v-chip>
                <span v-if="subflowCount(r)" class="text-caption ml-1">
                  + {{ subflowCount(r) }} sub-flow{{
                    subflowCount(r) > 1 ? 's' : ''
                  }}
                </span>
              </td>
              <td class="text-no-wrap">{{ duration(r) }}</td>
              <td class="fa-run-trigger" :title="triggerText(r)">
                <v-icon size="14" class="mr-1">{{ triggerIcon(r) }}</v-icon
                >{{ triggerText(r) }}
              </td>
              <td>{{ r.mode }}</td>
              <td class="fa-message" :title="r.message">{{ r.message }}</td>
              <td class="text-no-wrap">
                <v-btn
                  size="small"
                  variant="tonal"
                  prepend-icon="mdi-open-in-app"
                  class="mr-1"
                  @click.stop="$emit('open-run', r.flow_id, r.id)"
                >
                  Open
                </v-btn>
                <v-btn
                  size="small"
                  color="warning"
                  variant="tonal"
                  @click.stop="$emit('stop-run', r.id)"
                >
                  Stop
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
      </div>

      <div v-else-if="tab === 'runs'">
        <div v-if="!runs.length" class="text-medium-emphasis text-caption pa-3">
          No runs yet. Use Run now, or arm the flow and let its triggers fire.
        </div>
        <v-table v-else density="compact" fixed-header hover>
          <thead>
            <tr>
              <th>Started</th>
              <th>Status</th>
              <th>Duration</th>
              <th>Trigger</th>
              <th>Message</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in runs"
              :key="r.id"
              :class="{ 'fa-row-selected': run && r.id === run.id }"
              class="fa-clickable"
              @click="$emit('select-run', r.id)"
            >
              <td class="text-no-wrap">{{ formatTime(r.started_at) }}</td>
              <td>
                <v-chip
                  :color="statusColor(r.status)"
                  size="x-small"
                  label
                  variant="flat"
                >
                  {{ r.status }}
                </v-chip>
                <v-chip
                  v-if="r.dry_run"
                  size="x-small"
                  label
                  color="info"
                  class="ml-1"
                >
                  dry run
                </v-chip>
                <span v-if="r.parent_id" class="text-caption ml-1">
                  sub-flow
                </span>
              </td>
              <td>{{ duration(r) }}</td>
              <td class="fa-run-trigger" :title="triggerText(r)">
                <v-icon size="14" class="mr-1">{{ triggerIcon(r) }}</v-icon
                >{{ triggerText(r) }}
                <span v-if="r.flow_version" class="text-medium-emphasis">
                  · v{{ r.flow_version }}
                </span>
              </td>
              <td class="fa-message" :title="r.message">{{ r.message }}</td>
              <td>
                <v-btn
                  v-if="r.status === 'running'"
                  size="x-small"
                  color="warning"
                  variant="tonal"
                  @click.stop="$emit('stop-run', r.id)"
                >
                  Stop
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
      </div>

      <div v-else-if="tab === 'steps' && run" class="d-flex fa-steps">
        <div class="flex-grow-1 fa-steps-table">
          <div class="d-flex align-center flex-wrap px-2 py-1 fa-run-head">
            <v-chip
              :color="statusColor(run.status)"
              size="small"
              label
              variant="flat"
            >
              {{ run.status }}
            </v-chip>
            <v-chip
              v-if="run.dry_run"
              size="small"
              label
              color="info"
              class="ml-1"
            >
              DRY RUN{{ run.skip_waits ? ', waits skipped' : '' }}
            </v-chip>
            <span class="ml-2 text-caption" :title="`Run ${run.id}`">
              {{ run.flow_name }}
              <a
                v-if="run.flow_version"
                href="#"
                class="fa-version-link"
                title="Show this version in the flow's history"
                @click.prevent="$emit('open-version', run.flow_version)"
                >v{{ run.flow_version }}</a
              >
              · <v-icon size="14">{{ triggerIcon(run) }}</v-icon>
              {{ triggerText(run) }} · {{ run.step_count }} steps ·
              {{ run.mode }} mode · run {{ run.id }}
              <span v-if="run.auto_approve">(running the rest)</span>
              <span v-if="run.steps_dropped">
                ({{ run.steps_dropped }} oldest not shown)
              </span>
            </span>
            <span v-if="run.message" class="ml-2 text-caption text-error">
              {{ run.message }}
            </span>
          </div>
          <v-table density="compact" hover>
            <thead>
              <tr>
                <th>Time</th>
                <th>Node</th>
                <th>Status</th>
                <th>Output</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(step, i) in run.steps"
                :key="(run.steps_dropped || 0) + i"
                class="fa-clickable"
                @click="$emit('focus-node', step.node)"
              >
                <td class="text-no-wrap text-caption">
                  {{ formatTime(step.started_at, true) }}
                </td>
                <td>{{ nodeLabel(step.node) || step.label }}</td>
                <td>
                  <v-chip
                    :color="statusColor(step.status)"
                    size="x-small"
                    label
                    variant="flat"
                  >
                    {{ step.status }}
                  </v-chip>
                </td>
                <td>{{ step.output }}</td>
                <td class="fa-step-message">{{ step.message }}</td>
              </tr>
            </tbody>
          </v-table>
        </div>
        <div class="fa-vars">
          <div class="text-caption mb-1">Variables</div>
          <pre>{{ JSON.stringify(run.variables, null, 2) }}</pre>
        </div>
      </div>

      <div v-else-if="tab === 'problems'">
        <div
          v-if="!problems.length"
          class="text-medium-emphasis text-caption pa-3"
        >
          No problems. The flow can be armed.
        </div>
        <v-list v-else density="compact">
          <v-list-item
            v-for="(p, i) in problems"
            :key="i"
            prepend-icon="mdi-alert"
            :title="p.message"
            :subtitle="p.node ? nodeLabel(p.node) : 'Flow'"
            @click="p.node && $emit('focus-node', p.node)"
          />
        </v-list>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  props: {
    runs: { type: Array, default: () => [] },
    run: { type: Object, default: null },
    problems: { type: Array, default: () => [] },
    nodeLabel: { type: Function, default: (id) => id },
    // Every active run in the scope (engine /status), for the All running tab
    activeRuns: { type: Array, default: () => [] },
    flowId: { type: String, default: null },
    // Only the tab bar shows; clicking a tab asks to expand
    collapsed: { type: Boolean, default: false },
  },
  emits: [
    'select-run',
    'stop-run',
    'focus-node',
    'open-version',
    'open-run',
    'expand',
  ],
  data() {
    return { tab: 'runs' }
  },
  computed: {
    activeCount() {
      return this.runs.filter((r) => r.status === 'running').length
    },
    // Top-level runs only: sub-flows show as a count on the run that called them
    allRunning() {
      return this.activeRuns
        .filter((r) => !r.parent_id)
        .sort((a, b) => (b.started_at || 0) - (a.started_at || 0))
    },
  },
  methods: {
    show(tab) {
      this.tab = tab
    },
    descendants(run) {
      const out = []
      const walk = (id) => {
        for (const r of this.activeRuns) {
          if (r.parent_id === id) {
            out.push(r)
            walk(r.id)
          }
        }
      }
      walk(run.id)
      return out
    },
    subflowCount(run) {
      return this.descendants(run).length
    },
    waitingCount(run) {
      return [run, ...this.descendants(run)].reduce(
        (n, r) => n + (r.approvals || []).length,
        0,
      )
    },
    triggerIcon(r) {
      const kind = (r.trigger || {}).kind || 'manual'
      return (
        {
          manual: 'mdi-account',
          activity: 'mdi-satellite-uplink',
          rehearsal: 'mdi-satellite-uplink',
          subflow: 'mdi-sitemap',
          trigger_tlm: 'mdi-flash',
          trigger_schedule: 'mdi-clock-outline',
          trigger_packet: 'mdi-package-down',
          trigger_limits: 'mdi-alert-octagon',
        }[kind] || 'mdi-play'
      )
    },
    // What started a run, from its trigger record (engine: FlowRun.trigger_info)
    triggerText(r) {
      const t = r.trigger || {}
      switch (t.kind) {
        case 'manual':
          return `${r.dry_run ? 'Dry run' : 'Run'} by ${t.user || 'someone'}${t.simulated_contact ? ' (simulated contact)' : ''}`
        case 'activity':
          return `Activity '${t.activity_label}' · ${t.satellite} over ${t.station}${t.rule_name ? ` · rule '${t.rule_name}'` : ''}${t.started_late ? ` · ${Math.round(t.started_late)}s late` : ''}`
        case 'rehearsal':
          return `Rehearsal of '${t.activity_label}' by ${t.user || 'someone'}`
        case 'subflow':
          return `Called by '${t.parent_flow}'`
        case undefined:
          return r.reason
        default:
          return `${t.node_label || t.kind}: ${t.detail || ''}${t.queued_at ? ' (queued)' : ''}`
      }
    },
    statusColor(status) {
      return (
        {
          running: 'primary',
          success: 'success',
          done: 'success',
          failed: 'error',
          error: 'error',
          stopped: 'warning',
          waiting: 'amber',
        }[status] || 'grey'
      )
    },
    formatTime(seconds, withMs) {
      if (!seconds) return ''
      const date = new Date(seconds * 1000)
      const time = date.toISOString().substring(11, withMs ? 23 : 19)
      return withMs ? time : `${date.toISOString().substring(0, 10)} ${time}`
    },
    duration(r) {
      if (!r.started_at) return ''
      const end = r.ended_at || Date.now() / 1000
      const seconds = end - r.started_at
      return seconds < 60
        ? `${seconds.toFixed(1)}s`
        : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
    },
  },
}
</script>

<style>
.fa-run-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.fa-run-panel-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-run-panel-collapsed .fa-run-panel-head {
  border-bottom: none;
}
.fa-run-panel-actions {
  flex: 0 0 auto;
  padding: 0 6px;
}
.fa-run-panel-body {
  flex: 1;
  overflow: auto;
}
.fa-run-panel td {
  font-size: 13px;
}
.fa-flow-name {
  font-weight: 600;
  white-space: nowrap;
}
.fa-run-trigger {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Wraps rather than squeezing the status chips in a narrow log */
.fa-run-head {
  row-gap: 4px;
}
.fa-run-head .v-chip {
  flex-shrink: 0;
}
.fa-step-message {
  min-width: 240px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.fa-clickable {
  cursor: pointer;
}
.fa-row-selected {
  background: rgba(77, 172, 255, 0.15);
}
.fa-message {
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fa-steps {
  height: 100%;
}
.fa-steps-table {
  overflow: auto;
  min-width: 0;
}
.fa-vars {
  flex: 0 0 220px;
  padding: 8px;
  border-left: 1px solid rgba(255, 255, 255, 0.12);
  overflow: auto;
}
.fa-vars pre {
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
