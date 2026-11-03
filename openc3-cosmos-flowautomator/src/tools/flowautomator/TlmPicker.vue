<!--
# Pick a telemetry item (or just a packet) with COSMOS's own target /
# packet / item chooser. Emits pick({ target, packet, item, valueType }).
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="900"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-tlm-picker">
      <v-card-title>{{ title }}</v-card-title>
      <v-card-text>
        <target-packet-item-chooser
          v-if="modelValue"
          :choose-item="chooseItem"
          :select-types="chooseItem && selectTypes"
          :initial-target-name="initial.target"
          :initial-packet-name="initial.packet"
          :initial-item-name="initial.item"
          mode="tlm"
          @on-set="selected = $event"
        />
        <div v-if="preview" class="text-caption text-medium-emphasis">
          Inserts <code>{{ preview }}</code>
        </div>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="$emit('update:modelValue', false)">
          Cancel
        </v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :disabled="!ready"
          class="fa-tlm-pick"
          @click="pick"
        >
          {{ chooseItem ? 'Use item' : 'Use packet' }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script>
import { TargetPacketItemChooser } from '@openc3/vue-common/components'

export default {
  components: { TargetPacketItemChooser },
  props: {
    modelValue: { type: Boolean, default: false },
    chooseItem: { type: Boolean, default: true },
    selectTypes: { type: Boolean, default: true },
    // 'condition' inserts TGT PKT ITEM, 'template' {{ tlm('TGT PKT ITEM') }}, 'item' just the name
    insertAs: { type: String, default: 'item' },
    // "TGT PKT ITEM" or "TGT PKT" to start from
    current: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'pick'],
  data() {
    return { selected: null }
  },
  computed: {
    title() {
      return this.chooseItem ? 'Pick a telemetry item' : 'Pick a packet'
    },
    initial() {
      const parts = String(this.current || '')
        .trim()
        .split(/\s+/)
      const looksLikeItem = parts.every((p) => /^[A-Z][A-Z0-9_]*$/.test(p))
      if (!looksLikeItem) return { target: '', packet: '', item: '' }
      return {
        target: parts[0] || '',
        packet: parts[1] || '',
        item: parts[2] || '',
      }
    },
    ready() {
      const s = this.selected
      return !!(
        s &&
        s.targetName &&
        s.packetName &&
        (!this.chooseItem || s.itemName)
      )
    },
    preview() {
      return this.ready ? this.textFor(this.selected) : ''
    },
  },
  watch: {
    modelValue(open) {
      if (open) this.selected = null
    },
  },
  methods: {
    textFor(s) {
      const name = this.chooseItem
        ? `${s.targetName} ${s.packetName} ${s.itemName}`
        : `${s.targetName} ${s.packetName}`
      const fn =
        {
          RAW: 'tlm_raw',
          FORMATTED: 'tlm_formatted',
          WITH_UNITS: 'tlm_with_units',
        }[s.valueType] || 'tlm'
      if (this.insertAs === 'template') return `{{ ${fn}('${name}') }}`
      if (this.insertAs === 'condition')
        return fn === 'tlm' ? name : `${fn}('${name}')`
      return name
    },
    pick() {
      const s = this.selected
      this.$emit('pick', {
        target: s.targetName,
        packet: s.packetName,
        item: s.itemName,
        valueType: s.valueType,
        text: this.textFor(s),
      })
      this.$emit('update:modelValue', false)
    },
  },
}
</script>
