<!--
# Upload TLEs for any number of satellites (paste or choose a file). They go
# through the same checks as automated sources (tle_inputs.py): matched by
# catalog number then name, only newer epochs, big jumps held for review.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="820"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-tle-upload">
      <v-card-title>Upload TLEs</v-card-title>
      <v-card-text>
        <div class="text-caption text-medium-emphasis mb-2">
          Any number of TLEs, 2 or 3 lines each. Each is matched to a satellite
          by catalog number, then by name. Only TLEs newer than a satellite's
          current one apply, and one that moves the satellite more than the jump
          limit is held for review.
        </div>
        <div class="d-flex align-center mb-2">
          <v-file-input
            label="TLE file"
            accept=".tle,.txt,.3le,text/plain"
            density="compact"
            variant="outlined"
            hide-details
            prepend-icon="mdi-file-upload"
            class="mr-2"
            @update:model-value="readFile"
          />
          <v-text-field
            v-model="source"
            label="Source"
            hint="Recorded with each TLE"
            persistent-hint
            density="compact"
            variant="outlined"
            class="fa-upload-source"
          />
        </div>
        <v-textarea
          v-model="text"
          label="TLEs"
          rows="8"
          density="compact"
          variant="outlined"
          class="fa-mono"
          data-field="upload-tles"
        />
        <v-table v-if="outcome" density="compact" class="fa-upload-results">
          <thead>
            <tr>
              <th>TLE</th>
              <th>Satellite</th>
              <th>Result</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in outcome.results" :key="i">
              <td class="text-no-wrap">{{ r.name || r.catalog || '?' }}</td>
              <td>{{ r.satellite || '—' }}</td>
              <td>
                <v-chip
                  size="x-small"
                  label
                  variant="flat"
                  :color="resultColor(r.result)"
                >
                  {{ r.result }}
                </v-chip>
              </td>
              <td class="text-caption">{{ r.reason }}</td>
            </tr>
          </tbody>
        </v-table>
        <div
          v-for="(p, i) in outcome?.problems || []"
          :key="`p${i}`"
          class="text-caption text-warning"
        >
          {{ p }}
        </div>
        <v-alert v-if="error" type="error" density="compact" class="mt-2">
          {{ error }}
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="$emit('update:modelValue', false)">
          Close
        </v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :loading="busy"
          :disabled="!text.trim()"
          class="fa-upload-go"
          @click="upload"
        >
          Apply
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
  emits: ['update:modelValue', 'applied'],
  data() {
    return { text: '', source: 'upload', outcome: null, error: '', busy: false }
  },
  watch: {
    modelValue(open) {
      if (open) {
        this.outcome = null
        this.error = ''
      }
    },
  },
  methods: {
    async readFile(files) {
      const file = Array.isArray(files) ? files[0] : files
      if (!file) return
      this.text = await file.text()
      this.source = `upload:${file.name}`
    },
    async upload() {
      this.busy = true
      this.error = ''
      try {
        this.outcome = await flowApi.ingestTles(this.text, this.source)
        this.$emit('applied', this.outcome)
      } catch (error) {
        this.error = errorInfo(error).message
      } finally {
        this.busy = false
      }
    },
    resultColor(result) {
      return (
        {
          applied: 'success',
          unchanged: undefined,
          skipped: 'info',
          held: 'warning',
          rejected: 'error',
          unknown: 'error',
        }[result] || undefined
      )
    },
  },
}
</script>

<style>
.fa-upload-source {
  max-width: 240px;
}
</style>
