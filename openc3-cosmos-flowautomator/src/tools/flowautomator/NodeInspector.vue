<!--
# Right-hand panel: edits the selected node's settings, or the flow's
# settings when nothing is selected. Edits are made in place and reported
# with a 'changed' event so the editor can mark the flow unsaved.
-->

<template>
  <div class="fa-inspector">
    <template v-if="node && spec">
      <div class="d-flex align-center mb-1">
        <v-icon class="mr-2" :color="color">{{ spec.icon }}</v-icon>
        <span class="text-subtitle-1">{{ spec.label }}</span>
        <v-spacer />
        <v-btn
          icon="mdi-delete"
          size="small"
          variant="text"
          title="Delete node"
          @click="$emit('delete-node')"
        />
      </div>
      <div class="text-caption text-medium-emphasis mb-3">
        {{ spec.description }}
      </div>
      <v-alert
        v-for="(problem, i) in problems"
        :key="i"
        type="error"
        density="compact"
        variant="tonal"
        class="mb-2 text-caption"
      >
        {{ problem }}
      </v-alert>
      <v-text-field
        v-model="config.label"
        label="Label"
        density="compact"
        variant="outlined"
        :placeholder="spec.label"
        persistent-placeholder
        hide-details
        class="mb-3"
        @update:model-value="changed"
      />
      <v-switch
        v-if="!spec.trigger"
        v-model="config.require_approval"
        label="Ask operator first (walk mode)"
        hint="In walk mode the run pauses here until an operator approves. Crawl mode asks at every step; run mode never asks."
        persistent-hint
        color="warning"
        density="compact"
        class="mb-3"
        @update:model-value="changed"
      />
      <template v-for="field in visibleFields" :key="field.key">
        <template
          v-if="field.type === 'condition' || field.type === 'template'"
        >
          <v-textarea
            v-model="config[field.key]"
            :label="field.label"
            :hint="field.hint"
            persistent-hint
            density="compact"
            variant="outlined"
            rows="2"
            auto-grow
            append-inner-icon="mdi-satellite-variant"
            class="mb-3 fa-mono"
            :class="`fa-field-${field.key}`"
            @click:append-inner="pickTlm(field)"
            @update:model-value="changed"
            @focusout="rememberCaret(field.key, $event)"
          />
          <div v-if="field.builder" class="mb-3">
            <v-btn
              size="small"
              variant="tonal"
              prepend-icon="mdi-console-line"
              class="fa-build-command"
              @click="builder = { show: true, key: field.key }"
            >
              Build command…
            </v-btn>
          </div>
          <div v-else class="mb-2" />
        </template>
        <v-text-field
          v-else-if="field.type === 'item'"
          v-model="config[field.key]"
          :label="field.label"
          :hint="field.hint"
          :persistent-hint="!!field.hint"
          density="compact"
          variant="outlined"
          append-inner-icon="mdi-satellite-variant"
          class="mb-3 fa-mono"
          :class="`fa-field-${field.key}`"
          @click:append-inner="pickTlm(field)"
          @update:model-value="changed"
        />
        <div v-else-if="field.type === 'packet'" class="d-flex mb-3">
          <v-text-field
            v-model="config[field.key]"
            label="Target"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-1 fa-mono"
            @update:model-value="changed"
          />
          <v-text-field
            v-model="config[field.packetKey]"
            label="Packet"
            density="compact"
            variant="outlined"
            hide-details
            append-inner-icon="mdi-package-variant"
            class="fa-mono fa-field-packet"
            @click:append-inner="pickTlm(field)"
            @update:model-value="changed"
          />
        </div>
        <v-text-field
          v-else-if="field.type === 'text'"
          v-model="config[field.key]"
          :label="field.label"
          :hint="field.hint"
          :persistent-hint="!!field.hint"
          density="compact"
          variant="outlined"
          class="mb-3"
          @update:model-value="changed"
        />
        <v-text-field
          v-else-if="field.type === 'number'"
          v-model.number="config[field.key]"
          type="number"
          :label="field.label"
          :hint="field.hint"
          :persistent-hint="!!field.hint"
          density="compact"
          variant="outlined"
          class="mb-3"
          @update:model-value="changed"
        />
        <v-select
          v-else-if="field.type === 'select'"
          v-model="config[field.key]"
          :items="field.options"
          :label="field.label"
          density="compact"
          variant="outlined"
          hide-details
          class="mb-3"
          @update:model-value="changed"
        />
        <v-select
          v-else-if="field.type === 'multiselect'"
          v-model="config[field.key]"
          :items="field.options"
          :label="field.label"
          multiple
          chips
          closable-chips
          density="compact"
          variant="outlined"
          hide-details
          class="mb-3"
          @update:model-value="changed"
        />
        <v-switch
          v-else-if="field.type === 'boolean'"
          v-model="config[field.key]"
          :label="field.label"
          :hint="field.hint"
          :persistent-hint="!!field.hint"
          color="primary"
          density="compact"
          class="mb-2"
          @update:model-value="changed"
        />
        <v-combobox
          v-else-if="field.type === 'script'"
          v-model="config[field.key]"
          :items="scripts"
          :label="field.label"
          :hint="field.hint"
          persistent-hint
          density="compact"
          variant="outlined"
          class="mb-3"
          @update:model-value="changed"
        />
        <v-select
          v-else-if="field.type === 'flow'"
          v-model="config[field.key]"
          :items="flowItems"
          item-title="name"
          item-value="id"
          :label="field.label"
          density="compact"
          variant="outlined"
          hint="The flow must have a Manual trigger"
          persistent-hint
          class="mb-3"
          @update:model-value="changed"
        />
        <div v-else-if="field.type === 'list'" class="mb-3">
          <div class="text-caption mb-1">{{ field.label }}</div>
          <div
            v-for="(item, i) in config[field.key]"
            :key="i"
            class="d-flex align-center mb-1"
          >
            <v-text-field
              v-model="config[field.key][i]"
              density="compact"
              variant="outlined"
              hide-details
              class="fa-mono"
              @update:model-value="changed"
            />
            <v-btn
              icon="mdi-close"
              size="x-small"
              variant="text"
              @click="removeItem(field.key, i)"
            />
          </div>
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-plus"
            @click="addItem(field.key)"
          >
            Add
          </v-btn>
          <div class="text-caption text-medium-emphasis mt-1">
            {{ field.hint }}
          </div>
        </div>
        <div v-else-if="field.type === 'keyvalue'" class="mb-3">
          <key-value-editor
            v-model="config[field.key]"
            :label="field.label"
            :hint="field.hint"
            @update:model-value="changed"
          />
        </div>
      </template>
    </template>

    <template v-else-if="flow">
      <v-text-field
        v-model="settings.name"
        label="Name"
        density="compact"
        variant="outlined"
        class="mb-3"
        hide-details
        @update:model-value="changed"
      />
      <v-textarea
        v-model="settings.description"
        label="Description"
        density="compact"
        variant="outlined"
        rows="2"
        auto-grow
        hide-details
        class="mb-3"
        @update:model-value="changed"
      />
      <v-select
        v-model="settings.policy"
        :items="policies"
        label="If triggered while running"
        density="compact"
        variant="outlined"
        class="mb-3"
        persistent-hint
        :hint="policyHint"
        @update:model-value="changed"
      />
      <v-select
        v-model="settings.mode"
        :items="modes"
        item-title="title"
        item-value="value"
        label="Execution mode"
        density="compact"
        variant="outlined"
        class="mb-3"
        persistent-hint
        :hint="modeHint"
        @update:model-value="changed"
      >
        <template #item="{ props: itemProps, item }">
          <v-list-item
            v-bind="itemProps"
            :prepend-icon="item.raw.icon"
            :subtitle="item.raw.hint"
          />
        </template>
      </v-select>
      <v-text-field
        v-if="settings.mode !== 'run'"
        v-model.number="settings.approval_timeout"
        type="number"
        min="0"
        label="Approval timeout (s)"
        hint="Fail the run if nobody answers in time. 0 waits forever."
        persistent-hint
        density="compact"
        variant="outlined"
        class="mb-3"
        @update:model-value="changed"
      />
      <v-switch
        v-model="settings.template"
        label="Template for scheduled activities"
        hint="Templates can be scheduled into contacts from the Contacts view. Runs started that way also get contact_aos, contact_los and the other contact variables."
        persistent-hint
        color="primary"
        density="compact"
        class="mb-3 fa-template-switch"
        @update:model-value="changed"
      />
      <v-switch
        v-model="settings.dry_run"
        label="Dry-run mode"
        hint="Every run of this flow is a dry run, triggers included: commands are checked but not sent. Use it to commission triggers on live telemetry."
        persistent-hint
        color="info"
        density="compact"
        class="mb-3 fa-dry-run-switch"
        @update:model-value="changed"
      />
      <div v-if="settings.template" class="mb-4">
        <div class="text-caption mb-1">Parameters</div>
        <parameter-editor
          v-model="settings.parameters"
          @update:model-value="changed"
        />
      </div>
      <key-value-editor
        v-model="settings.variables"
        label="Starting variables"
        hint="Values are expressions: 5, 'text', True"
        parse-values
        @update:model-value="changed"
      />
      <div class="text-caption text-medium-emphasis mt-4">
        Drag nodes from the palette, then drag from an output dot to a node's
        input to wire them. Select a node or wire and press Delete to remove it.
      </div>
    </template>
    <tlm-picker
      v-model="picker.show"
      :choose-item="picker.chooseItem"
      :insert-as="picker.insertAs"
      :current="picker.current"
      @pick="onPick"
    />
    <command-builder
      v-model="builder.show"
      :current="node && builder.key ? config[builder.key] : ''"
      @pick="onBuild"
    />
  </div>
</template>

<script>
import { MODES, NODE_TYPES, categoryColor } from './nodeTypes'
import KeyValueEditor from './KeyValueEditor.vue'
import ParameterEditor from './ParameterEditor.vue'
import TlmPicker from './TlmPicker.vue'
import CommandBuilder from './CommandBuilder.vue'

export default {
  components: { KeyValueEditor, ParameterEditor, TlmPicker, CommandBuilder },
  props: {
    node: { type: Object, default: null },
    flow: { type: Object, default: null },
    flows: { type: Array, default: () => [] },
    scripts: { type: Array, default: () => [] },
    problems: { type: Array, default: () => [] },
  },
  emits: ['changed', 'delete-node', 'list-removed'],
  data() {
    return {
      carets: {}, // field key -> cursor position when it last lost focus
      picker: {
        show: false,
        key: null,
        chooseItem: true,
        insertAs: 'item',
        current: '',
      },
      builder: { show: false, key: null },
      modes: MODES,
      policies: [
        { title: 'Skip the new trigger', value: 'skip' },
        { title: 'Queue it', value: 'queue' },
        { title: 'Run another copy', value: 'parallel' },
      ],
    }
  },
  computed: {
    spec() {
      return this.node ? NODE_TYPES[this.node.data.kind] : null
    },
    // The node's config and the flow settings are edited in place; the
    // editor owns them and saves them when it saves the flow
    config() {
      return this.node.data.config
    },
    settings() {
      return this.flow
    },
    color() {
      return categoryColor(this.node.data.kind)
    },
    visibleFields() {
      return this.spec.fields.filter((f) => !f.show || f.show(this.config))
    },
    flowItems() {
      return this.flows.filter((f) => !this.flow || f.id !== this.flow.id)
    },
    modeHint() {
      return MODES.find((m) => m.value === this.flow.mode)?.hint
    },
    policyHint() {
      return {
        skip: 'Ignore triggers while a run of this flow is active',
        queue: 'Start queued runs one after another (up to 10 waiting)',
        parallel: 'Start a new run alongside the active one',
      }[this.flow.policy]
    },
  },
  methods: {
    changed() {
      this.$emit('changed')
    },
    // Telemetry picker: insert into a condition/template, or set an item or packet field
    pickTlm(field) {
      const packetOnly = field.type === 'packet'
      this.picker = {
        show: true,
        key: field.key,
        packetKey: field.packetKey,
        chooseItem: !packetOnly,
        insertAs: field.type === 'item' || packetOnly ? 'item' : field.type,
        current: packetOnly
          ? `${this.config[field.key] || ''} ${this.config[field.packetKey] || ''}`
          : field.type === 'item'
            ? this.config[field.key]
            : '',
      }
    },
    onPick(selection) {
      const { key, packetKey, insertAs } = this.picker
      if (packetKey) {
        this.config[key] = selection.target
        this.config[packetKey] = selection.packet
      } else if (insertAs === 'item') {
        this.config[key] = selection.text
      } else {
        // Insert where the cursor was (the end if the field was never focused), with spaces around it
        const existing = String(this.config[key] || '')
        const at = Math.min(
          this.carets[key] ?? existing.length,
          existing.length,
        )
        const before = existing.slice(0, at)
        const after = existing.slice(at)
        const left = before && !/\s$/.test(before) ? ' ' : ''
        const right = after && !/^\s/.test(after) ? ' ' : ''
        this.config[key] = `${before}${left}${selection.text}${right}${after}`
        this.carets[key] = at + left.length + selection.text.length
      }
      this.changed()
    },
    rememberCaret(key, event) {
      const target = event.target
      if (target && typeof target.selectionStart === 'number') {
        this.carets[key] = target.selectionStart
      }
    },
    onBuild(command) {
      this.config[this.builder.key] = command
      this.changed()
    },
    addItem(key) {
      if (!Array.isArray(this.config[key])) this.config[key] = []
      this.config[key].push('')
      this.changed()
    },
    removeItem(key, index) {
      this.config[key].splice(index, 1)
      // Before 'changed': the editor renumbers wires that follow list positions (a Switch's case_N outputs)
      this.$emit('list-removed', { key, index })
      this.changed()
    },
  },
}
</script>

<style>
.fa-inspector {
  padding: 12px;
}
.fa-mono textarea,
.fa-mono input {
  font-family: monospace;
  font-size: 12px;
}
</style>
