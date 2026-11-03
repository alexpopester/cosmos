<!--
# Flow Automator: build automation flows by wiring nodes together. Flows are
# stored and run by the FLOW_ENGINE microservice; this tool is the editor
# and the live view of their runs.
-->

<template>
  <div class="fa-root">
    <top-bar :menus="menus" :title="title" />
    <v-toolbar density="compact" class="fa-toolbar px-2" color="surface">
      <!-- Always empty so typing searches from scratch; the open flow's name
           shows as the placeholder -->
      <v-autocomplete
        v-model:search="flowSearch"
        :model-value="null"
        :items="flows"
        item-title="name"
        item-value="id"
        :placeholder="
          flowId ? flowSettings.name : 'Open a flow (type to search)'
        "
        persistent-placeholder
        auto-select-first
        density="compact"
        variant="outlined"
        hide-details
        class="fa-flow-select mr-2"
        @update:model-value="(id) => id && openFlow(id)"
        @update:menu="(open) => open && refreshFlows()"
      >
        <template #item="{ props: itemProps, item }">
          <v-list-item v-bind="itemProps">
            <template #append>
              <v-chip
                v-if="item.raw.armed"
                size="x-small"
                color="success"
                label
              >
                armed
              </v-chip>
              <v-chip
                v-if="item.raw.active_runs"
                size="x-small"
                color="primary"
                label
                class="ml-1"
              >
                running
              </v-chip>
            </template>
          </v-list-item>
        </template>
      </v-autocomplete>
      <v-btn
        size="small"
        variant="tonal"
        prepend-icon="mdi-plus"
        class="mr-2"
        @click="newFlow"
      >
        New
      </v-btn>
      <v-btn
        size="small"
        variant="tonal"
        prepend-icon="mdi-content-save"
        :color="dirty ? 'primary' : undefined"
        class="mr-2"
        :loading="saving"
        @click="save"
      >
        Save
      </v-btn>
      <span v-if="dirty" class="text-caption text-medium-emphasis mr-3">
        unsaved changes
      </span>
      <v-divider vertical class="mx-2" />
      <v-switch
        :model-value="armed"
        :disabled="!flowId"
        color="success"
        density="compact"
        hide-details
        inset
        class="flex-grow-0 mr-1"
        @update:model-value="setArmed"
      />
      <span class="text-body-2 mr-3" :class="armed ? 'text-success' : ''">
        {{ armed ? 'Armed' : 'Disarmed' }}
      </span>
      <v-menu v-if="triggerNodes.length > 1">
        <template #activator="{ props: menuProps }">
          <v-btn
            v-bind="menuProps"
            size="small"
            color="primary"
            variant="flat"
            prepend-icon="mdi-play"
            append-icon="mdi-menu-down"
            class="mr-2"
          >
            Run now
          </v-btn>
        </template>
        <v-list density="compact">
          <v-list-item
            v-for="n in triggerNodes"
            :key="n.id"
            :title="n.data.config.label || nodeTypes[n.data.kind].label"
            :subtitle="'Start from this trigger'"
            @click="runNow(n.id)"
          />
        </v-list>
      </v-menu>
      <v-btn
        v-else
        size="small"
        color="primary"
        variant="flat"
        prepend-icon="mdi-play"
        class="mr-2"
        :disabled="!triggerNodes.length"
        @click="runNow(triggerNodes[0] && triggerNodes[0].id)"
      >
        Run now
      </v-btn>
      <v-btn
        size="small"
        variant="tonal"
        color="warning"
        prepend-icon="mdi-stop"
        class="mr-2"
        :disabled="!activeRunIds.length"
        @click="stopFlowRuns"
      >
        Stop
      </v-btn>
      <v-spacer />
      <v-tooltip location="bottom">
        <template #activator="{ props: tip }">
          <v-chip
            v-bind="tip"
            size="small"
            :color="engineOnline ? 'success' : 'error'"
            variant="tonal"
            class="mr-2"
          >
            <v-icon start size="small">
              {{ engineOnline ? 'mdi-engine' : 'mdi-engine-off' }}
            </v-icon>
            <template v-if="engineOnline">
              {{ engineStatus.armed }} armed ·
              {{ engineStatus.active_runs.length }} running
            </template>
            <template v-else>Engine offline</template>
          </v-chip>
        </template>
        <span v-if="engineOnline">
          Flow engine is running. Triggers checked
          {{ engineStatus.triggers.watching }} ways.
          <span v-if="engineStatus.triggers.error">
            Last error: {{ engineStatus.triggers.error }}
          </span>
        </span>
        <span v-else>
          Can't reach the FLOW_ENGINE microservice at {{ routePrefix }}.
          {{ engineError }}
        </span>
      </v-tooltip>
      <v-btn
        size="small"
        color="error"
        variant="flat"
        prepend-icon="mdi-octagon"
        @click="confirmStopAll = true"
      >
        Stop all
      </v-btn>
    </v-toolbar>

    <div class="fa-main">
      <div class="fa-palette">
        <div v-for="category in categories" :key="category.key" class="mb-2">
          <div
            class="text-caption text-uppercase px-2 pt-2 pb-1 fa-category"
            :style="{ color: category.color }"
          >
            {{ category.label }}
          </div>
          <div
            v-for="(spec, type) in typesIn(category.key)"
            :key="type"
            class="fa-palette-item"
            :style="{ '--fa-color': category.color }"
            draggable="true"
            :title="spec.description"
            @dragstart="onDragStart($event, type)"
            @dblclick="addNode(type)"
          >
            <v-icon size="16" class="mr-2" :color="category.color">
              {{ spec.icon }}
            </v-icon>
            {{ spec.label }}
          </div>
        </div>
      </div>

      <div class="fa-center">
        <div class="fa-canvas" @drop="onDrop" @dragover.prevent>
          <VueFlow
            id="flow-automator"
            v-model:nodes="nodes"
            v-model:edges="edges"
            :default-edge-options="defaultEdgeOptions"
            :delete-key-code="['Delete', 'Backspace']"
            :snap-to-grid="true"
            :snap-grid="[10, 10]"
            :min-zoom="0.2"
            :max-zoom="2"
            :is-valid-connection="isValidConnection"
            class="fa-flow"
            @connect="onConnect"
            @node-drag-stop="dirty = true"
            @nodes-change="onNodesChange"
            @edges-change="onEdgesChange"
            @nodes-initialized="onNodesInitialized"
            @pane-click="selectedNodeId = null"
            @node-click="selectedNodeId = $event.node.id"
          >
            <template #node-flow="nodeProps">
              <flow-node v-bind="nodeProps" />
            </template>
            <Background pattern-color="#3a4a5a" :gap="20" />
            <Controls />
            <MiniMap pannable zoomable :node-color="miniMapColor" />
            <div v-if="!flowId && !nodes.length" class="fa-empty">
              <v-icon size="48" class="mb-2">mdi-sitemap-outline</v-icon>
              <div>Open a flow, or drag nodes from the left to start one.</div>
            </div>
          </VueFlow>
        </div>
        <div class="fa-bottom">
          <run-panel
            ref="runPanel"
            :runs="runs"
            :run="selectedRun"
            :problems="problems"
            :node-label="nodeLabel"
            @select-run="selectRun"
            @stop-run="stopRun"
            @focus-node="focusNode"
          />
        </div>
      </div>

      <div class="fa-side">
        <node-inspector
          :node="selectedNode"
          :flow="flowSettings"
          :flows="flows"
          :scripts="scripts"
          :problems="
            selectedNode ? flowContext.nodeProblems[selectedNode.id] : []
          "
          @changed="onInspectorChange"
          @delete-node="deleteSelected"
        />
      </div>
    </div>

    <v-dialog v-model="confirmStopAll" max-width="440">
      <v-card>
        <v-card-title class="text-error">
          <v-icon class="mr-2">mdi-octagon</v-icon>Stop all flows?
        </v-card-title>
        <v-card-text>
          This disarms every flow and stops every run in this scope, including
          flows you don't have open. Scripts started by runs that are waiting on
          them are stopped too.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmStopAll = false">Cancel</v-btn>
          <v-btn color="error" variant="flat" @click="stopAll">Stop all</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="confirmDiscard.show" max-width="420">
      <v-card>
        <v-card-title>Discard unsaved changes?</v-card-title>
        <v-card-text>
          '{{ flowSettings.name }}' has changes that aren't saved.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmDiscard.show = false">
            Keep editing
          </v-btn>
          <v-btn color="warning" variant="flat" @click="discardAndContinue">
            Discard
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <input
      ref="importInput"
      type="file"
      accept=".json,application/json"
      style="display: none"
      @change="importFile"
    />
  </div>
</template>

<script>
import { reactive } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { TopBar } from '@openc3/vue-common/components'
import FlowNode from './FlowNode.vue'
import NodeInspector from './NodeInspector.vue'
import RunPanel from './RunPanel.vue'
import flowApi, { ROUTE_PREFIX, errorInfo } from './flowApi'
import {
  CATEGORIES,
  NODE_TYPES,
  categoryColor,
  defaultData,
  outputsFor,
} from './nodeTypes'

const STATUS_POLL_MS = 1000
const RUN_POLL_MS = 500

function blankSettings() {
  return { name: 'New flow', description: '', policy: 'skip', variables: {} }
}

export default {
  components: {
    TopBar,
    VueFlow,
    Background,
    Controls,
    MiniMap,
    FlowNode,
    NodeInspector,
    RunPanel,
  },
  provide() {
    return { flowContext: this.flowContext }
  },
  setup() {
    const { screenToFlowCoordinate, fitView, setCenter, findNode } =
      useVueFlow('flow-automator')
    return { screenToFlowCoordinate, fitView, setCenter, findNode }
  },
  data() {
    return {
      title: 'Flow Automator',
      routePrefix: ROUTE_PREFIX,
      nodeTypes: NODE_TYPES,
      categories: CATEGORIES,
      flows: [],
      flowSearch: '',
      flowId: null,
      armed: false,
      flowSettings: blankSettings(),
      nodes: [],
      edges: [],
      dirty: false,
      saving: false,
      problems: [],
      selectedNodeId: null,
      runs: [],
      selectedRun: null,
      followLive: true,
      engineStatus: { armed: 0, active_runs: [], triggers: {} },
      engineOnline: true,
      engineError: '',
      scripts: [],
      confirmStopAll: false,
      confirmDiscard: { show: false, action: null },
      defaultEdgeOptions: {
        type: 'smoothstep',
        markerEnd: 'arrowclosed',
        style: { strokeWidth: 2 },
      },
      flowContext: reactive({
        nodeStatus: {},
        nodeProblems: {},
        flowName: (id) => this.flows.find((f) => f.id === id)?.name,
      }),
      timers: [],
    }
  },
  computed: {
    menus() {
      return [
        {
          label: 'File',
          items: [
            { label: 'New Flow', icon: 'mdi-plus', command: this.newFlow },
            {
              label: 'Save',
              icon: 'mdi-content-save',
              command: this.save,
            },
            {
              label: 'Save As Copy',
              icon: 'mdi-content-copy',
              command: this.saveCopy,
            },
            { divider: true },
            {
              label: 'Import JSON...',
              icon: 'mdi-import',
              command: () => this.$refs.importInput.click(),
            },
            {
              label: 'Export JSON',
              icon: 'mdi-export',
              command: this.exportFile,
            },
            { divider: true },
            {
              label: 'Delete Flow',
              icon: 'mdi-delete',
              command: this.deleteFlow,
            },
          ],
        },
        {
          label: 'View',
          items: [
            {
              label: 'Fit to Screen',
              icon: 'mdi-fit-to-screen',
              command: () => this.fitView({ padding: 0.2 }),
            },
            {
              label: 'Follow Live Runs',
              checkbox: true,
              checked: this.followLive,
              command: () => {
                this.followLive = !this.followLive
              },
            },
          ],
        },
      ]
    },
    selectedNode() {
      return this.nodes.find((n) => n.id === this.selectedNodeId) || null
    },
    triggerNodes() {
      return this.nodes.filter((n) => NODE_TYPES[n.data.kind]?.trigger)
    },
    activeRunIds() {
      return this.engineStatus.active_runs
        .filter((r) => r.flow_id === this.flowId && !r.parent_id)
        .map((r) => r.id)
    },
  },
  watch: {
    selectedRun(run) {
      this.flowContext.nodeStatus = this.statusFromRun(run)
    },
  },
  created() {
    this.refreshFlows()
    this.pollStatus()
    this.timers.push(setInterval(this.pollStatus, STATUS_POLL_MS))
    this.timers.push(setInterval(this.pollRun, RUN_POLL_MS))
    flowApi
      .listScripts()
      .then((files) => {
        this.scripts = (files || [])
          .map((f) => (typeof f === 'string' ? f : f.name))
          .filter((f) => f && /\.(py|rb)$/.test(f))
      })
      .catch(() => {})
    window.addEventListener('beforeunload', this.beforeUnload)
  },
  beforeUnmount() {
    this.timers.forEach(clearInterval)
    clearTimeout(this.validateTimer)
    window.removeEventListener('beforeunload', this.beforeUnload)
  },
  methods: {
    // ---- flows --------------------------------------------------------
    async refreshFlows() {
      try {
        this.flows = await flowApi.listFlows()
        this.engineOnline = true
      } catch (error) {
        this.engineOnline = false
        this.engineError = errorInfo(error).message
      }
    },
    guardUnsaved(action) {
      if (this.dirty) {
        this.confirmDiscard = { show: true, action }
      } else {
        action()
      }
    },
    discardAndContinue() {
      const action = this.confirmDiscard.action
      this.confirmDiscard = { show: false, action: null }
      this.dirty = false
      action()
    },
    newFlow() {
      this.guardUnsaved(() => {
        this.loadFlow(null)
        this.addNode('trigger_manual', { x: 80, y: 120 })
        this.dirty = false
      })
    },
    openFlow(id) {
      this.flowSearch = ''
      if (id === this.flowId) return
      this.guardUnsaved(async () => {
        try {
          const { flow } = await flowApi.getFlow(id)
          this.loadFlow(flow)
        } catch (error) {
          this.$notify.serious({
            title: 'Could not open flow',
            body: errorInfo(error).message,
          })
        }
      })
    },
    loadFlow(flow) {
      this.flowId = flow ? flow.id : null
      this.armed = !!(flow && flow.armed)
      this.flowSettings = flow
        ? {
            name: flow.name,
            description: flow.description || '',
            policy: flow.policy || 'skip',
            variables: { ...(flow.variables || {}) },
          }
        : blankSettings()
      this.nodes = (flow ? flow.nodes : []).map((n) => ({
        id: n.id,
        type: 'flow',
        position: n.position || { x: 0, y: 0 },
        data: { kind: n.type, config: n.data || {} },
      }))
      this.edges = (flow ? flow.edges : []).map((e) => this.makeEdge(e))
      this.selectedNodeId = null
      this.selectedRun = null
      this.runs = []
      this.dirty = false
      this.problems = []
      this.flowContext.nodeProblems = {}
      this.validateSoon()
      this.refreshRuns()
      this.fitPending = true
    },
    toEngine() {
      return {
        ...this.flowSettings,
        nodes: this.nodes.map((n) => ({
          id: n.id,
          type: n.data.kind,
          position: { x: n.position.x, y: n.position.y },
          data: n.data.config,
        })),
        edges: this.edges.map((e) => ({
          id: e.id,
          source: e.source,
          sourceHandle: e.sourceHandle || 'out',
          target: e.target,
        })),
      }
    },
    async save() {
      this.saving = true
      try {
        const body = this.toEngine()
        const result = this.flowId
          ? await flowApi.updateFlow(this.flowId, body)
          : await flowApi.createFlow(body)
        const wasArmed = this.armed
        this.flowId = result.flow.id
        this.armed = result.flow.armed
        this.dirty = false
        this.applyProblems(result.problems)
        if (wasArmed && !this.armed) {
          this.$notify.caution({
            title: 'Flow disarmed',
            body: 'The saved flow has problems, so it was disarmed.',
          })
        }
        await this.refreshFlows()
        return true
      } catch (error) {
        this.$notify.serious({
          title: 'Save failed',
          body: errorInfo(error).message,
        })
        return false
      } finally {
        this.saving = false
      }
    },
    async saveCopy() {
      this.flowId = null
      this.armed = false
      this.flowSettings.name = `${this.flowSettings.name} (copy)`
      await this.save()
    },
    async deleteFlow() {
      if (!this.flowId) return

      if (!window.confirm(`Delete flow '${this.flowSettings.name}'?`)) return
      await flowApi.deleteFlow(this.flowId)
      this.dirty = false
      this.loadFlow(null)
      this.refreshFlows()
    },
    exportFile() {
      const flow = this.toEngine()
      const blob = new Blob([JSON.stringify(flow, null, 2)], {
        type: 'application/json',
      })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `${flow.name.replace(/[^\w-]+/g, '_')}.json`
      link.click()
      URL.revokeObjectURL(link.href)
    },
    importFile(event) {
      const file = event.target.files[0]
      event.target.value = ''
      if (!file) return
      file.text().then((text) => {
        this.guardUnsaved(() => {
          try {
            const flow = JSON.parse(text)
            delete flow.id
            flow.armed = false
            this.loadFlow(flow)
            this.dirty = true
          } catch (error) {
            this.$notify.serious({
              title: 'Import failed',
              body: String(error),
            })
          }
        })
      })
    },
    beforeUnload(event) {
      if (this.dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    },

    // ---- arming and running --------------------------------------------
    ensureSaved() {
      if (this.dirty || !this.flowId) {
        return this.save()
      }
      return Promise.resolve(true)
    },
    async setArmed(value) {
      if (value && !(await this.ensureSaved())) return
      try {
        const { flow } = value
          ? await flowApi.arm(this.flowId)
          : await flowApi.disarm(this.flowId)
        this.armed = flow.armed
        this.refreshFlows()
      } catch (error) {
        const info = errorInfo(error)
        this.armed = false
        this.applyProblems(info.problems.length ? info.problems : this.problems)
        this.$refs.runPanel.show('problems')
        this.$notify.caution({ title: 'Not armed', body: info.message })
      }
    },
    async runNow(triggerNode) {
      if (!(await this.ensureSaved())) return
      try {
        const result = await flowApi.run(this.flowId, triggerNode, {})
        if (result.result === 'started') {
          this.selectRun(result.run.id)
        } else {
          this.$notify.normal({
            title: `Run ${result.result}`,
            body: result.message,
          })
        }
        this.refreshRuns()
      } catch (error) {
        const info = errorInfo(error)
        if (info.problems.length) {
          this.applyProblems(info.problems)
          this.$refs.runPanel.show('problems')
        }
        this.$notify.caution({ title: 'Run not started', body: info.message })
      }
    },
    async stopRun(id) {
      try {
        await flowApi.stopRun(id)
      } catch (error) {
        this.$notify.caution({
          title: 'Stop failed',
          body: errorInfo(error).message,
        })
      }
    },
    stopFlowRuns() {
      this.activeRunIds.forEach(this.stopRun)
    },
    async stopAll() {
      this.confirmStopAll = false
      const result = await flowApi.stopAll()
      this.armed = false
      this.$notify.caution({
        title: 'All flows stopped',
        body: `Disarmed ${result.disarmed} flows and stopped ${result.stopped} runs.`,
      })
      this.refreshFlows()
    },

    // ---- live status ----------------------------------------------------
    async pollStatus() {
      try {
        const status = await flowApi.status()
        this.engineStatus = status
        // Someone else added or deleted a flow: keep the selector current
        if (status.flows !== this.flows.length) this.refreshFlows()
        this.engineOnline = true
        const active = status.active_runs.filter(
          (r) => r.flow_id === this.flowId && !r.parent_id,
        )
        const known = new Set(this.runs.map((r) => r.id))
        if (active.some((r) => !known.has(r.id))) {
          // A trigger started a run: show it
          await this.refreshRuns()
          if (
            this.followLive &&
            (!this.selectedRun || this.selectedRun.status !== 'running')
          ) {
            this.selectRun(active[active.length - 1].id)
          }
        } else if (this.runs.some((r) => r.status === 'running')) {
          await this.refreshRuns()
        }
      } catch (error) {
        this.engineOnline = false
        this.engineError = errorInfo(error).message
      }
    },
    async refreshRuns() {
      if (!this.flowId) {
        this.runs = []
        return
      }
      try {
        this.runs = await flowApi.listRuns(this.flowId, 50)
      } catch {
        // pollStatus reports the engine being down
      }
    },
    async selectRun(id) {
      try {
        this.selectedRun = await flowApi.getRun(id)
        this.$refs.runPanel.show('steps')
      } catch (error) {
        this.$notify.caution({
          title: 'Run not found',
          body: errorInfo(error).message,
        })
      }
    },
    async pollRun() {
      const run = this.selectedRun
      if (!run || run.status !== 'running' || this.pollingRun) return
      this.pollingRun = true
      try {
        const updated = await flowApi.getRun(run.id)
        if (this.selectedRun && this.selectedRun.id === updated.id) {
          this.selectedRun = updated
        }
      } catch {
        // Engine restarted or run expired from history
      } finally {
        this.pollingRun = false
      }
    },
    statusFromRun(run) {
      const status = {}
      if (!run || run.flow_id !== this.flowId) return status
      for (const step of run.steps || []) {
        status[step.node] = step.status
      }
      for (const id of run.active_nodes || []) {
        status[id] = 'running'
      }
      return status
    },

    // ---- validation -----------------------------------------------------
    validateSoon() {
      clearTimeout(this.validateTimer)
      this.validateTimer = setTimeout(async () => {
        try {
          const { problems } = await flowApi.validate(this.toEngine())
          this.applyProblems(problems)
        } catch {
          // Engine offline: shown in the toolbar
        }
      }, 400)
    },
    applyProblems(problems) {
      this.problems = problems || []
      const byNode = {}
      for (const p of this.problems) {
        if (p.node) (byNode[p.node] = byNode[p.node] || []).push(p.message)
      }
      this.flowContext.nodeProblems = byNode
    },

    // ---- editing --------------------------------------------------------
    typesIn(category) {
      return Object.fromEntries(
        Object.entries(NODE_TYPES).filter(([, s]) => s.category === category),
      )
    },
    onDragStart(event, type) {
      event.dataTransfer.setData('application/x-flow-node', type)
      event.dataTransfer.effectAllowed = 'move'
    },
    onDrop(event) {
      const type = event.dataTransfer.getData('application/x-flow-node')
      if (!type) return
      event.preventDefault()
      const position = this.screenToFlowCoordinate({
        x: event.clientX,
        y: event.clientY,
      })
      this.addNode(type, { x: position.x - 115, y: position.y - 20 })
    },
    addNode(type, position) {
      if (!position) {
        const offset = (this.nodes.length % 8) * 30
        position = { x: 200 + offset, y: 100 + offset }
      }
      const id = `${type}_${Math.random().toString(36).slice(2, 8)}`
      this.nodes = [
        ...this.nodes,
        {
          id,
          type: 'flow',
          position,
          data: { kind: type, config: defaultData(type) },
        },
      ]
      this.selectedNodeId = id
      this.changed()
    },
    makeEdge(edge) {
      const handle = edge.sourceHandle || 'out'
      return {
        id: edge.id || `e_${Math.random().toString(36).slice(2, 10)}`,
        source: edge.source,
        target: edge.target,
        sourceHandle: handle,
        targetHandle: 'in',
        type: 'smoothstep',
        markerEnd: 'arrowclosed',
        class: handle === 'error' ? 'fa-edge-error' : '',
        animated: handle === 'error',
      }
    },
    isValidConnection(connection) {
      return (
        connection.source !== connection.target ||
        // A loop node may wire its body straight back into itself
        this.findNode(connection.source)?.data.kind === 'loop'
      )
    },
    onConnect(connection) {
      const source = this.nodes.find((n) => n.id === connection.source)
      const handle = connection.sourceHandle || 'out'
      // One wire per output, except Parallel which branches on every wire:
      // connecting again replaces the old wire
      let edges = this.edges
      if (source && source.data.kind !== 'parallel') {
        edges = edges.filter(
          (e) =>
            !(
              e.source === connection.source &&
              (e.sourceHandle || 'out') === handle
            ),
        )
      }
      this.edges = [...edges, this.makeEdge(connection)]
      this.changed()
    },
    onNodesChange(changes) {
      if (changes.some((c) => c.type === 'remove')) {
        if (changes.some((c) => c.id === this.selectedNodeId)) {
          this.selectedNodeId = null
        }
        this.changed()
      }
    },
    onEdgesChange(changes) {
      if (changes.some((c) => c.type === 'remove')) this.changed()
    },
    onInspectorChange() {
      // Switch cases change a node's outputs: drop wires from outputs that are gone
      const node = this.selectedNode
      if (node) {
        const outputs = outputsFor(node.data.kind, node.data.config)
        this.edges = this.edges.filter(
          (e) =>
            e.source !== node.id || outputs.includes(e.sourceHandle || 'out'),
        )
      }
      this.changed()
    },
    deleteSelected() {
      const id = this.selectedNodeId
      this.nodes = this.nodes.filter((n) => n.id !== id)
      this.edges = this.edges.filter((e) => e.source !== id && e.target !== id)
      this.selectedNodeId = null
      this.changed()
    },
    changed() {
      this.dirty = true
      this.validateSoon()
    },
    nodeLabel(id) {
      const node = this.nodes.find((n) => n.id === id)
      if (!node) return id
      return node.data.config.label || NODE_TYPES[node.data.kind]?.label || id
    },
    focusNode(id) {
      const node = this.findNode(id)
      if (!node) return
      this.selectedNodeId = id
      this.setCenter(node.position.x + 115, node.position.y + 40, {
        zoom: 1.2,
        duration: 300,
      })
    },
    onNodesInitialized() {
      if (this.fitPending) {
        this.fitPending = false
        this.fitView({ padding: 0.15, maxZoom: 1 })
      }
    },
    miniMapColor(node) {
      return categoryColor(node.data?.kind)
    },
  },
}
</script>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';

.fa-root {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 118px);
}
.fa-toolbar {
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-flow-select {
  max-width: 280px;
}
.fa-main {
  flex: 1;
  display: flex;
  min-height: 0;
}
.fa-palette {
  width: 190px;
  overflow-y: auto;
  border-right: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-category {
  font-weight: 600;
  letter-spacing: 0.05em;
}
.fa-palette-item {
  display: flex;
  align-items: center;
  margin: 2px 8px;
  padding: 4px 8px;
  font-size: 13px;
  border-radius: 4px;
  border-left: 3px solid var(--fa-color);
  background: rgba(255, 255, 255, 0.04);
  cursor: grab;
  user-select: none;
}
.fa-palette-item:hover {
  background: rgba(255, 255, 255, 0.1);
}
.fa-center {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.fa-canvas {
  flex: 1;
  min-height: 0;
  position: relative;
}
.fa-flow {
  height: 100%;
  width: 100%;
  background-color: rgb(23, 38, 53);
}
.fa-flow .vue-flow__edge-path {
  stroke: #8fa3b5;
}
.fa-flow .vue-flow__edge.selected .vue-flow__edge-path {
  stroke: #4dacff;
}
.fa-flow .fa-edge-error .vue-flow__edge-path {
  stroke: #e57373;
}
.fa-flow .vue-flow__node-flow {
  padding: 0;
  border: none;
  background: transparent;
}
.fa-flow .vue-flow__minimap {
  background: rgb(31, 47, 64);
}
.fa-flow .vue-flow__minimap svg {
  width: 160px;
  height: 100px;
}
.fa-flow .vue-flow__controls-button {
  background: #2c3e50;
  border-bottom-color: #3a4a5a;
  fill: #e6edf3;
}
.fa-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #6b7f92;
  pointer-events: none;
}
.fa-bottom {
  height: 240px;
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-side {
  width: 320px;
  overflow-y: auto;
  border-left: 1px solid rgba(255, 255, 255, 0.12);
}
</style>
