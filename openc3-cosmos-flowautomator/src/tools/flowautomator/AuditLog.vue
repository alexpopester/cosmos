<!--
# Who did what: the engine's audit log (audit.py), newest first. Changes
# made through the tool are recorded with the user from their token; the
# contact scheduler's own actions show as "scheduler".
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="1100"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-audit">
      <v-card-title class="d-flex align-center">
        <v-icon class="mr-2">mdi-clipboard-text-clock</v-icon>
        Audit log
        <v-spacer />
        <span v-if="me" class="text-caption text-medium-emphasis">
          You are {{ me.name || me.username }}
        </span>
      </v-card-title>
      <v-card-text>
        <div class="d-flex align-center mb-2">
          <v-text-field
            v-model="search"
            placeholder="Search"
            prepend-inner-icon="mdi-magnify"
            density="compact"
            variant="outlined"
            hide-details
            clearable
            class="mr-2 fa-audit-search"
            @update:model-value="loadSoon"
          />
          <v-select
            v-model="kind"
            :items="kinds"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2 fa-audit-kind"
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
        <v-table density="compact" class="fa-audit-table">
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>User</th>
              <th>Action</th>
              <th>What</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(e, i) in entries" :key="i">
              <td class="text-no-wrap">{{ fmtTime(e.time) }}</td>
              <td>
                <v-chip
                  size="x-small"
                  label
                  :color="e.user === 'scheduler' ? 'info' : undefined"
                >
                  {{ e.user }}
                </v-chip>
              </td>
              <td>{{ e.action }}</td>
              <td>
                <span class="text-medium-emphasis">{{ e.kind }}</span>
                {{ e.target_name }}
              </td>
              <td class="text-caption">{{ e.detail }}</td>
            </tr>
            <tr v-if="!entries.length">
              <td colspan="5" class="text-caption text-medium-emphasis">
                {{ loading ? 'Loading…' : 'Nothing recorded yet.' }}
              </td>
            </tr>
          </tbody>
        </v-table>
        <div class="text-caption text-medium-emphasis mt-2">
          The engine keeps the latest 10,000 entries. Every entry is also in the
          COSMOS log ([audit] messages), which COSMOS keeps in its log bucket.
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
  },
  emits: ['update:modelValue'],
  data() {
    return {
      entries: [],
      me: null,
      search: '',
      kind: null,
      loading: false,
      timer: null,
      kinds: [
        { title: 'Everything', value: null },
        { title: 'Flows and runs', value: 'flow' },
        { title: 'Activities', value: 'activity' },
        { title: 'Rules', value: 'rule' },
        { title: 'Satellites', value: 'satellite' },
        { title: 'Stations', value: 'station' },
        { title: 'Scheduler', value: 'scheduler' },
        { title: 'Engine', value: 'engine' },
      ],
    }
  },
  watch: {
    modelValue(open) {
      if (open) {
        this.load()
        flowApi
          .whoami()
          .then((r) => (this.me = r.user))
          .catch(() => {})
      }
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
        this.entries = await flowApi.audit({
          limit: 500,
          kind: this.kind || undefined,
          search: this.search || undefined,
        })
      } catch {
        this.entries = []
      } finally {
        this.loading = false
      }
    },
    fmtTime(t) {
      return new Date(t * 1000).toISOString().slice(0, 19).replace('T', ' ')
    },
  },
}
</script>

<style>
.fa-audit-search {
  max-width: 360px;
}
.fa-audit-kind {
  max-width: 200px;
}
</style>
