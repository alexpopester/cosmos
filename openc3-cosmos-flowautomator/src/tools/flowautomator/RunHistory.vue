<!--
# Runs across every flow (including deleted ones): what started each one,
# which version of the flow ran, and how it ended. Click a run to open it.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="1300"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-run-history">
      <v-card-title class="d-flex align-center">
        <v-icon class="mr-2">mdi-format-list-bulleted-type</v-icon>
        Run history
      </v-card-title>
      <v-card-text>
        <div class="d-flex align-center flex-wrap mb-2 fa-rh-filters">
          <v-text-field
            v-model="filters.search"
            placeholder="Search"
            prepend-inner-icon="mdi-magnify"
            density="compact"
            variant="outlined"
            hide-details
            clearable
            class="mr-2 fa-rh-search"
            @update:model-value="loadSoon"
          />
          <v-select
            v-model="filters.flow_id"
            :items="flowItems"
            label="Flow"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2 fa-rh-select"
            @update:model-value="load"
          />
          <v-select
            v-model="filters.trigger"
            :items="triggers"
            label="Started by"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2 fa-rh-select"
            @update:model-value="load"
          />
          <v-select
            v-model="filters.status"
            :items="statuses"
            label="Status"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2 fa-rh-select"
            @update:model-value="load"
          />
          <v-select
            v-model="filters.dry_run"
            :items="dryItems"
            label="Dry runs"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2 fa-rh-select"
            @update:model-value="load"
          />
          <v-btn
            icon="mdi-refresh"
            size="small"
            variant="text"
            :loading="loading"
            @click="load"
          />
        </div>
        <v-table density="compact" hover class="fa-rh-table">
          <thead>
            <tr>
              <th>Started (UTC)</th>
              <th>Flow</th>
              <th>Started by</th>
              <th>Mode</th>
              <th>Status</th>
              <th>Took</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in runs"
              :key="r.id"
              class="fa-clickable"
              @click="$emit('open-run', r.flow_id, r.id)"
            >
              <td class="text-no-wrap">{{ fmtTime(r.started_at) }}</td>
              <td>
                {{ r.flow_name }}
                <span v-if="r.flow_version" class="text-medium-emphasis">
                  v{{ r.flow_version }}
                </span>
              </td>
              <td class="text-caption fa-rh-trigger">
                {{ triggerText(r) }}
              </td>
              <td>{{ r.mode }}</td>
              <td>
                <v-chip
                  size="x-small"
                  label
                  variant="flat"
                  :color="statusColor(r.status)"
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
              </td>
              <td class="text-no-wrap">{{ took(r) }}</td>
              <td class="text-caption fa-rh-message">{{ r.message }}</td>
            </tr>
            <tr v-if="!runs.length">
              <td colspan="7" class="text-caption text-medium-emphasis">
                {{ loading ? 'Loading…' : 'No runs match.' }}
              </td>
            </tr>
          </tbody>
        </v-table>
        <div class="text-caption text-medium-emphasis mt-2">
          Showing the newest {{ limit }}. Each flow keeps its last runs (the
          plugin's run_history setting); the COSMOS log keeps every run's start
          and end.
        </div>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="$emit('update:modelValue', false)">
          Close
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script>
import flowApi from './flowApi'

export default {
  props: {
    modelValue: { type: Boolean, default: false },
    flows: { type: Array, default: () => [] },
  },
  emits: ['update:modelValue', 'open-run'],
  data() {
    return {
      runs: [],
      loading: false,
      timer: null,
      limit: 300,
      filters: {
        search: '',
        flow_id: null,
        trigger: null,
        status: null,
        dry_run: null,
      },
      triggers: [
        { title: 'Anything', value: null },
        { title: 'Someone (Run now)', value: 'manual' },
        { title: 'Scheduled activity', value: 'activity' },
        { title: 'Rehearsal', value: 'rehearsal' },
        { title: 'Telemetry trigger', value: 'trigger_tlm' },
        { title: 'Schedule trigger', value: 'trigger_schedule' },
        { title: 'Packet trigger', value: 'trigger_packet' },
        { title: 'Limits trigger', value: 'trigger_limits' },
        { title: 'Sub-flow call', value: 'subflow' },
      ],
      statuses: [
        { title: 'Any', value: null },
        { title: 'Running', value: 'running' },
        { title: 'Success', value: 'success' },
        { title: 'Failed', value: 'failed' },
        { title: 'Stopped', value: 'stopped' },
      ],
      dryItems: [
        { title: 'Include', value: null },
        { title: 'Only dry runs', value: 'true' },
        { title: 'Real runs only', value: 'false' },
      ],
    }
  },
  computed: {
    flowItems() {
      return [
        { title: 'All flows', value: null },
        ...this.flows.map((f) => ({ title: f.name, value: f.id })),
      ]
    },
  },
  watch: {
    modelValue(open) {
      if (open) this.load()
    },
  },
  methods: {
    loadSoon() {
      clearTimeout(this.timer)
      this.timer = setTimeout(this.load, 300)
    },
    async load() {
      this.loading = true
      try {
        const params = { limit: this.limit }
        for (const [key, value] of Object.entries(this.filters)) {
          if (value !== null && value !== '') params[key] = value
        }
        this.runs = await flowApi.searchRuns(params)
      } catch {
        this.runs = []
      } finally {
        this.loading = false
      }
    },
    triggerText(r) {
      const t = r.trigger || {}
      switch (t.kind) {
        case 'manual':
          return `${t.user || 'someone'}${t.simulated_contact ? ' (simulated contact)' : ''}`
        case 'activity':
          return `Activity '${t.activity_label}' (${t.satellite} / ${t.station})${t.rule_name ? `, rule '${t.rule_name}'` : ''}`
        case 'rehearsal':
          return `${t.user || 'someone'} rehearsing '${t.activity_label}'`
        case 'subflow':
          return `Sub-flow of '${t.parent_flow}'`
        case undefined:
          return r.reason
        default:
          return `${t.node_label || t.kind}: ${t.detail || ''}`
      }
    },
    statusColor(status) {
      return (
        {
          running: 'primary',
          success: 'success',
          failed: 'error',
          stopped: 'warning',
        }[status] || undefined
      )
    },
    took(r) {
      if (!r.started_at) return ''
      const seconds = (r.ended_at || Date.now() / 1000) - r.started_at
      return seconds < 60
        ? `${seconds.toFixed(1)}s`
        : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
    },
    fmtTime(t) {
      return t
        ? new Date(t * 1000).toISOString().slice(0, 19).replace('T', ' ')
        : ''
    },
  },
}
</script>

<style>
.fa-rh-search {
  max-width: 260px;
}
.fa-rh-select {
  max-width: 190px;
}
.fa-rh-message {
  max-width: 320px;
}
</style>
