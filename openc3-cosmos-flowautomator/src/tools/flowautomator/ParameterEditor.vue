<!--
# Declares a template's parameters: name, type, default, required and
# choices. Each one becomes a run variable of the same name.
-->

<template>
  <div>
    <div v-for="(p, i) in modelValue" :key="i" class="fa-param mb-2">
      <div class="d-flex align-center mb-1">
        <v-text-field
          v-model="p.name"
          placeholder="name"
          density="compact"
          variant="outlined"
          hide-details
          class="mr-1 fa-mono"
          @update:model-value="changed"
        />
        <v-select
          v-model="p.type"
          :items="types"
          density="compact"
          variant="outlined"
          hide-details
          class="fa-param-type"
          @update:model-value="changed"
        />
        <v-btn
          icon="mdi-close"
          size="x-small"
          variant="text"
          @click="remove(i)"
        />
      </div>
      <v-text-field
        v-if="p.type === 'choice'"
        :model-value="(p.choices || []).join(', ')"
        placeholder="choices, comma separated"
        density="compact"
        variant="outlined"
        hide-details
        class="mb-1 fa-mono"
        @update:model-value="setChoices(p, $event)"
      />
      <div class="d-flex align-center">
        <v-text-field
          v-model="p.default"
          placeholder="default"
          density="compact"
          variant="outlined"
          hide-details
          class="mr-2 fa-mono"
          @update:model-value="changed"
        />
        <v-checkbox
          v-model="p.required"
          label="Required"
          density="compact"
          hide-details
          class="flex-grow-0"
          @update:model-value="changed"
        />
      </div>
      <div v-if="p.type === 'string'" class="d-flex mt-1">
        <v-text-field
          v-model="p.pattern"
          placeholder="pattern (regex), e.g. [A-Z0-9_]+"
          density="compact"
          variant="outlined"
          hide-details
          class="mr-1 fa-mono"
          @update:model-value="changed"
        />
        <v-text-field
          v-model="p.max_length"
          placeholder="max length"
          type="number"
          density="compact"
          variant="outlined"
          hide-details
          class="fa-param-small"
          @update:model-value="changed"
        />
      </div>
      <div v-if="p.type === 'number'" class="d-flex align-center mt-1">
        <v-text-field
          v-model="p.min"
          placeholder="min"
          type="number"
          density="compact"
          variant="outlined"
          hide-details
          class="mr-1 fa-param-small"
          @update:model-value="changed"
        />
        <v-text-field
          v-model="p.max"
          placeholder="max"
          type="number"
          density="compact"
          variant="outlined"
          hide-details
          class="mr-1 fa-param-small"
          @update:model-value="changed"
        />
        <v-checkbox
          v-model="p.integer"
          label="Whole"
          density="compact"
          hide-details
          class="flex-grow-0"
          @update:model-value="changed"
        />
      </div>
      <v-text-field
        v-model="p.description"
        placeholder="description (shown when scheduling)"
        density="compact"
        variant="plain"
        hide-details
        class="text-caption"
        @update:model-value="changed"
      />
    </div>
    <v-btn size="small" variant="tonal" prepend-icon="mdi-plus" @click="add">
      Add parameter
    </v-btn>
    <div class="text-caption text-medium-emphasis mt-1">
      Use a parameter as a variable: <code v-pre>{{ name }}</code> in commands,
      or name in conditions.
    </div>
  </div>
</template>

<script>
export default {
  props: {
    modelValue: { type: Array, default: () => [] },
  },
  emits: ['update:modelValue'],
  data() {
    return {
      types: [
        { title: 'text', value: 'string' },
        { title: 'number', value: 'number' },
        { title: 'yes/no', value: 'boolean' },
        { title: 'choice', value: 'choice' },
        { title: 'telemetry item', value: 'item' },
        { title: 'command', value: 'command' },
        { title: 'list', value: 'list' },
      ],
    }
  },
  methods: {
    changed() {
      this.$emit('update:modelValue', this.modelValue)
    },
    add() {
      this.$emit('update:modelValue', [
        ...this.modelValue,
        {
          name: `param${this.modelValue.length + 1}`,
          type: 'string',
          default: '',
          required: false,
          description: '',
        },
      ])
    },
    remove(index) {
      this.$emit(
        'update:modelValue',
        this.modelValue.filter((_p, i) => i !== index),
      )
    },
    setChoices(param, text) {
      param.choices = text
        .split(',')
        .map((c) => c.trim())
        .filter(Boolean)
      this.changed()
    },
  },
}
</script>

<style>
.fa-param {
  border-left: 2px solid rgba(224, 123, 57, 0.6);
  padding-left: 6px;
}
.fa-param-small {
  max-width: 110px;
}
.fa-param-type {
  max-width: 110px;
}
</style>
