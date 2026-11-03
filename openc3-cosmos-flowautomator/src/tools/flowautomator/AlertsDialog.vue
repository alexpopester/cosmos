<!--
# Where alerts go (alerts.py): the COSMOS notification bell and webhooks
# (Slack, Teams, or generic JSON). Webhook URLs are secrets, so the engine
# only shows their host; leave the URL empty to keep the saved one.
-->

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="900"
    scrollable
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <v-card class="fa-alerts">
      <v-card-title class="d-flex align-center">
        <v-icon class="mr-2">mdi-bell-ring</v-icon>
        Alerts
      </v-card-title>
      <v-card-text v-if="settings">
        <div class="text-subtitle-2 mb-1">COSMOS notifications</div>
        <div class="text-caption text-medium-emphasis mb-2">
          The bell in every COSMOS tool. Only events someone has to act on pop
          up as alerts (COSMOS shows them again on every page until
          acknowledged): approvals nobody has answered, unattended runs that
          failed, missed activities, failed predictions. Runs you start
          yourself, Stop all and routine events only go to the bell. Dry runs
          never ring it.
        </div>
        <div class="d-flex align-center">
          <v-switch
            v-model="settings.cosmos.enabled"
            label="Send to COSMOS notifications"
            color="primary"
            density="compact"
            hide-details
            class="flex-grow-0 mr-4 fa-alerts-cosmos"
          />
          <v-btn
            size="small"
            variant="tonal"
            :loading="testing === 'cosmos'"
            @click="test(null)"
          >
            Send test
          </v-btn>
        </div>
        <v-text-field
          v-model.number="settings.approval_escalate_after"
          type="number"
          min="0"
          label="Alert about an approval after (s)"
          hint="Someone watching the tool answers sooner; 0 alerts at once"
          persistent-hint
          density="compact"
          variant="outlined"
          class="mt-3 fa-approval-delay"
        />
        <v-select
          v-model="settings.cosmos.events"
          :items="eventItems"
          label="Events"
          multiple
          chips
          closable-chips
          density="compact"
          variant="outlined"
          class="mt-2"
        />

        <div class="d-flex align-center mt-2">
          <div class="text-subtitle-2">Webhooks</div>
          <v-spacer />
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-plus"
            class="fa-add-webhook"
            @click="addHook"
          >
            Add webhook
          </v-btn>
        </div>
        <div class="text-caption text-medium-emphasis mb-2">
          An HTTP POST for each event: Slack and Teams incoming webhooks, or
          generic JSON for anything else (a pager relay, a ticket system).
        </div>
        <v-card
          v-for="(hook, i) in settings.webhooks"
          :key="hook.id || i"
          variant="outlined"
          class="pa-3 mb-3 fa-webhook"
        >
          <div class="d-flex align-center">
            <v-text-field
              v-model="hook.name"
              label="Name"
              density="compact"
              variant="outlined"
              hide-details
              class="mr-2"
            />
            <v-select
              v-model="hook.format"
              :items="formats"
              label="Format"
              density="compact"
              variant="outlined"
              hide-details
              class="mr-2 fa-hook-format"
            />
            <v-switch
              v-model="hook.enabled"
              color="primary"
              density="compact"
              hide-details
              class="flex-grow-0"
            />
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              @click="settings.webhooks.splice(i, 1)"
            />
          </div>
          <v-text-field
            v-model="hook.url"
            :placeholder="
              hook.url_masked
                ? `${hook.url_masked} (saved; type to replace)`
                : 'https://hooks.slack.com/services/…'
            "
            persistent-placeholder
            label="URL"
            density="compact"
            variant="outlined"
            hide-details
            class="mt-2 fa-mono"
            data-field="hook-url"
          />
          <v-select
            v-model="hook.events"
            :items="eventItems"
            label="Events"
            multiple
            chips
            closable-chips
            density="compact"
            variant="outlined"
            hide-details
            class="mt-2"
          />
          <div class="d-flex align-center mt-1">
            <v-checkbox
              v-model="hook.include_dry_runs"
              label="Include dry runs"
              density="compact"
              hide-details
            />
            <v-spacer />
            <span class="text-caption text-medium-emphasis mr-2">
              <template v-if="hook.status && hook.status.sent">
                {{ hook.status.sent }} sent
              </template>
              <span
                v-if="hook.status && hook.status.last_error"
                class="text-error"
              >
                · last error: {{ hook.status.last_error }}
              </span>
            </span>
            <v-btn
              size="small"
              variant="tonal"
              :disabled="!hook.id"
              :title="hook.id ? '' : 'Save first'"
              :loading="testing === hook.id"
              @click="test(hook.id)"
            >
              Test
            </v-btn>
          </div>
        </v-card>

        <v-text-field
          v-model="settings.base_url"
          label="COSMOS address for links in webhooks"
          placeholder="https://cosmos.example.com"
          hint="So links in Slack or Teams open the Flow Automator"
          persistent-hint
          density="compact"
          variant="outlined"
          class="mt-2"
        />

        <div v-if="settings.recent.length" class="mt-4">
          <div class="text-subtitle-2 mb-1">Recent events</div>
          <div v-for="(e, i) in settings.recent" :key="i" class="text-caption">
            {{ fmtTime(e.time) }} ·
            <span :class="e.level === 'alert' ? 'text-error' : ''">
              {{ e.event }}
            </span>
            · {{ e.title }}
          </div>
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
          :loading="saving"
          class="fa-alerts-save"
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
    return {
      settings: null,
      saving: false,
      testing: null,
      error: '',
      formats: [
        { title: 'Slack', value: 'slack' },
        { title: 'Teams', value: 'teams' },
        { title: 'Generic JSON', value: 'generic' },
      ],
    }
  },
  computed: {
    eventItems() {
      return Object.entries(this.settings?.events || {}).map(([key, e]) => ({
        title: e.description,
        value: key,
      }))
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
        this.settings = await flowApi.alerts()
        for (const hook of this.settings.webhooks) hook.url = ''
      } catch (error) {
        this.error = errorInfo(error).message
      }
    },
    addHook() {
      this.settings.webhooks.push({
        name: 'Ops channel',
        url: '',
        format: 'slack',
        enabled: true,
        events: [...this.settings.cosmos.events],
        include_dry_runs: false,
      })
    },
    body() {
      return {
        cosmos: this.settings.cosmos,
        base_url: this.settings.base_url,
        approval_escalate_after: Number(this.settings.approval_escalate_after),
        webhooks: this.settings.webhooks.map((h) => ({
          id: h.id,
          name: h.name,
          url: h.url,
          format: h.format,
          enabled: h.enabled,
          events: h.events,
          include_dry_runs: h.include_dry_runs,
        })),
      }
    },
    async save() {
      this.saving = true
      this.error = ''
      try {
        await flowApi.saveAlerts(this.body())
        this.$emit('update:modelValue', false)
      } catch (error) {
        this.error = errorInfo(error).message
      } finally {
        this.saving = false
      }
    },
    async test(hookId) {
      this.testing = hookId || 'cosmos'
      try {
        const result = await flowApi.testAlert(hookId)
        if (result.ok) {
          this.$notify.normal({ title: 'Test alert sent' })
        } else {
          this.$notify.caution({ title: 'Test failed', body: result.error })
        }
        const fresh = await flowApi.alerts()
        const byId = Object.fromEntries(fresh.webhooks.map((h) => [h.id, h]))
        for (const hook of this.settings.webhooks) {
          if (byId[hook.id]) hook.status = byId[hook.id].status
        }
      } catch (error) {
        this.$notify.caution({
          title: 'Test failed',
          body: errorInfo(error).message,
        })
      } finally {
        this.testing = null
      }
    },
    fmtTime(t) {
      return new Date(t * 1000).toISOString().slice(11, 19)
    },
  },
}
</script>

<style>
.fa-hook-format {
  max-width: 160px;
}
</style>
