<!--
# Version history of a flow (versions.py): every save with who, when, the
# note and what changed. Pick a version to see how it differs from the
# current flow, export it or restore it (restoring saves it as a new version).
# With no flow open it lists deleted flows to restore.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="1100"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-history">
      <v-card-title class="d-flex align-center">
        <v-icon class="mr-2">mdi-history</v-icon>
        <span v-if="flowId">History of '{{ flowName }}'</span>
        <span v-else>Deleted flows</span>
        <v-spacer />
        <v-btn-toggle
          v-model="tab"
          mandatory
          density="compact"
          variant="outlined"
          divided
        >
          <v-btn value="versions" size="small" :disabled="!flowId">
            Versions
          </v-btn>
          <v-btn value="deleted" size="small">Deleted flows</v-btn>
        </v-btn-toggle>
      </v-card-title>
      <v-card-text class="fa-history-body">
        <div v-if="tab === 'versions'" class="d-flex fa-history-split">
          <v-list density="compact" class="fa-history-list">
            <v-list-item
              v-for="v in versions"
              :key="v.version"
              :active="selected && selected.version === v.version"
              class="fa-history-version"
              :data-version="v.version"
              @click="select(v)"
            >
              <v-list-item-title>
                <b>v{{ v.version }}</b>
                <v-chip
                  v-if="v.version === currentVersion"
                  size="x-small"
                  label
                  color="primary"
                  class="ml-1"
                >
                  current
                </v-chip>
                · {{ v.saved_by }}
              </v-list-item-title>
              <v-list-item-subtitle>
                {{ fmtTime(v.saved_at) }}
              </v-list-item-subtitle>
              <div v-if="v.note" class="text-body-2 mt-1">"{{ v.note }}"</div>
              <div class="text-caption text-medium-emphasis">
                {{ v.summary }}
                <template v-if="v.restored_from">
                  (restored v{{ v.restored_from }})
                </template>
              </div>
            </v-list-item>
          </v-list>
          <div class="fa-history-detail pa-3">
            <div v-if="!selected" class="text-medium-emphasis text-caption">
              Pick a version to see what's different from the current flow.
            </div>
            <template v-else>
              <div class="d-flex align-center mb-2">
                <span class="text-subtitle-1">
                  v{{ selected.version }} compared with the current flow (v{{
                    currentVersion
                  }})
                </span>
                <v-spacer />
                <v-btn
                  size="small"
                  variant="text"
                  prepend-icon="mdi-download"
                  @click="exportVersion"
                >
                  Export
                </v-btn>
                <v-btn
                  size="small"
                  color="primary"
                  variant="flat"
                  prepend-icon="mdi-restore"
                  class="ml-2 fa-restore-version"
                  :disabled="selected.version === currentVersion"
                  @click="restore"
                >
                  Restore v{{ selected.version }}
                </v-btn>
              </div>
              <div v-if="!diff" class="text-caption">Loading…</div>
              <template v-else>
                <div
                  v-if="diff.summary === 'no changes'"
                  class="text-medium-emphasis"
                >
                  Same as the current flow.
                </div>
                <div v-if="Object.keys(diff.settings).length" class="mb-3">
                  <div class="text-caption text-uppercase mb-1">Settings</div>
                  <div
                    v-for="(change, key) in diff.settings"
                    :key="key"
                    class="fa-diff-row"
                  >
                    <b>{{ key }}</b
                    >: <span class="fa-diff-old">{{ show(change[0]) }}</span> →
                    <span class="fa-diff-new">{{ show(change[1]) }}</span>
                  </div>
                </div>
                <div v-if="diff.nodes_added.length" class="mb-3">
                  <div class="text-caption text-uppercase mb-1">
                    Added since v{{ selected.version }}
                  </div>
                  <div
                    v-for="n in diff.nodes_added"
                    :key="n.id"
                    class="fa-diff-new"
                  >
                    + {{ n.label }} ({{ n.type }})
                  </div>
                </div>
                <div v-if="diff.nodes_removed.length" class="mb-3">
                  <div class="text-caption text-uppercase mb-1">
                    Removed since v{{ selected.version }}
                  </div>
                  <div
                    v-for="n in diff.nodes_removed"
                    :key="n.id"
                    class="fa-diff-old"
                  >
                    − {{ n.label }} ({{ n.type }})
                  </div>
                </div>
                <div v-if="diff.nodes_changed.length" class="mb-3">
                  <div class="text-caption text-uppercase mb-1">Changed</div>
                  <div
                    v-for="n in diff.nodes_changed"
                    :key="n.id"
                    class="mb-2 fa-diff-node"
                  >
                    <b>{{ n.label }}</b>
                    <span class="text-caption"> ({{ n.type }})</span>
                    <div
                      v-for="(change, field) in n.fields"
                      :key="field"
                      class="fa-diff-row ml-3"
                    >
                      {{ field }}:
                      <span class="fa-diff-old">{{ show(change[0]) }}</span>
                      →
                      <span class="fa-diff-new">{{ show(change[1]) }}</span>
                    </div>
                  </div>
                </div>
                <div
                  v-if="diff.edges_added || diff.edges_removed"
                  class="text-caption"
                >
                  Wires: {{ diff.edges_added }} added,
                  {{ diff.edges_removed }} removed since v{{ selected.version }}
                </div>
                <div v-if="diff.nodes_moved" class="text-caption">
                  {{ diff.nodes_moved }} nodes moved
                </div>
              </template>
            </template>
          </div>
        </div>

        <div v-else>
          <v-table density="compact" class="fa-deleted">
            <thead>
              <tr>
                <th>Flow</th>
                <th>Deleted</th>
                <th>By</th>
                <th>Last version</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in deleted" :key="d.id">
                <td>
                  {{ d.name }}
                  <v-chip v-if="d.template" size="x-small" label class="ml-1">
                    template
                  </v-chip>
                </td>
                <td>{{ fmtTime(d.deleted_at) }}</td>
                <td>{{ d.deleted_by }}</td>
                <td>v{{ d.version }}</td>
                <td>
                  <v-btn
                    size="x-small"
                    variant="tonal"
                    prepend-icon="mdi-restore"
                    @click="undelete(d)"
                  >
                    Restore
                  </v-btn>
                </td>
              </tr>
              <tr v-if="!deleted.length">
                <td colspan="5" class="text-caption text-medium-emphasis">
                  No deleted flows.
                </td>
              </tr>
            </tbody>
          </v-table>
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
import flowApi, { errorInfo } from './flowApi'

export default {
  props: {
    modelValue: { type: Boolean, default: false },
    flowId: { type: String, default: null },
    flowName: { type: String, default: '' },
    currentVersion: { type: Number, default: null },
    // Open with this version selected (a run's "v3" link)
    initialVersion: { type: Number, default: null },
    startTab: { type: String, default: 'versions' },
  },
  emits: ['update:modelValue', 'restored'],
  data() {
    return {
      tab: 'versions',
      versions: [],
      selected: null,
      diff: null,
      deleted: [],
    }
  },
  watch: {
    modelValue(open) {
      if (open) this.load()
    },
    tab() {
      if (this.tab === 'deleted') this.loadDeleted()
    },
  },
  methods: {
    async load() {
      this.selected = null
      this.diff = null
      this.tab =
        this.flowId && this.startTab !== 'deleted' ? 'versions' : 'deleted'
      if (this.tab === 'deleted') return this.loadDeleted()
      try {
        this.versions = await flowApi.versions(this.flowId)
        const wanted = this.versions.find(
          (v) => v.version === this.initialVersion,
        )
        if (wanted) this.select(wanted)
      } catch (error) {
        this.versions = []
        this.$notify.caution({
          title: 'Could not load history',
          body: errorInfo(error).message,
        })
      }
    },
    async loadDeleted() {
      try {
        this.deleted = await flowApi.deletedFlows()
      } catch {
        this.deleted = []
      }
    },
    async select(version) {
      this.selected = version
      this.diff = null
      try {
        this.diff = await flowApi.diff(this.flowId, version.version)
      } catch (error) {
        this.diff = null
        this.$notify.caution({
          title: 'Could not compare',
          body: errorInfo(error).message,
        })
      }
    },
    async restore() {
      const v = this.selected.version
      if (
        !window.confirm(
          `Restore v${v}? It's saved as a new version; nothing is lost.`,
        )
      )
        return
      try {
        const result = await flowApi.restoreVersion(this.flowId, v)
        this.$emit('restored', result.flow)
        this.$emit('update:modelValue', false)
      } catch (error) {
        this.$notify.caution({
          title: 'Restore failed',
          body: errorInfo(error).message,
        })
      }
    },
    async undelete(d) {
      try {
        const result = await flowApi.undelete(d.id)
        this.$emit('restored', result.flow)
        this.$emit('update:modelValue', false)
      } catch (error) {
        this.$notify.caution({
          title: 'Restore failed',
          body: errorInfo(error).message,
        })
      }
    },
    async exportVersion() {
      const entry = await flowApi.version(this.flowId, this.selected.version)
      const blob = new Blob([JSON.stringify(entry.flow, null, 2)], {
        type: 'application/json',
      })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `${this.flowName || 'flow'}-v${this.selected.version}.json`
      link.click()
      URL.revokeObjectURL(link.href)
    },
    show(value) {
      if (value === undefined || value === null || value === '') return '—'
      const text = typeof value === 'string' ? value : JSON.stringify(value)
      return text.length > 120 ? `${text.slice(0, 117)}…` : text
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
.fa-history-body {
  height: 70vh;
}
.fa-history-split {
  height: 100%;
}
.fa-history-list {
  width: 340px;
  flex-shrink: 0;
  overflow-y: auto;
  border-right: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-history-detail {
  flex: 1;
  overflow-y: auto;
}
.fa-diff-row {
  font-family: monospace;
  font-size: 12px;
  word-break: break-all;
}
.fa-diff-old {
  color: #ef9a9a;
}
.fa-diff-new {
  color: #a5d6a7;
}
</style>
