<!--
# Inputs for a template's declared parameters (templates.py). Emits the
# values keyed by parameter name; empty inputs fall back to the default.
-->

<template>
  <div>
    <div
      v-if="!parameters.length"
      class="text-caption text-medium-emphasis mb-2"
    >
      This flow declares no parameters.
    </div>
    <template v-for="p in parameters" :key="p.name">
      <v-select
        v-if="p.type === 'choice'"
        :model-value="modelValue[p.name] ?? p.default"
        :items="(p.choices || []).map(String)"
        :label="label(p)"
        :hint="p.description"
        persistent-hint
        density="compact"
        variant="outlined"
        class="mb-3"
        :data-param="p.name"
        @update:model-value="set(p.name, $event)"
      />
      <v-switch
        v-else-if="p.type === 'boolean'"
        :model-value="asBool(modelValue[p.name] ?? p.default)"
        :label="label(p)"
        :hint="p.description"
        persistent-hint
        color="primary"
        density="compact"
        class="mb-2"
        :data-param="p.name"
        @update:model-value="set(p.name, $event)"
      />
      <v-text-field
        v-else
        :model-value="modelValue[p.name] ?? ''"
        :type="p.type === 'number' ? 'number' : 'text'"
        :label="label(p)"
        :placeholder="p.default != null ? String(p.default) : ''"
        persistent-placeholder
        :hint="p.description"
        persistent-hint
        :rules="[(v) => !p.required || hasValue(v, p) || 'Required']"
        density="compact"
        variant="outlined"
        class="mb-3 fa-mono"
        :data-param="p.name"
        @update:model-value="set(p.name, $event)"
      />
    </template>
  </div>
</template>

<script>
export default {
  props: {
    parameters: { type: Array, default: () => [] },
    modelValue: { type: Object, default: () => ({}) },
  },
  emits: ['update:modelValue'],
  methods: {
    label(p) {
      return `${p.name}${p.required ? ' *' : ''}`
    },
    asBool(value) {
      return (
        value === true || ['true', 'yes', '1', 'on'].includes(String(value))
      )
    },
    hasValue(value, p) {
      return (
        (value !== '' && value != null) ||
        (p.default !== '' && p.default != null)
      )
    },
    set(name, value) {
      this.$emit('update:modelValue', { ...this.modelValue, [name]: value })
    },
  },
}
</script>
