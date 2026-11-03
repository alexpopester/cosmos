<!--
# A node on the Flow Automator canvas. Input on the left, one row per
# output on the right. The run being viewed colors it by its step status.
-->

<template>
  <div
    class="fa-node"
    :class="[
      `fa-status-${status}`,
      { 'fa-selected': selected, 'fa-has-problem': problems.length > 0 },
    ]"
    :style="{ '--fa-color': color }"
  >
    <Handle
      v-if="!spec.trigger"
      id="in"
      type="target"
      :position="Position.Left"
      class="fa-handle fa-handle-in"
    />
    <div class="fa-header">
      <v-icon size="16" class="mr-1">{{ spec.icon }}</v-icon>
      <span class="fa-title">{{ data.config.label || spec.label }}</span>
      <v-spacer />
      <v-icon
        v-if="data.config.require_approval && !spec.trigger"
        size="14"
        color="warning"
        class="mr-1"
        title="Asks the operator first in walk mode"
      >
        mdi-hand-back-right-outline
      </v-icon>
      <v-icon v-if="status === 'waiting'" size="14" color="warning">
        mdi-hand-back-right
      </v-icon>
      <v-icon v-else-if="status === 'running'" size="14" class="fa-spin">
        mdi-loading
      </v-icon>
      <v-icon v-else-if="status === 'done'" size="14" color="success">
        mdi-check-circle
      </v-icon>
      <v-icon v-else-if="status === 'error'" size="14" color="error">
        mdi-alert-circle
      </v-icon>
      <v-icon v-else-if="status === 'stopped'" size="14" color="warning">
        mdi-stop-circle
      </v-icon>
      <v-tooltip v-if="problems.length" location="top">
        <template #activator="{ props: tip }">
          <v-icon v-bind="tip" size="14" color="error" class="ml-1">
            mdi-alert
          </v-icon>
        </template>
        <div v-for="(p, i) in problems" :key="i">{{ p }}</div>
      </v-tooltip>
    </div>
    <div class="fa-summary" :title="summary">{{ summary }}</div>
    <div class="fa-outputs">
      <div
        v-for="output in outputs"
        :key="output"
        class="fa-output"
        :class="{ 'fa-output-error': output === 'error' }"
      >
        <span class="fa-output-label">{{ outputLabel(output) }}</span>
        <Handle
          :id="output"
          type="source"
          :position="Position.Right"
          class="fa-handle"
          :class="{ 'fa-handle-error': output === 'error' }"
        />
      </div>
    </div>
  </div>
</template>

<script>
import { Handle, Position } from '@vue-flow/core'
import { NODE_TYPES, categoryColor, outputsFor } from './nodeTypes'

export default {
  components: { Handle },
  inject: ['flowContext'],
  props: {
    id: { type: String, required: true },
    data: { type: Object, required: true },
    selected: { type: Boolean, default: false },
  },
  setup() {
    return { Position }
  },
  computed: {
    spec() {
      return NODE_TYPES[this.data.kind] || { label: this.data.kind, fields: [] }
    },
    color() {
      return categoryColor(this.data.kind)
    },
    outputs() {
      return outputsFor(this.data.kind, this.data.config)
    },
    summary() {
      if (!this.spec.summary) return ''
      return this.spec.summary(this.data.config, this.flowContext) || ''
    },
    status() {
      return this.flowContext.nodeStatus[this.id] || 'idle'
    },
    problems() {
      return this.flowContext.nodeProblems[this.id] || []
    },
  },
  methods: {
    outputLabel(output) {
      if (this.spec.outputLabel) {
        return this.spec.outputLabel(output, this.data.config)
      }
      return output === 'out' ? '' : output
    },
  },
}
</script>

<style>
.fa-node {
  width: 230px;
  border-radius: 6px;
  border: 1px solid #44515e;
  border-left: 4px solid var(--fa-color);
  background: rgb(31, 47, 64);
  color: #e6edf3;
  font-size: 12px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}
.fa-node.fa-selected {
  outline: 2px solid #4dacff;
}
.fa-node.fa-has-problem {
  border-color: #c75050;
}
.fa-node.fa-status-running {
  box-shadow: 0 0 0 3px rgba(77, 172, 255, 0.8);
  animation: fa-pulse 1s ease-in-out infinite alternate;
}
.fa-node.fa-status-waiting {
  box-shadow: 0 0 0 3px rgba(255, 179, 0, 0.9);
  animation: fa-pulse-waiting 0.8s ease-in-out infinite alternate;
}
@keyframes fa-pulse-waiting {
  from {
    box-shadow: 0 0 0 2px rgba(255, 179, 0, 0.4);
  }
  to {
    box-shadow: 0 0 0 6px rgba(255, 179, 0, 1);
  }
}
.fa-node.fa-status-done {
  box-shadow: 0 0 0 2px rgba(76, 175, 80, 0.8);
}
.fa-node.fa-status-error {
  box-shadow: 0 0 0 3px rgba(229, 57, 53, 0.9);
}
.fa-node.fa-status-stopped {
  box-shadow: 0 0 0 2px rgba(255, 179, 0, 0.8);
}
@keyframes fa-pulse {
  from {
    box-shadow: 0 0 0 2px rgba(77, 172, 255, 0.4);
  }
  to {
    box-shadow: 0 0 0 5px rgba(77, 172, 255, 0.9);
  }
}
.fa-header {
  display: flex;
  align-items: center;
  padding: 4px 8px;
  font-weight: 600;
  background: rgba(255, 255, 255, 0.04);
  border-bottom: 1px solid #36424e;
}
.fa-header .v-icon:first-child {
  color: var(--fa-color);
}
.fa-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fa-summary {
  padding: 4px 8px;
  font-family: monospace;
  font-size: 11px;
  color: #b8c4cf;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-all;
  min-height: 14px;
}
.fa-outputs {
  padding-bottom: 4px;
}
.fa-output {
  position: relative;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding-right: 12px;
}
.fa-output-label {
  font-size: 10px;
  color: #9fb0bf;
  text-transform: uppercase;
}
.fa-output-error .fa-output-label {
  color: #e57373;
}
.fa-node .fa-handle {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #9fb0bf;
  border: 2px solid rgb(31, 47, 64);
}
.fa-node .fa-handle-in {
  width: 10px;
  height: 18px;
  border-radius: 3px;
}
.fa-node .fa-handle-error {
  background: #e57373;
}
.fa-spin {
  animation: fa-rotate 1s linear infinite;
}
@keyframes fa-rotate {
  to {
    transform: rotate(360deg);
  }
}
</style>
