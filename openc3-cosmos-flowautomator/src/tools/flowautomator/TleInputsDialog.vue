<!--
# Where other apps can send TLEs (tle_inputs.py): a COSMOS stash key, files
# in the COSMOS bucket, telemetry packets, and the HTTP API. Plus the jump
# limit that holds suspicious TLEs for review, and each input's last result.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="960"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-tle-inputs">
      <v-card-title class="d-flex align-center">
        <v-icon class="mr-2">mdi-import</v-icon>TLE inputs
      </v-card-title>
      <v-card-text v-if="settings">
        <div class="text-caption text-medium-emphasis mb-3">
          Other apps can keep TLEs current through any of these. Every TLE is
          matched by catalog number (then name), must be newer than the current
          one, gives way to a satellite's preferred source while that one is
          fresh, and is held for review if it moves the satellite more than the
          jump limit. The engine checks the stash, files and packets every
          {{ settings.poll_seconds }}s.
        </div>
        <div class="d-flex">
          <v-text-field
            v-model.number="settings.max_jump_km"
            type="number"
            label="Hold for review if it moves the satellite more than (km)"
            density="compact"
            variant="outlined"
            class="mr-2"
            data-field="max-jump"
          />
          <v-text-field
            v-model.number="settings.poll_seconds"
            type="number"
            label="Check inputs every (s)"
            density="compact"
            variant="outlined"
          />
        </div>

        <div class="text-subtitle-2 mt-2">COSMOS stash</div>
        <v-text-field
          v-model="settings.stash_key"
          label="Stash key (empty to turn off)"
          density="compact"
          variant="outlined"
          class="fa-mono"
          hide-details
        />
        <pre class="fa-snippet">{{ stashExample }}</pre>
        <div class="text-caption" :class="statusClass(settings.status.stash)">
          {{ statusText(settings.status.stash) }}
        </div>

        <div class="text-subtitle-2 mt-4">Files in the COSMOS bucket</div>
        <v-textarea
          v-model="filesText"
          label="Target paths, one per line"
          placeholder="INST/tle/latest.tle"
          rows="2"
          auto-grow
          density="compact"
          variant="outlined"
          class="fa-mono"
          hide-details
          data-field="tle-files"
        />
        <div class="text-caption text-medium-emphasis mt-1">
          Read again whenever the file changes. Write one from a script with
          <code>put_target_file("INST/tle/latest.tle", text)</code>, or upload
          it in Bucket Explorer under config/{{ scope }}/targets_modified.
        </div>
        <div
          v-for="(s, path) in settings.status.files"
          :key="path"
          class="text-caption"
        >
          <code>{{ path }}</code
          >:
          <span :class="statusClass(s)">{{ statusText(s) }}</span>
        </div>

        <div class="d-flex align-center mt-4">
          <span class="text-subtitle-2">Telemetry packets</span>
          <v-spacer />
          <v-btn
            size="x-small"
            variant="tonal"
            prepend-icon="mdi-plus"
            @click="addPacket"
          >
            Add packet
          </v-btn>
        </div>
        <div class="text-caption text-medium-emphasis mb-1">
          Read when the packet's count goes up: one item holding all the TLE
          lines, or separate line 1 and line 2 items.
        </div>
        <div
          v-for="(p, i) in settings.packets"
          :key="i"
          class="d-flex align-center mb-1"
        >
          <v-text-field
            v-model="p.target"
            placeholder="TARGET"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-1 fa-mono"
          />
          <v-text-field
            v-model="p.packet"
            placeholder="PACKET"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-1 fa-mono"
          />
          <v-text-field
            v-model="p.item"
            placeholder="item with all lines"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-1 fa-mono"
          />
          <span class="text-caption mx-1">or</span>
          <v-text-field
            v-model="p.line1"
            placeholder="LINE1 item"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-1 fa-mono"
          />
          <v-text-field
            v-model="p.line2"
            placeholder="LINE2 item"
            density="compact"
            variant="outlined"
            hide-details
            class="fa-mono"
          />
          <v-btn
            icon="mdi-close"
            size="x-small"
            variant="text"
            @click="settings.packets.splice(i, 1)"
          />
        </div>
        <div
          v-for="(s, name) in settings.status.packets"
          :key="name"
          class="text-caption"
        >
          <code>{{ name }}</code
          >:
          <span :class="statusClass(s)">{{ statusText(s) }}</span>
        </div>

        <div class="text-subtitle-2 mt-4">HTTP API</div>
        <pre class="fa-snippet">{{ curlExample }}</pre>
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
          :loading="saving"
          class="fa-tle-inputs-save"
          @click="save"
        >
          Save
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
  },
  emits: ['update:modelValue'],
  data() {
    return { settings: null, filesText: '', saving: false, error: '' }
  },
  computed: {
    scope() {
      return window.openc3Scope || 'DEFAULT'
    },
    stashExample() {
      const key = this.settings?.stash_key || 'flow_automator_tles'
      return `# From any COSMOS script (Python; Ruby is the same):
stash_set("${key}", {"tle": tle_text, "source": "my-pipeline"})
# ... a few seconds later, what happened to each TLE:
print(stash_get("${key}_result"))`
    },
    curlExample() {
      return `curl -X POST "${window.location.origin}/flow-automator/api/tles?scope=${this.scope}" \\
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \\
  -d "{\\"tle\\": \\"$(sed 's/$/\\\\n/' latest.tle | tr -d '\\n')\\", \\"source\\": \\"fds\\"}"`
    },
  },
  watch: {
    modelValue(open) {
      if (open) this.load()
    },
  },
  methods: {
    async load() {
      this.error = ''
      try {
        this.settings = await flowApi.tleInputs()
        this.filesText = this.settings.files.join('\n')
      } catch (error) {
        this.error = errorInfo(error).message
      }
    },
    addPacket() {
      this.settings.packets.push({
        target: '',
        packet: '',
        item: '',
        line1: '',
        line2: '',
      })
    },
    async save() {
      this.saving = true
      this.error = ''
      try {
        await flowApi.saveTleInputs({
          stash_key: this.settings.stash_key,
          files: this.filesText
            .split('\n')
            .map((f) => f.trim())
            .filter(Boolean),
          packets: this.settings.packets,
          max_jump_km: Number(this.settings.max_jump_km),
          poll_seconds: Number(this.settings.poll_seconds),
        })
        this.$emit('update:modelValue', false)
      } catch (error) {
        this.error = errorInfo(error).message
      } finally {
        this.saving = false
      }
    },
    statusText(status) {
      if (!status) return 'nothing received yet'
      const when = new Date(status.time * 1000).toISOString().slice(11, 19)
      if (status.error) return `${when} UTC: ${status.error}`
      const counts = Object.entries(status.counts || {})
        .filter(([, n]) => n)
        .map(([k, n]) => `${n} ${k}`)
        .join(', ')
      return `${when} UTC: ${counts || 'no TLEs in it'}`
    },
    statusClass(status) {
      if (!status) return 'text-medium-emphasis'
      return status.error ? 'text-error' : ''
    },
  },
}
</script>

<style>
.fa-snippet {
  font-family: monospace;
  font-size: 11px;
  padding: 6px 8px;
  margin: 4px 0;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.3);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
