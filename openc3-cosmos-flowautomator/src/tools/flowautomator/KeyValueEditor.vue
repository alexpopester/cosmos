<!--
# Edits a flat {key: value} object as rows. With parse-values the values
# are kept as JSON-ish literals (numbers, true/false) so starting variables
# get real types; otherwise values stay strings (templates).
-->

<template>
  <div>
    <div class="text-caption mb-1">{{ label }}</div>
    <div v-for="(row, i) in rows" :key="i" class="d-flex align-center mb-1">
      <v-text-field
        v-model="row.key"
        placeholder="name"
        density="compact"
        variant="outlined"
        hide-details
        class="mr-1 fa-mono"
        @update:model-value="emit"
      />
      <v-text-field
        v-model="row.value"
        placeholder="value"
        density="compact"
        variant="outlined"
        hide-details
        class="fa-mono"
        @update:model-value="emit"
      />
      <v-btn
        icon="mdi-close"
        size="x-small"
        variant="text"
        @click="remove(i)"
      />
    </div>
    <v-btn size="small" variant="tonal" prepend-icon="mdi-plus" @click="add">
      Add
    </v-btn>
    <div v-if="hint" class="text-caption text-medium-emphasis mt-1">
      {{ hint }}
    </div>
  </div>
</template>

<script>
export default {
  props: {
    modelValue: { type: Object, default: () => ({}) },
    label: { type: String, default: '' },
    hint: { type: String, default: '' },
    parseValues: { type: Boolean, default: false },
  },
  emits: ['update:modelValue'],
  data() {
    return { rows: [] }
  },
  watch: {
    modelValue: {
      immediate: true,
      handler(value) {
        // Only rebuild rows when the object changed from outside
        if (value !== this.lastEmitted) {
          this.rows = Object.entries(value || {}).map(([key, v]) => ({
            key,
            value: this.parseValues ? this.format(v) : String(v),
          }))
        }
      },
    },
  },
  methods: {
    format(value) {
      if (typeof value === 'string') return `'${value}'`
      if (value === true) return 'True'
      if (value === false) return 'False'
      if (value === null) return 'None'
      return String(value)
    },
    parse(text) {
      const trimmed = String(text).trim()
      if (trimmed === 'True' || trimmed === 'true') return true
      if (trimmed === 'False' || trimmed === 'false') return false
      if (trimmed === 'None' || trimmed === '') return null
      if (!isNaN(Number(trimmed))) return Number(trimmed)
      const quoted = trimmed.match(/^(['"])(.*)\1$/)
      return quoted ? quoted[2] : trimmed
    },
    emit() {
      const result = {}
      for (const row of this.rows) {
        if (row.key) {
          result[row.key] = this.parseValues ? this.parse(row.value) : row.value
        }
      }
      this.lastEmitted = result
      this.$emit('update:modelValue', result)
    },
    add() {
      this.rows.push({ key: '', value: '' })
    },
    remove(index) {
      this.rows.splice(index, 1)
      this.emit()
    },
  },
}
</script>
