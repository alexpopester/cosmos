<!--
# Build a command with COSMOS's Command Sender form: pick the target and
# command, fill in its parameters (states, ranges, required and hazardous
# checks come from the command definition). Starts from the node's current
# command. Emits pick(commandString). To use a variable, edit the text
# afterwards: TYPE '{{ kind }}'.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="1100"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-command-builder">
      <v-card-title>Build a command</v-card-title>
      <v-card-text>
        <command-editor
          v-if="modelValue"
          ref="editor"
          :cmd-string="startFrom"
          :show-command-button="false"
          :show-queue-select="false"
          @command-changed="error = ''"
        />
        <div class="text-caption text-medium-emphasis mt-2">
          After inserting you can replace any value with a variable or
          expression, e.g. <code v-pre>TYPE '{{ kind }}'</code> or
          <code v-pre>DURATION {{ round(t, 1) }}</code
          >.
        </div>
        <v-alert v-if="error" type="error" density="compact" class="mt-2">
          {{ error }}
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="$emit('update:modelValue', false)">
          Cancel
        </v-btn>
        <v-btn
          color="primary"
          variant="flat"
          class="fa-command-use"
          @click="use"
        >
          Use command
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script>
import { CommandEditor } from '@openc3/vue-common/components'

export default {
  components: { CommandEditor },
  props: {
    modelValue: { type: Boolean, default: false },
    current: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'pick'],
  data() {
    return { error: '' }
  },
  computed: {
    // Only start from the current command if it has no {{ }}: the editor can't show those
    startFrom() {
      const text = String(this.current || '').trim()
      return text && !text.includes('{{') ? text : null
    },
  },
  methods: {
    use() {
      try {
        const command = this.$refs.editor.getCmdString()
        if (!command || command.includes('undefined')) {
          this.error = 'Pick a target and a command first'
          return
        }
        this.$emit('pick', command)
        this.$emit('update:modelValue', false)
      } catch (error) {
        this.error = error.message || String(error)
      }
    },
  },
}
</script>
