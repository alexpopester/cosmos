<!--
# Operator approvals for runs in crawl or walk mode: what the step is about
# to do and the Go / Go and run the rest / Abort choices.
-->

<template>
  <div v-if="approvals.length" class="fa-approvals">
    <v-card
      v-for="a in approvals"
      :key="a.id"
      class="fa-approval mb-2"
      :class="{ 'fa-approval-hazardous': a.reason.includes('HAZARDOUS') }"
      elevation="8"
    >
      <div class="d-flex align-center px-3 pt-2">
        <v-icon
          class="mr-2"
          :color="a.reason.includes('HAZARDOUS') ? 'error' : 'warning'"
        >
          {{
            a.reason.includes('HAZARDOUS') ? 'mdi-alert' : 'mdi-hand-back-right'
          }}
        </v-icon>
        <span class="text-subtitle-2">
          {{ a.ask ? 'Question' : 'Waiting for approval' }}:
          <b>{{ a.label }}</b>
        </span>
        <v-spacer />
        <span class="text-caption text-medium-emphasis">
          {{ waited(a) }}
        </span>
      </div>
      <div class="px-3 text-caption text-medium-emphasis">
        <span v-if="showFlow">{{ a.flow_name }} · </span>run {{ a.run_id }} ·
        {{ a.reason }}
      </div>
      <pre v-if="!a.ask" class="fa-approval-preview mx-3 my-2">{{
        a.preview
      }}</pre>
      <div v-else class="px-3 pt-2 fa-ask">
        <div class="text-body-1 mb-2">{{ a.ask.prompt || a.preview }}</div>
        <div class="d-flex align-center pb-2">
          <v-select
            v-if="a.ask.kind === 'choice'"
            v-model="answers[a.id]"
            :items="a.ask.choices"
            label="Answer"
            density="compact"
            variant="outlined"
            hide-details
            class="mr-2"
          />
          <v-text-field
            v-else-if="a.ask.kind !== 'confirm'"
            v-model="answers[a.id]"
            :type="a.ask.kind === 'number' ? 'number' : 'text'"
            :placeholder="a.ask.default != null ? String(a.ask.default) : ''"
            label="Answer"
            density="compact"
            variant="outlined"
            hide-details
            autofocus
            class="mr-2 fa-ask-input"
            @keyup.enter="answer(a, answers[a.id])"
          />
          <template v-if="a.ask.kind === 'confirm'">
            <v-btn
              size="small"
              color="success"
              variant="flat"
              class="mr-2"
              :loading="busy === a.id"
              @click="answer(a, true)"
            >
              Yes
            </v-btn>
            <v-btn
              size="small"
              variant="tonal"
              class="mr-2"
              :disabled="busy === a.id"
              @click="answer(a, false)"
            >
              No
            </v-btn>
          </template>
          <v-btn
            v-else
            size="small"
            color="primary"
            variant="flat"
            class="mr-2 fa-ask-submit"
            :loading="busy === a.id"
            @click="answer(a, answers[a.id])"
          >
            Answer
          </v-btn>
          <v-spacer />
          <v-btn
            size="small"
            color="warning"
            variant="tonal"
            prepend-icon="mdi-stop"
            :disabled="busy === a.id"
            @click="decide(a, 'abort')"
          >
            Abort run
          </v-btn>
        </div>
      </div>
      <div v-if="!a.ask" class="d-flex px-3 pb-2">
        <v-btn
          size="small"
          :color="a.reason.includes('HAZARDOUS') ? 'error' : 'success'"
          variant="flat"
          prepend-icon="mdi-play"
          class="mr-2"
          :loading="busy === a.id"
          @click="decide(a, 'go')"
        >
          Go
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          prepend-icon="mdi-run-fast"
          class="mr-2"
          title="Approve this step and stop asking for routine steps in this run. Hazardous commands still ask."
          :disabled="busy === a.id"
          @click="decide(a, 'run')"
        >
          Go &amp; run rest
        </v-btn>
        <v-spacer />
        <v-btn
          size="small"
          color="warning"
          variant="tonal"
          prepend-icon="mdi-stop"
          :disabled="busy === a.id"
          @click="decide(a, 'abort')"
        >
          Abort run
        </v-btn>
      </div>
    </v-card>
  </div>
</template>

<script>
import flowApi, { errorInfo } from './flowApi'

export default {
  props: {
    approvals: { type: Array, default: () => [] },
    showFlow: { type: Boolean, default: false },
    now: { type: Number, default: () => Date.now() / 1000 },
  },
  emits: ['decided'],
  data() {
    return { busy: null, answers: {} }
  },
  methods: {
    answer(approval, value) {
      const empty = value === undefined || value === null || value === ''
      const fallback = approval.ask.default
      return this.decide(
        approval,
        'go',
        empty && fallback != null ? fallback : value,
      )
    },
    async decide(approval, decision, value) {
      this.busy = approval.id
      try {
        await flowApi.approve(approval.run_id, approval.id, decision, value)
        this.$emit('decided', approval, decision)
      } catch (error) {
        this.$notify.caution({
          title: 'Approval not sent',
          body: errorInfo(error).message,
        })
      } finally {
        this.busy = null
      }
    },
    waited(approval) {
      const seconds = Math.max(0, this.now - approval.requested_at)
      return seconds < 60
        ? `${Math.floor(seconds)}s`
        : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`
    },
  },
}
</script>

<style>
.fa-approvals {
  position: absolute;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  width: min(560px, 90%);
  z-index: 10;
  max-height: 70%;
  overflow-y: auto;
}
.fa-approval {
  border-left: 4px solid rgb(var(--v-theme-warning));
}
.fa-approval-hazardous {
  border-left-color: rgb(var(--v-theme-error));
}
.fa-approval-preview {
  font-family: monospace;
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.3);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
