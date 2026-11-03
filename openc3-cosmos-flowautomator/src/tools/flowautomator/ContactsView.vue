<!--
# Contacts: satellites (TLEs), ground stations, the passes the engine predicts
# for them, and activities (flow templates scheduled relative to a pass).
# All the planning runs in the FLOW_ENGINE (contacts.py); this view polls
# /schedule and edits through the API.
-->

<template>
  <div class="fa-contacts">
    <div class="fa-contacts-left">
      <div class="d-flex align-center px-3 pt-3 pb-1">
        <span class="text-subtitle-2">Satellites</span>
        <v-spacer />
        <v-btn
          size="x-small"
          variant="tonal"
          prepend-icon="mdi-plus"
          class="fa-add-satellite"
          @click="editSatellite(null)"
        >
          Add
        </v-btn>
      </div>
      <v-list density="compact" class="py-0">
        <v-list-item
          v-for="(sat, i) in satellites"
          :key="sat.id"
          class="fa-satellite"
          @click="editSatellite(sat)"
        >
          <template #prepend>
            <v-icon :color="satColor(i)" size="18" class="mr-2">
              mdi-satellite-variant
            </v-icon>
          </template>
          <v-list-item-title>
            {{ sat.name }}
            <span v-if="sat.target" class="text-caption text-medium-emphasis">
              · {{ sat.target }}
            </span>
          </v-list-item-title>
          <v-list-item-subtitle>
            <span v-if="sat.problem" class="text-error">{{ sat.problem }}</span>
            <span v-else-if="sat.tle">
              TLE {{ sat.tle.norad_id }} ·
              <span :class="sat.tle_stale ? 'text-warning' : ''">
                {{ ageText(sat.tle_age_days) }} old
              </span>
            </span>
          </v-list-item-subtitle>
          <template #append>
            <v-icon v-if="sat.tle_stale" color="warning" size="16">
              mdi-clock-alert-outline
            </v-icon>
            <v-icon v-if="!sat.enabled" size="16">mdi-eye-off</v-icon>
          </template>
        </v-list-item>
        <div
          v-if="!satellites.length"
          class="text-caption text-medium-emphasis px-4 py-2"
        >
          Add a satellite and paste its TLE.
        </div>
      </v-list>

      <div class="d-flex align-center px-3 pt-4 pb-1">
        <span class="text-subtitle-2">Ground stations</span>
        <v-spacer />
        <v-btn
          size="x-small"
          variant="tonal"
          prepend-icon="mdi-plus"
          class="fa-add-station"
          @click="editStation(null)"
        >
          Add
        </v-btn>
      </div>
      <v-list density="compact" class="py-0">
        <v-list-item
          v-for="station in stations"
          :key="station.id"
          class="fa-station"
          @click="editStation(station)"
        >
          <template #prepend>
            <v-icon size="18" class="mr-2">mdi-radio-tower</v-icon>
          </template>
          <v-list-item-title>{{ station.name }}</v-list-item-title>
          <v-list-item-subtitle>
            {{ station.latitude.toFixed(3) }},
            {{ station.longitude.toFixed(3) }} · mask
            {{ station.min_elevation }}°
          </v-list-item-subtitle>
          <template #append>
            <v-icon v-if="!station.enabled" size="16">mdi-eye-off</v-icon>
          </template>
        </v-list-item>
        <div
          v-if="!stations.length"
          class="text-caption text-medium-emphasis px-4 py-2"
        >
          Add a ground station.
        </div>
      </v-list>

      <div class="text-caption text-medium-emphasis px-3 pt-4">
        <div v-if="status.computed_at">
          Passes predicted {{ relText(status.computed_at) }} for the next
          {{ horizonDays }} days ({{ status.contacts }} contacts,
          {{ (status.compute_seconds || 0).toFixed(2) }}s).
        </div>
        <v-btn
          size="x-small"
          variant="text"
          prepend-icon="mdi-refresh"
          class="mt-1 px-0"
          @click="recompute"
        >
          Recompute now
        </v-btn>
      </div>
    </div>

    <div class="fa-contacts-center">
      <v-alert
        v-if="status.sgp4_error"
        type="error"
        density="compact"
        class="ma-2"
      >
        Pass prediction is unavailable: {{ status.sgp4_error }}. The plugin
        installs sgp4 from requirements.txt; check the plugin install log.
      </v-alert>
      <v-alert
        v-if="status.paused"
        type="warning"
        density="compact"
        class="ma-2 fa-paused"
      >
        <div class="d-flex align-center">
          <span>
            The scheduler is paused ({{ status.paused_reason }}). Scheduled
            activities won't start; any that pass their late tolerance are
            marked missed.
          </span>
          <v-spacer />
          <v-btn
            size="small"
            variant="flat"
            color="warning"
            class="ml-2 fa-resume"
            @click="resume"
          >
            Resume
          </v-btn>
        </div>
      </v-alert>

      <div class="d-flex align-center px-3 py-2">
        <span class="text-subtitle-2 mr-3">Timeline (UTC)</span>
        <v-btn-toggle
          v-model="rangeHours"
          mandatory
          density="compact"
          variant="outlined"
          divided
        >
          <v-btn
            v-for="r in ranges"
            :key="r.hours"
            :value="r.hours"
            size="x-small"
          >
            {{ r.label }}
          </v-btn>
        </v-btn-toggle>
        <v-spacer />
        <span v-if="status.next_contact" class="text-body-2 mr-3">
          Next contact:
          <b>{{ status.next_contact.satellite }}</b> over
          {{ status.next_contact.station }}
          <span class="text-primary">{{ countdown(status.next_contact) }}</span>
        </span>
        <v-btn
          size="small"
          variant="tonal"
          :prepend-icon="status.paused ? 'mdi-play' : 'mdi-pause'"
          :color="status.paused ? 'warning' : undefined"
          @click="status.paused ? resume() : pause()"
        >
          {{ status.paused ? 'Resume scheduler' : 'Pause scheduler' }}
        </v-btn>
      </div>

      <div class="fa-timeline mx-3" @click.self="selectedContactId = null">
        <div class="fa-tl-axis">
          <div
            v-for="tick in ticks"
            :key="tick.t"
            class="fa-tl-tick"
            :style="{ left: pct(tick.t) }"
          >
            {{ tick.label }}
          </div>
        </div>
        <div
          v-for="station in timelineStations"
          :key="station.id"
          class="fa-tl-row"
        >
          <div class="fa-tl-label" :title="station.name">
            {{ station.name }}
          </div>
          <div class="fa-tl-lane">
            <div
              v-for="c in contactsFor(station.id)"
              :key="c.id"
              class="fa-tl-pass"
              :class="{ 'fa-tl-selected': c.id === selectedContactId }"
              :style="passStyle(c)"
              :title="passTitle(c)"
              :data-contact="c.id"
              @click="selectedContactId = c.id"
            >
              <span class="fa-tl-pass-label">{{ c.satellite }}</span>
            </div>
            <div
              v-for="a in activitiesFor(station.id)"
              :key="a.id"
              class="fa-tl-activity"
              :class="`fa-act-${a.status}`"
              :style="{ left: pct(a.start) }"
              :title="`${a.label}: ${a.status} at ${fmtTime(a.start)}`"
              @click="selectedContactId = a.contact.id"
            />
          </div>
        </div>
        <div v-if="!timelineStations.length" class="text-caption pa-4">
          No ground stations yet.
        </div>
        <div
          class="fa-tl-now"
          :style="{ left: `calc(120px + (100% - 120px) * ${frac(now)})` }"
        />
      </div>

      <div class="fa-contacts-tables">
        <div class="d-flex align-center px-3 pt-3">
          <span class="text-subtitle-2">Activities</span>
          <v-chip
            v-if="status.needs_review"
            size="x-small"
            color="warning"
            class="ml-2"
            label
          >
            {{ status.needs_review }} need review
          </v-chip>
          <v-spacer />
          <v-checkbox
            v-model="showFinished"
            label="Show finished"
            density="compact"
            hide-details
            class="flex-grow-0"
          />
        </div>
        <v-table density="compact" class="fa-activities">
          <thead>
            <tr>
              <th>Activity</th>
              <th>Contact</th>
              <th>Start (UTC)</th>
              <th>Mode</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="a in visibleActivities"
              :key="a.id"
              :class="{ 'fa-row-selected': a.contact.id === selectedContactId }"
              :data-activity="a.id"
              @click="selectedContactId = a.contact.id"
            >
              <td>
                <div>{{ a.label }}</div>
                <div class="text-caption text-medium-emphasis">
                  {{ a.template_name }}
                  <v-chip
                    v-if="a.template_changed"
                    size="x-small"
                    color="info"
                    label
                    class="ml-1"
                    title="The template was edited after this was scheduled. Update copies the current version."
                  >
                    template changed
                  </v-chip>
                  <v-chip
                    v-if="a.template_missing"
                    size="x-small"
                    color="error"
                    label
                    class="ml-1"
                  >
                    template deleted
                  </v-chip>
                  <v-chip
                    v-if="a.overlaps.length"
                    size="x-small"
                    color="warning"
                    label
                    class="ml-1 fa-overlap"
                    :title="`Overlaps ${a.overlaps.length} other activity in this contact`"
                  >
                    overlaps
                  </v-chip>
                </div>
              </td>
              <td>
                {{ a.contact.satellite }} / {{ a.contact.station }}
                <div class="text-caption text-medium-emphasis">
                  AOS {{ fmtTime(a.contact.aos) }}
                </div>
              </td>
              <td>
                {{ fmtTime(a.start) }}
                <div class="text-caption text-medium-emphasis">
                  {{ anchorText(a) }} · {{ relText(a.start) }}
                </div>
              </td>
              <td>{{ a.mode }}</td>
              <td>
                <v-chip
                  size="x-small"
                  :color="statusColor(a.status)"
                  variant="flat"
                  label
                  class="fa-activity-status"
                >
                  {{ a.status.replace('_', ' ') }}
                </v-chip>
                <div
                  class="text-caption text-medium-emphasis fa-activity-message"
                >
                  {{ a.message }}
                </div>
              </td>
              <td class="text-no-wrap">
                <v-btn
                  v-if="a.status === 'needs_review' && a.suggested_contact_id"
                  size="x-small"
                  variant="tonal"
                  color="warning"
                  class="mr-1"
                  @click.stop="accept(a)"
                >
                  Accept new time
                </v-btn>
                <v-btn
                  v-if="a.template_changed && editable(a)"
                  size="x-small"
                  variant="tonal"
                  class="mr-1"
                  @click.stop="refreshTemplate(a)"
                >
                  Update
                </v-btn>
                <v-btn
                  v-if="a.run_id"
                  icon="mdi-open-in-app"
                  size="x-small"
                  variant="text"
                  title="Open the run"
                  @click.stop="$emit('open-run', a.template_id, a.run_id)"
                />
                <v-btn
                  v-if="editable(a)"
                  icon="mdi-pencil"
                  size="x-small"
                  variant="text"
                  title="Edit"
                  @click.stop="editActivity(a)"
                />
                <v-btn
                  icon="mdi-delete"
                  size="x-small"
                  variant="text"
                  :title="a.status === 'running' ? 'Stop and delete' : 'Delete'"
                  @click.stop="removeActivity(a)"
                />
              </td>
            </tr>
            <tr v-if="!visibleActivities.length">
              <td colspan="6" class="text-caption text-medium-emphasis">
                Nothing scheduled. Pick a contact on the timeline or in the list
                and schedule a template into it.
              </td>
            </tr>
          </tbody>
        </v-table>

        <div class="d-flex align-center px-3 pt-4">
          <span class="text-subtitle-2">Contacts in view</span>
        </div>
        <v-table density="compact" class="fa-contact-list">
          <thead>
            <tr>
              <th>Satellite</th>
              <th>Station</th>
              <th>AOS (UTC)</th>
              <th>LOS</th>
              <th>Duration</th>
              <th>Max el</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="c in listedContacts"
              :key="c.id"
              :class="{
                'fa-row-selected': c.id === selectedContactId,
                'text-medium-emphasis': c.los < now,
              }"
              :data-contact="c.id"
              @click="selectedContactId = c.id"
            >
              <td>{{ c.satellite }}</td>
              <td>{{ c.station }}</td>
              <td>
                {{ fmtTime(c.aos) }}
                <div class="text-caption text-medium-emphasis">
                  {{ relText(c.aos) }}
                </div>
              </td>
              <td>{{ fmtTime(c.los).slice(11) }}</td>
              <td>{{ duration(c.duration) }}</td>
              <td>{{ c.max_elevation.toFixed(1) }}°</td>
              <td class="text-no-wrap">
                <v-chip
                  v-if="c.activities.length"
                  size="x-small"
                  label
                  class="mr-1"
                >
                  {{ c.activities.length }}
                </v-chip>
                <v-btn
                  size="x-small"
                  variant="tonal"
                  color="primary"
                  prepend-icon="mdi-calendar-plus"
                  :disabled="c.los < now"
                  class="fa-schedule-btn"
                  @click.stop="newActivity(c)"
                >
                  Schedule
                </v-btn>
              </td>
            </tr>
            <tr v-if="!listedContacts.length">
              <td colspan="7" class="text-caption text-medium-emphasis">
                No contacts in this window.
              </td>
            </tr>
          </tbody>
        </v-table>
      </div>
    </div>

    <!-- Satellite -->
    <v-dialog v-model="satDialog.show" max-width="640">
      <v-card class="fa-sat-dialog">
        <v-card-title>{{
          satDialog.id ? 'Edit satellite' : 'Add satellite'
        }}</v-card-title>
        <v-card-text>
          <div class="d-flex">
            <v-text-field
              v-model="satDialog.name"
              label="Name"
              density="compact"
              variant="outlined"
              class="mr-2"
              data-field="sat-name"
            />
            <v-text-field
              v-model="satDialog.target"
              label="COSMOS target (optional)"
              density="compact"
              variant="outlined"
              data-field="sat-target"
            />
          </div>
          <v-textarea
            v-model="satDialog.tle"
            label="TLE"
            placeholder="Paste 2 lines (or 3 with a name line)"
            rows="3"
            density="compact"
            variant="outlined"
            class="fa-mono"
            data-field="sat-tle"
            :hint="tleHint"
            persistent-hint
          />
          <v-switch
            v-model="satDialog.enabled"
            label="Predict contacts"
            color="primary"
            density="compact"
            hide-details
          />
          <div class="text-caption text-medium-emphasis">
            TLE source: pasted here. Other sources (files, URLs, telemetry) plug
            into the same update path.
          </div>
          <v-alert
            v-if="satDialog.error"
            type="error"
            density="compact"
            class="mt-2"
          >
            {{ satDialog.error }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-btn
            v-if="satDialog.id"
            color="error"
            variant="text"
            @click="deleteSatellite"
          >
            Delete
          </v-btn>
          <v-spacer />
          <v-btn variant="text" @click="satDialog.show = false">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="satDialog.busy"
            @click="saveSatellite"
          >
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Station -->
    <v-dialog v-model="stationDialog.show" max-width="520">
      <v-card class="fa-station-dialog">
        <v-card-title>{{
          stationDialog.id ? 'Edit ground station' : 'Add ground station'
        }}</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="stationDialog.name"
            label="Name"
            density="compact"
            variant="outlined"
            data-field="station-name"
          />
          <div class="d-flex">
            <v-text-field
              v-model.number="stationDialog.latitude"
              type="number"
              label="Latitude (°N)"
              density="compact"
              variant="outlined"
              class="mr-2"
              data-field="station-lat"
            />
            <v-text-field
              v-model.number="stationDialog.longitude"
              type="number"
              label="Longitude (°E)"
              density="compact"
              variant="outlined"
              data-field="station-lon"
            />
          </div>
          <div class="d-flex">
            <v-text-field
              v-model.number="stationDialog.altitude"
              type="number"
              label="Altitude (m)"
              density="compact"
              variant="outlined"
              class="mr-2"
            />
            <v-text-field
              v-model.number="stationDialog.min_elevation"
              type="number"
              label="Elevation mask (°)"
              density="compact"
              variant="outlined"
              data-field="station-mask"
            />
          </div>
          <v-switch
            v-model="stationDialog.enabled"
            label="Predict contacts"
            color="primary"
            density="compact"
            hide-details
          />
          <v-alert
            v-if="stationDialog.error"
            type="error"
            density="compact"
            class="mt-2"
          >
            {{ stationDialog.error }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-btn
            v-if="stationDialog.id"
            color="error"
            variant="text"
            @click="deleteStation"
          >
            Delete
          </v-btn>
          <v-spacer />
          <v-btn variant="text" @click="stationDialog.show = false"
            >Cancel</v-btn
          >
          <v-btn
            color="primary"
            variant="flat"
            :loading="stationDialog.busy"
            @click="saveStation"
          >
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Activity -->
    <v-dialog v-model="actDialog.show" max-width="640" scrollable>
      <v-card class="fa-activity-dialog">
        <v-card-title>
          {{ actDialog.id ? 'Edit activity' : 'Schedule activity' }}
        </v-card-title>
        <v-card-text>
          <v-select
            v-model="actDialog.contact_id"
            :items="contactChoices"
            item-title="title"
            item-value="value"
            label="Contact"
            density="compact"
            variant="outlined"
            class="mb-3"
          />
          <v-autocomplete
            v-model="actDialog.template_id"
            :items="templateChoices"
            item-title="name"
            item-value="id"
            label="Flow template"
            density="compact"
            variant="outlined"
            class="mb-3 fa-template-select"
            :disabled="!!actDialog.id"
            :hint="
              actDialog.id
                ? 'Delete and reschedule to use another flow'
                : 'Templates first; any flow with a Manual trigger works'
            "
            persistent-hint
            @update:model-value="templateChosen"
          >
            <template #item="{ props: itemProps, item }">
              <v-list-item v-bind="itemProps">
                <template #append>
                  <v-chip
                    v-if="item.raw.template"
                    size="x-small"
                    color="primary"
                    label
                  >
                    template
                  </v-chip>
                </template>
              </v-list-item>
            </template>
          </v-autocomplete>
          <div v-if="actDialog.template_id" class="mb-2">
            <div class="text-caption mb-1">Parameters</div>
            <param-form
              v-model="actDialog.params"
              :parameters="actParameters"
            />
          </div>
          <div class="d-flex align-center">
            <v-select
              v-model="actDialog.anchor"
              :items="anchors"
              label="Start relative to"
              density="compact"
              variant="outlined"
              class="mr-2 fa-anchor"
            />
            <v-text-field
              v-model.number="actDialog.offset"
              type="number"
              label="Offset (s)"
              hint="Negative is before, e.g. -120"
              persistent-hint
              density="compact"
              variant="outlined"
              class="fa-offset"
              data-field="offset"
            />
          </div>
          <div
            class="text-body-2 my-2 fa-start-preview"
            :class="startProblem ? 'text-error' : ''"
          >
            <v-icon size="16" class="mr-1">mdi-clock-start</v-icon>
            <template v-if="startPreview">
              Starts {{ fmtTime(startPreview) }} UTC ({{
                relText(startPreview)
              }}){{ startProblem ? ` — ${startProblem}` : '' }}
            </template>
          </div>
          <div class="d-flex align-center mb-2">
            <span class="text-body-2 mr-3">Mode</span>
            <v-btn-toggle
              v-model="actDialog.mode"
              mandatory
              density="compact"
              variant="outlined"
              divided
              color="primary"
              class="fa-act-mode"
            >
              <v-btn
                v-for="m in modes"
                :key="m.value"
                :value="m.value"
                size="small"
                :title="m.hint"
                :prepend-icon="m.icon"
              >
                {{ m.title }}
              </v-btn>
            </v-btn-toggle>
          </div>
          <div class="d-flex">
            <v-text-field
              v-if="actDialog.mode !== 'run'"
              v-model.number="actDialog.approval_margin"
              type="number"
              label="Approvals close before LOS (s)"
              density="compact"
              variant="outlined"
              class="mr-2"
            />
            <v-text-field
              v-model.number="actDialog.late_tolerance"
              type="number"
              label="May start late by (s)"
              hint="If the engine couldn't start it on time"
              persistent-hint
              density="compact"
              variant="outlined"
              class="mr-2"
            />
            <v-text-field
              v-model="actDialog.duration"
              type="number"
              label="Expected duration (s)"
              hint="For overlap warnings; empty means until LOS"
              persistent-hint
              density="compact"
              variant="outlined"
            />
          </div>
          <v-switch
            v-model="actDialog.stop_at_los"
            label="Stop the run at LOS"
            color="primary"
            density="compact"
            hide-details
          />
          <v-text-field
            v-model="actDialog.label"
            label="Label (optional)"
            density="compact"
            variant="outlined"
            class="mt-2"
          />
          <v-alert
            v-if="actDialog.error"
            type="error"
            density="compact"
            class="mt-2 fa-activity-error"
          >
            {{ actDialog.error }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="actDialog.show = false">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="actDialog.busy"
            :disabled="!actDialog.template_id || !actDialog.contact_id"
            class="fa-activity-save"
            @click="saveActivity"
          >
            {{ actDialog.id ? 'Save' : 'Schedule' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script>
import flowApi, { errorInfo } from './flowApi'
import ParamForm from './ParamForm.vue'
import { MODES } from './nodeTypes'

const POLL_MS = 2000
const COLORS = [
  '#4dacff',
  '#e07b39',
  '#6ccf7f',
  '#c58af9',
  '#f2c94c',
  '#ef6f6c',
]

function blankActivity() {
  return {
    show: false,
    id: null,
    contact_id: null,
    template_id: null,
    params: {},
    anchor: 'aos',
    offset: -120,
    mode: 'walk',
    approval_margin: 30,
    late_tolerance: 30,
    duration: '',
    stop_at_los: false,
    label: '',
    error: '',
    busy: false,
  }
}

export default {
  components: { ParamForm },
  props: {
    flows: { type: Array, default: () => [] },
  },
  emits: ['open-run', 'changed'],
  data() {
    return {
      schedule: null,
      now: Date.now() / 1000,
      rangeHours: 6,
      ranges: [
        { hours: 6, label: '6h' },
        { hours: 24, label: '24h' },
        { hours: 72, label: '3d' },
        { hours: 168, label: '7d' },
      ],
      selectedContactId: null,
      showFinished: true,
      modes: MODES,
      anchors: [
        { title: 'AOS (rise)', value: 'aos' },
        { title: 'TCA (closest)', value: 'tca' },
        { title: 'LOS (set)', value: 'los' },
      ],
      satDialog: { show: false },
      stationDialog: { show: false },
      actDialog: blankActivity(),
      timer: null,
    }
  },
  computed: {
    satellites() {
      return this.schedule?.satellites || []
    },
    stations() {
      return this.schedule?.stations || []
    },
    contacts() {
      return this.schedule?.contacts || []
    },
    activities() {
      return this.schedule?.activities || []
    },
    status() {
      return this.schedule?.status || {}
    },
    horizonDays() {
      const w = this.schedule?.window
      return w ? Math.round((w.end - this.now) / 86400) : 7
    },
    viewStart() {
      return this.now - Math.min(3600, this.rangeHours * 360)
    },
    viewEnd() {
      return this.viewStart + this.rangeHours * 3600
    },
    timelineStations() {
      return this.stations.filter((s) => s.enabled !== false)
    },
    listedContacts() {
      const start = this.now - 900
      return this.contacts.filter(
        (c) => c.los >= start && c.aos <= this.viewEnd,
      )
    },
    visibleActivities() {
      return this.activities.filter(
        (a) =>
          this.showFinished ||
          ['scheduled', 'needs_review', 'running'].includes(a.status),
      )
    },
    ticks() {
      const step =
        { 6: 3600, 24: 3 * 3600, 72: 12 * 3600, 168: 86400 }[this.rangeHours] ||
        3600
      const first = Math.ceil(this.viewStart / step) * step
      const ticks = []
      for (let t = first; t < this.viewEnd; t += step) {
        const iso = new Date(t * 1000).toISOString()
        ticks.push({
          t,
          label:
            step >= 86400 || iso.slice(11, 16) === '00:00'
              ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}`
              : iso.slice(11, 16),
        })
      }
      return ticks
    },
    satIndex() {
      return Object.fromEntries(this.satellites.map((s, i) => [s.id, i]))
    },
    templateChoices() {
      return [...this.flows].sort(
        (a, b) => Number(!!b.template) - Number(!!a.template),
      )
    },
    actTemplate() {
      return this.flows.find((f) => f.id === this.actDialog.template_id)
    },
    actParameters() {
      if (this.actDialog.id) {
        const existing = this.activities.find((a) => a.id === this.actDialog.id)
        if (existing) return existing.parameters
      }
      return this.actTemplate?.parameters || []
    },
    actContact() {
      return this.contacts.find((c) => c.id === this.actDialog.contact_id)
    },
    contactChoices() {
      return this.contacts
        .filter((c) => c.los > this.now || c.id === this.actDialog.contact_id)
        .map((c) => ({
          value: c.id,
          title: `${c.satellite} / ${c.station} · AOS ${this.fmtTime(c.aos)} · ${this.duration(c.duration)} · ${c.max_elevation.toFixed(0)}°`,
        }))
    },
    startPreview() {
      const c = this.actContact
      if (!c) return null
      return c[this.actDialog.anchor] + (Number(this.actDialog.offset) || 0)
    },
    startProblem() {
      if (!this.startPreview) return ''
      if (this.startPreview >= this.actContact.los) return 'after LOS'
      if (this.startPreview <= this.now) return 'already passed'
      return ''
    },
    tleHint() {
      const sat = this.satellites.find((s) => s.id === this.satDialog.id)
      if (!sat?.tle) return 'Two-line elements from your orbit provider'
      return `Current TLE epoch ${this.fmtTime(sat.tle.epoch)} UTC (${this.ageText(sat.tle_age_days)} old), from ${sat.tle.source}`
    },
  },
  mounted() {
    this.refresh()
    this.timer = setInterval(this.refresh, POLL_MS)
  },
  unmounted() {
    clearInterval(this.timer)
  },
  methods: {
    async refresh() {
      try {
        this.schedule = await flowApi.schedule()
        this.now = this.schedule.time
      } catch {
        this.now = Date.now() / 1000 // engine offline: the toolbar shows it
      }
    },
    notifyError(title, error) {
      this.$notify.caution({ title, body: errorInfo(error).message })
    },

    // ---- formatting ------------------------------------------------------
    fmtTime(t) {
      if (t == null) return ''
      return new Date(t * 1000).toISOString().slice(0, 19).replace('T', ' ')
    },
    duration(seconds) {
      const s = Math.round(seconds)
      return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
    },
    relText(t) {
      const delta = t - this.now
      const abs = Math.abs(delta)
      let text
      if (abs < 60) text = `${Math.round(abs)}s`
      else if (abs < 3600)
        text = `${Math.floor(abs / 60)}m ${Math.round(abs % 60)}s`
      else if (abs < 86400)
        text = `${Math.floor(abs / 3600)}h ${Math.floor((abs % 3600) / 60)}m`
      else text = `${(abs / 86400).toFixed(1)}d`
      return delta >= 0 ? `in ${text}` : `${text} ago`
    },
    ageText(days) {
      if (days == null) return ''
      return days < 1 ? `${Math.round(days * 24)}h` : `${days.toFixed(1)}d`
    },
    countdown(c) {
      return c.aos > this.now
        ? `AOS ${this.relText(c.aos)}`
        : `in contact, LOS ${this.relText(c.los)}`
    },
    anchorText(a) {
      const o = Number(a.offset) || 0
      return `${a.anchor.toUpperCase()} ${o < 0 ? '-' : '+'} ${Math.abs(o)}s`
    },
    statusColor(status) {
      return (
        {
          scheduled: 'primary',
          needs_review: 'warning',
          running: 'info',
          done: 'success',
          failed: 'error',
          stopped: 'warning',
          missed: 'error',
        }[status] || undefined
      )
    },
    satColor(i) {
      return COLORS[i % COLORS.length]
    },
    editable(a) {
      return ['scheduled', 'needs_review'].includes(a.status)
    },

    // ---- timeline --------------------------------------------------------
    frac(t) {
      return Math.min(
        1,
        Math.max(0, (t - this.viewStart) / (this.viewEnd - this.viewStart)),
      )
    },
    pct(t) {
      return `${this.frac(t) * 100}%`
    },
    contactsFor(stationId) {
      return this.contacts.filter(
        (c) =>
          c.station_id === stationId &&
          c.los >= this.viewStart &&
          c.aos <= this.viewEnd,
      )
    },
    activitiesFor(stationId) {
      return this.activities.filter(
        (a) =>
          a.contact.station_id === stationId &&
          a.start >= this.viewStart &&
          a.start <= this.viewEnd,
      )
    },
    passStyle(c) {
      const left = this.frac(c.aos)
      const width = Math.max(this.frac(c.los) - left, 0.002)
      return {
        left: `${left * 100}%`,
        width: `${width * 100}%`,
        background: this.satColor(this.satIndex[c.satellite_id] || 0),
        opacity: c.los < this.now ? 0.45 : 0.9,
      }
    },
    passTitle(c) {
      return `${c.satellite} over ${c.station}\nAOS ${this.fmtTime(c.aos)}  LOS ${this.fmtTime(c.los)}\n${this.duration(c.duration)}, max elevation ${c.max_elevation.toFixed(1)}°`
    },

    // ---- satellites and stations -----------------------------------------
    editSatellite(sat) {
      this.satDialog = {
        show: true,
        id: sat?.id || null,
        name: sat?.name || '',
        target: sat?.target || '',
        enabled: sat ? sat.enabled !== false : true,
        tle: sat?.tle ? `${sat.tle.line1}\n${sat.tle.line2}` : '',
        original: sat?.tle ? `${sat.tle.line1}\n${sat.tle.line2}` : '',
        error: '',
        busy: false,
      }
    },
    async saveSatellite() {
      const d = this.satDialog
      d.busy = true
      d.error = ''
      try {
        const body = { name: d.name, target: d.target, enabled: d.enabled }
        if (d.tle.trim() && d.tle.trim() !== d.original) body.tle = d.tle
        await flowApi.saveSatellite(d.id, body)
        d.show = false
        await this.refresh()
      } catch (error) {
        d.error = errorInfo(error).message
      } finally {
        d.busy = false
      }
    },
    async deleteSatellite() {
      if (!window.confirm(`Delete satellite '${this.satDialog.name}'?`)) return
      try {
        await flowApi.deleteSatellite(this.satDialog.id)
        this.satDialog.show = false
        await this.refresh()
      } catch (error) {
        this.satDialog.error = errorInfo(error).message
      }
    },
    editStation(station) {
      this.stationDialog = {
        show: true,
        id: station?.id || null,
        name: station?.name || '',
        latitude: station?.latitude ?? 0,
        longitude: station?.longitude ?? 0,
        altitude: station?.altitude ?? 0,
        min_elevation: station?.min_elevation ?? 10,
        enabled: station ? station.enabled !== false : true,
        error: '',
        busy: false,
      }
    },
    async saveStation() {
      const d = this.stationDialog
      d.busy = true
      d.error = ''
      try {
        await flowApi.saveStation(d.id, {
          name: d.name,
          latitude: d.latitude,
          longitude: d.longitude,
          altitude: d.altitude,
          min_elevation: d.min_elevation,
          enabled: d.enabled,
        })
        d.show = false
        await this.refresh()
      } catch (error) {
        d.error = errorInfo(error).message
      } finally {
        d.busy = false
      }
    },
    async deleteStation() {
      if (!window.confirm(`Delete station '${this.stationDialog.name}'?`))
        return
      try {
        await flowApi.deleteStation(this.stationDialog.id)
        this.stationDialog.show = false
        await this.refresh()
      } catch (error) {
        this.stationDialog.error = errorInfo(error).message
      }
    },
    async recompute() {
      try {
        await flowApi.recompute()
        await this.refresh()
      } catch (error) {
        this.notifyError('Recompute failed', error)
      }
    },
    async pause() {
      await flowApi.pauseScheduler()
      await this.refresh()
    },
    async resume() {
      await flowApi.resumeScheduler()
      await this.refresh()
    },

    // ---- activities ------------------------------------------------------
    newActivity(contact) {
      this.selectedContactId = contact.id
      this.actDialog = {
        ...blankActivity(),
        show: true,
        contact_id: contact.id,
      }
    },
    templateChosen() {
      const template = this.actTemplate
      if (!template) return
      this.actDialog.mode = template.mode || 'run'
      this.actDialog.params = {}
    },
    editActivity(a) {
      this.actDialog = {
        ...blankActivity(),
        show: true,
        id: a.id,
        contact_id: a.contact.id,
        template_id: a.template_id,
        params: { ...a.params },
        anchor: a.anchor,
        offset: a.offset,
        mode: a.mode,
        approval_margin: a.approval_margin,
        late_tolerance: a.late_tolerance,
        duration: a.duration ?? '',
        stop_at_los: a.stop_at_los,
        label: a.label,
      }
    },
    async saveActivity() {
      const d = this.actDialog
      d.busy = true
      d.error = ''
      const body = {
        contact_id: d.contact_id,
        template_id: d.template_id,
        params: d.params,
        anchor: d.anchor,
        offset: Number(d.offset) || 0,
        mode: d.mode,
        approval_margin: Number(d.approval_margin) || 0,
        late_tolerance: Number(d.late_tolerance) || 0,
        duration: d.duration === '' ? null : Number(d.duration),
        stop_at_los: d.stop_at_los,
      }
      if (d.label) body.label = d.label
      try {
        await flowApi.saveActivity(d.id, body)
        d.show = false
        await this.refresh()
      } catch (error) {
        d.error = errorInfo(error).message
      } finally {
        d.busy = false
      }
    },
    async accept(a) {
      try {
        await flowApi.acceptActivity(a.id)
        await this.refresh()
      } catch (error) {
        this.notifyError('Could not accept', error)
      }
    },
    async refreshTemplate(a) {
      try {
        await flowApi.refreshActivityTemplate(a.id)
        await this.refresh()
      } catch (error) {
        this.notifyError('Could not update from template', error)
      }
    },
    async removeActivity(a) {
      const verb = a.status === 'running' ? 'Stop and delete' : 'Delete'
      if (!window.confirm(`${verb} activity '${a.label}'?`)) return
      try {
        await flowApi.deleteActivity(a.id)
        await this.refresh()
      } catch (error) {
        this.notifyError('Could not delete', error)
      }
    },
  },
}
</script>

<style>
.fa-contacts {
  display: flex;
  flex: 1;
  min-height: 0;
}
.fa-contacts-left {
  width: 280px;
  flex-shrink: 0;
  overflow-y: auto;
  border-right: 1px solid rgba(255, 255, 255, 0.1);
}
.fa-contacts-center {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.fa-contacts-tables {
  flex: 1;
  overflow-y: auto;
  padding-bottom: 12px;
}
.fa-timeline {
  position: relative;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 4px;
  padding-bottom: 4px;
  flex-shrink: 0;
}
.fa-tl-axis {
  position: relative;
  height: 22px;
  margin-left: 120px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-tl-tick {
  position: absolute;
  top: 3px;
  font-size: 10px;
  color: #9fb0bf;
  transform: translateX(-50%);
  white-space: nowrap;
}
.fa-tl-row {
  display: flex;
  height: 30px;
  align-items: center;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
}
.fa-tl-label {
  width: 120px;
  flex-shrink: 0;
  padding: 0 8px;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fa-tl-lane {
  position: relative;
  flex: 1;
  height: 100%;
}
.fa-tl-pass {
  position: absolute;
  top: 6px;
  height: 18px;
  border-radius: 3px;
  cursor: pointer;
  overflow: hidden;
  min-width: 3px;
}
.fa-tl-pass.fa-tl-selected {
  outline: 2px solid #fff;
  z-index: 2;
}
.fa-tl-pass-label {
  font-size: 10px;
  color: #0d1b2a;
  padding-left: 3px;
  white-space: nowrap;
  font-weight: 600;
}
.fa-tl-activity {
  position: absolute;
  top: 1px;
  width: 0;
  height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 7px solid #fff;
  transform: translateX(-5px);
  cursor: pointer;
  z-index: 3;
}
.fa-act-needs_review,
.fa-act-stopped {
  border-top-color: rgb(var(--v-theme-warning));
}
.fa-act-failed,
.fa-act-missed {
  border-top-color: rgb(var(--v-theme-error));
}
.fa-act-done {
  border-top-color: rgb(var(--v-theme-success));
}
.fa-act-running {
  border-top-color: rgb(var(--v-theme-info));
}
.fa-tl-now {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  background: #ef6f6c;
  pointer-events: none;
}
.fa-row-selected {
  background: rgba(77, 172, 255, 0.12);
}
.fa-activities td,
.fa-contact-list td {
  vertical-align: top;
  padding-top: 4px !important;
  padding-bottom: 4px !important;
}
.fa-activity-message {
  max-width: 280px;
}
.fa-anchor {
  max-width: 200px;
}
</style>
