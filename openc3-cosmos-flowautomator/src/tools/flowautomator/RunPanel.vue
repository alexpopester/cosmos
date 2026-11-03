<!--
# Bottom panel: the flow's validation problems, its run history and the
# step-by-step log of the run being viewed.
-->

<template>
  <div class="fa-run-panel">
    <v-tabs v-model="tab" density="compact" class="flex-grow-0">
      <v-tab value="runs">
        Runs
        <v-badge
          v-if="activeCount"
          :content="activeCount"
          color="primary"
          inline
        />
      </v-tab>
      <v-tab value="steps" :disabled="!run">
        Run log
        <span v-if="run" class="ml-1 text-caption">({{ run.id }})</span>
      </v-tab>
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
    <div class="fa-run-panel-body">
      <div v-if="tab === 'runs'">
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
                <span v-if="r.parent_id" class="text-caption ml-1">
                  sub-flow
                </span>
              </td>
              <td>{{ duration(r) }}</td>
              <td class="text-caption">{{ r.reason }}</td>
              <td class="text-caption fa-message">{{ r.message }}</td>
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
          <div class="d-flex align-center px-2 py-1">
            <v-chip
              :color="statusColor(run.status)"
              size="small"
              label
              variant="flat"
            >
              {{ run.status }}
            </v-chip>
            <span class="ml-2 text-caption">
              {{ run.flow_name }} · {{ run.reason }} ·
              {{ run.step_count }} steps · {{ run.mode }} mode
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
                :key="i"
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
                <td class="text-caption fa-message">{{ step.message }}</td>
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
  },
  emits: ['select-run', 'stop-run', 'focus-node'],
  data() {
    return { tab: 'runs' }
  },
  computed: {
    activeCount() {
      return this.runs.filter((r) => r.status === 'running').length
    },
  },
  methods: {
    show(tab) {
      this.tab = tab
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
.fa-run-panel-body {
  flex: 1;
  overflow: auto;
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
}
.fa-vars {
  width: 260px;
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
