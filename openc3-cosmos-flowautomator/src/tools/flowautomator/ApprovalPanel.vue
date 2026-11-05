<!--
# Operator approvals for runs in crawl or walk mode: what the step is about
# to do and the Go / Go and run the rest / Abort choices.
-->

<template>
  <div
    v-if="approvals.length"
    class="fa-approvals"
    :class="{ 'fa-approvals-min': minimized }"
  >
    <!-- Minimized: a small pill, so the cards stop covering the flow. A new approval reopens it -->
    <v-btn
      v-if="minimized"
      size="small"
      color="warning"
      variant="flat"
      prepend-icon="mdi-hand-back-right"
      append-icon="mdi-chevron-down"
      class="fa-approvals-restore"
      title="Show the approval cards"
      @click="minimized = false"
    >
      {{ approvals.length }} awaiting approval
    </v-btn>
    <div v-else class="fa-approvals-head">
      <v-icon size="16" color="warning" class="mr-1">
        mdi-hand-back-right
      </v-icon>
      {{ approvals.length }} awaiting approval
      <v-spacer />
      <v-btn
        size="x-small"
        variant="text"
        prepend-icon="mdi-chevron-up"
        class="fa-approvals-minimize"
        title="Shrink to a small button so you can see what's underneath"
        @click="minimize"
      >
        Minimize
      </v-btn>
    </div>
    <v-card
      v-for="a in approvals"
      v-show="!minimized"
      :key="a.id"
      class="fa-approval mb-2"
      :class="{ 'fa-approval-hazardous': hazardous(a) }"
      elevation="8"
    >
      <div class="d-flex align-center px-3 pt-2">
        <v-icon class="mr-2" :color="hazardous(a) ? 'error' : 'warning'">
          {{ hazardous(a) ? 'mdi-alert' : 'mdi-hand-back-right' }}
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
      <div
        v-if="stale"
        class="px-3 text-caption text-warning fa-approval-stale"
      >
        The engine isn't answering: this may be out of date, and can't be
        answered until it's back.
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
            class="mr-2 fa-ask-input"
            :data-approval="a.id"
            @keydown.enter="answer(a, answers[a.id])"
          />
          <template v-if="a.ask.kind === 'confirm'">
            <v-btn
              size="small"
              color="success"
              variant="flat"
              class="mr-2 fa-ask-yes"
              :loading="busy === a.id"
              :disabled="locked(a)"
              @click="answer(a, true)"
            >
              Yes
            </v-btn>
            <v-btn
              size="small"
              variant="tonal"
              class="mr-2 fa-ask-no"
              :disabled="busy === a.id || locked(a)"
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
            :disabled="locked(a)"
            @click="answer(a, answers[a.id])"
          >
            Answer
          </v-btn>
          <v-spacer />
          <v-btn
            size="small"
            variant="flat"
            prepend-icon="mdi-stop"
            class="fa-btn-caution"
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
          variant="flat"
          :prepend-icon="hazardous(a) ? 'mdi-alert' : 'mdi-play'"
          class="mr-2 fa-approval-go"
          :class="hazardous(a) ? 'fa-btn-danger' : 'fa-btn-go'"
          :loading="busy === a.id"
          :disabled="locked(a)"
          @click="go(a, 'go')"
        >
          {{ hazardous(a) ? 'Send hazardous…' : 'Go' }}
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          prepend-icon="mdi-run-fast"
          class="mr-2"
          title="Approve this step and stop asking for routine steps in this run. Hazardous commands still ask."
          :disabled="busy === a.id || locked(a)"
          @click="go(a, 'run')"
        >
          Go &amp; run rest
        </v-btn>
        <v-spacer />
        <v-btn
          size="small"
          variant="flat"
          prepend-icon="mdi-stop"
          class="fa-btn-caution"
          :disabled="busy === a.id"
          @click="decide(a, 'abort')"
        >
          Abort run
        </v-btn>
      </div>
    </v-card>

    <!-- Approving a hazardous command sends it: ask once more, showing exactly what goes out -->
    <v-dialog v-model="confirm.show" max-width="520">
      <v-card v-if="confirm.approval">
        <v-card-title class="text-error d-flex align-center">
          <v-icon class="mr-2">mdi-alert</v-icon>Send a hazardous command?
        </v-card-title>
        <v-card-text>
          <div class="mb-2">
            <b>{{ confirm.approval.flow_name }}</b> ·
            {{ confirm.approval.label }} · run {{ confirm.approval.run_id }}
          </div>
          <pre class="fa-approval-preview">{{ confirm.approval.preview }}</pre>
          <div class="text-caption mt-2">
            This is exactly what will be sent. If a value changes before it goes
            out, the step fails instead of sending something else.
            <template v-if="confirm.decision === 'run'">
              Routine steps after it stop asking; later hazardous commands still
              ask.
            </template>
          </div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" autofocus @click="confirm.show = false">
            Cancel
          </v-btn>
          <v-btn
            variant="flat"
            prepend-icon="mdi-alert"
            class="fa-btn-danger fa-confirm-hazardous"
            @click="confirmHazardous"
          >
            Send hazardous command
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script>
import flowApi, { errorInfo } from './flowApi'

const FRESH_MS = 750

export default {
  props: {
    approvals: { type: Array, default: () => [] },
    showFlow: { type: Boolean, default: false },
    now: { type: Number, default: () => Date.now() / 1000 },
    // The engine isn't answering: these cards may be out of date, so nothing can be decided on them
    stale: { type: Boolean, default: false },
  },
  emits: ['decided'],
  data() {
    return {
      busy: null,
      answers: {},
      minimized: false,
      minimizedIds: [],
      // Cards that just appeared ignore clicks for a moment, so a fast second click meant for the
      // previous card can't approve the next step that took its place
      fresh: [],
      shown: [], // card ids in display order, to notice cards sliding up
      seen: [],
      confirm: { show: false, approval: null, decision: null },
    }
  },
  watch: {
    approvals: {
      immediate: true,
      handler(list) {
        if (
          this.minimized &&
          list.some((a) => !this.minimizedIds.includes(a.id))
        ) {
          this.minimized = false
        }
        const added = list.filter((a) => !this.seen.includes(a.id))
        const current = list.map((a) => a.id)
        // A card that was answered disappears and the ones below slide up under the cursor: those are
        // "new under the cursor" too, so a second click meant for the gone card can't hit them
        const moved =
          this.shown.length > 0 &&
          this.shown.some(
            (id, i) => current.indexOf(id) !== -1 && current.indexOf(id) !== i,
          )
        this.shown = current
        if (!added.length && !moved) return
        this.seen = [...this.seen, ...added.map((a) => a.id)].slice(-200)
        const ids = moved ? current : added.map((a) => a.id)
        this.fresh = [...new Set([...this.fresh, ...ids])]
        setTimeout(() => {
          this.fresh = this.fresh.filter((id) => !ids.includes(id))
        }, FRESH_MS)
        if (added.some((a) => a.ask && a.ask.kind !== 'confirm')) {
          this.$nextTick(() => this.focusAnswer(added.find((a) => a.ask)))
        }
      },
    },
  },
  methods: {
    hazardous(approval) {
      // The engine flags it; older engines only said so in the reason text
      return approval.hazardous ?? approval.reason.includes('HAZARDOUS')
    },
    locked(approval) {
      return this.stale || this.fresh.includes(approval.id)
    },
    go(approval, decision) {
      if (this.locked(approval)) return
      if (this.hazardous(approval)) {
        this.confirm = { show: true, approval, decision }
        return
      }
      this.decide(approval, decision)
    },
    confirmHazardous() {
      const { approval, decision } = this.confirm
      this.confirm = { show: false, approval: null, decision: null }
      if (approval && this.approvals.some((a) => a.id === approval.id)) {
        this.decide(approval, decision)
      }
    },
    minimize() {
      this.minimizedIds = this.approvals.map((a) => a.id)
      this.minimized = true
    },
    focusAnswer(approval) {
      // Only when nobody is typing elsewhere: a card appearing mustn't take the keyboard (and an Enter that
      // was meant for another field mustn't answer it)
      const active = document.activeElement
      if (active && active !== document.body) return
      const input = this.$el?.querySelector?.(
        `.fa-ask-input[data-approval="${approval.id}"] input`,
      )
      if (input) input.focus()
    },
    answer(approval, value) {
      if (this.locked(approval)) return
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
.fa-approvals.fa-approvals-min {
  width: auto;
  overflow: visible;
}
.fa-approvals-head {
  display: flex;
  align-items: center;
  margin-bottom: 4px;
  padding: 2px 4px 2px 10px;
  border-radius: 4px;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: rgba(31, 47, 64, 0.95);
  border: 1px solid #44515e;
}
.fa-approval {
  border-left: 4px solid rgb(var(--v-theme-warning));
}
.fa-approval-hazardous {
  border-left-color: rgb(var(--v-theme-error));
}
/* COSMOS's global CSS paints every filled button the primary blue (!important), which erased the
   meaning of Go / Abort / Stop all. These win on specificity: use them for buttons whose colour matters. */
.v-application .v-btn.fa-btn-danger.fa-btn-danger,
.v-application .v-btn.fa-btn-danger.fa-btn-danger:hover {
  background-color: rgb(var(--v-theme-error)) !important;
  color: #fff !important;
}
.v-application .v-btn.fa-btn-caution.fa-btn-caution,
.v-application .v-btn.fa-btn-caution.fa-btn-caution:hover {
  background-color: rgb(var(--v-theme-warning)) !important;
  color: #000 !important;
}
.v-application .v-btn.fa-btn-go.fa-btn-go,
.v-application .v-btn.fa-btn-go.fa-btn-go:hover {
  background-color: rgb(var(--v-theme-success)) !important;
  color: #000 !important;
}
.v-application .v-btn.fa-btn-danger.v-btn--disabled,
.v-application .v-btn.fa-btn-go.v-btn--disabled {
  opacity: 0.5;
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
