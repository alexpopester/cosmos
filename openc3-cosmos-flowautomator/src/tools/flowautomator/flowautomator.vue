<!--
# Flow Automator: build automation flows by wiring nodes together. Flows are
# stored and run by the FLOW_ENGINE microservice; this tool is the editor
# and the live view of their runs.
-->

<template>
  <div class="fa-root" :style="rootHeight ? { height: `${rootHeight}px` } : {}">
    <top-bar :menus="menus" :title="title" />
    <!-- Row 1: what's open and global status. Row 2 (Flows): running the flow and the layout -->
    <v-sheet class="fa-toolbar" color="surface">
      <div class="fa-toolbar-row">
        <v-btn-toggle
          v-model="view"
          mandatory
          density="compact"
          variant="outlined"
          divided
          class="fa-view-toggle mr-3"
          @update:model-value="viewChanged"
        >
          <v-btn value="flows" size="small" prepend-icon="mdi-sitemap-outline">
            Flows
          </v-btn>
          <v-btn
            value="contacts"
            size="small"
            prepend-icon="mdi-satellite-uplink"
          >
            Contacts
            <v-badge
              v-if="contactStatus.needs_review"
              :content="contactStatus.needs_review"
              color="warning"
              inline
            />
          </v-btn>
        </v-btn-toggle>
        <template v-if="view === 'flows'">
          <!-- Always empty so typing searches from scratch; the open flow's name
             shows as the placeholder -->
          <v-autocomplete
            v-model:search="flowSearch"
            :model-value="null"
            :items="flows"
            item-title="name"
            item-value="id"
            :placeholder="
              flowId ? flowSettings.name : 'Open a flow (type to search)'
            "
            persistent-placeholder
            auto-select-first
            density="compact"
            variant="outlined"
            hide-details
            prepend-inner-icon="mdi-sitemap-outline"
            class="fa-flow-select mr-2"
            :class="{ 'fa-flow-open': flowId }"
            :title="
              flowId
                ? `Open: ${flowSettings.name}. Type to open another flow.`
                : ''
            "
            @update:model-value="(id) => id && openFlow(id)"
            @update:menu="(open) => open && refreshFlows()"
          >
            <template #item="{ props: itemProps, item }">
              <v-list-item v-bind="itemProps">
                <template #append>
                  <v-chip
                    v-if="item.raw.armed"
                    size="x-small"
                    color="success"
                    label
                  >
                    armed
                  </v-chip>
                  <v-chip
                    v-if="item.raw.active_runs"
                    size="x-small"
                    color="primary"
                    label
                    class="ml-1"
                  >
                    running
                  </v-chip>
                </template>
              </v-list-item>
            </template>
          </v-autocomplete>
          <v-chip
            v-if="flowVersion"
            size="small"
            label
            class="mr-2 fa-version-chip"
            :title="`Version ${flowVersion}. Click for every version.`"
            @click="openHistory()"
          >
            v{{ flowVersion }}
          </v-chip>
          <v-chip
            v-if="dirty"
            size="small"
            label
            color="warning"
            variant="tonal"
            class="mr-2"
          >
            unsaved changes
          </v-chip>
          <v-chip
            v-if="flowSettings.dry_run"
            size="small"
            label
            color="info"
            variant="flat"
            class="mr-2 fa-dry-mode-chip"
            title="Every run of this flow is a dry run: nothing is sent"
          >
            DRY-RUN MODE
          </v-chip>
          <v-divider vertical class="mx-2" />
          <v-btn-toggle
            :model-value="editing ? 'edit' : 'view'"
            mandatory
            density="compact"
            variant="outlined"
            divided
            color="primary"
            class="fa-edit-toggle mr-2"
            @update:model-value="(v) => setEditing(v === 'edit')"
          >
            <v-btn
              value="view"
              size="small"
              prepend-icon="mdi-eye-outline"
              title="Watch runs: hides the node palette and settings and locks the canvas"
            >
              View
            </v-btn>
            <v-btn
              value="edit"
              size="small"
              prepend-icon="mdi-pencil"
              title="Change the flow: shows the node palette and settings"
            >
              Edit
            </v-btn>
          </v-btn-toggle>
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-plus"
            class="mr-2"
            title="Start a new flow"
            @click="newFlow"
          >
            New
          </v-btn>
          <v-btn
            v-if="editing || dirty"
            size="small"
            :variant="dirty ? 'flat' : 'tonal'"
            prepend-icon="mdi-content-save"
            :color="dirty ? 'primary' : undefined"
            class="mr-2"
            :loading="saving"
            @click="save"
          >
            Save
          </v-btn>
        </template>
        <template v-else>
          <span class="text-body-2 fa-status-text">
            <template v-if="contactStatus.paused">
              <v-icon color="warning" size="small">mdi-pause-circle</v-icon>
              Scheduler paused
            </template>
            <template v-else>
              {{ contactStatus.scheduled || 0 }} scheduled ·
              {{ contactStatus.running || 0 }} running
            </template>
          </span>
        </template>
        <!-- Status and Stop all stay together at the right, also when the row wraps -->
        <div class="fa-toolbar-end">
          <v-menu v-if="allApprovals.length" max-width="560">
            <template #activator="{ props: menuProps }">
              <v-chip
                v-bind="menuProps"
                size="small"
                color="warning"
                variant="flat"
                class="mr-2 fa-approvals-chip"
                prepend-icon="mdi-hand-back-right"
              >
                {{ allApprovals.length }} awaiting approval
              </v-chip>
            </template>
            <v-list density="compact">
              <v-list-item
                v-for="a in allApprovals"
                :key="a.id"
                :title="`${a.flow_name}: ${a.label}`"
                :subtitle="a.preview"
                prepend-icon="mdi-hand-back-right"
                @click="openApproval(a)"
              />
            </v-list>
          </v-menu>
          <!-- Opens to the left: below, it would cover the Show buttons on the second row -->
          <v-tooltip location="start" open-delay="300">
            <template #activator="{ props: tip }">
              <v-chip
                v-bind="tip"
                size="small"
                :color="engineOnline ? 'success' : 'error'"
                variant="tonal"
                class="mr-2 fa-engine-chip"
                @click="showAllRunning"
              >
                <v-icon start size="small">
                  {{ engineOnline ? 'mdi-engine' : 'mdi-engine-off' }}
                </v-icon>
                <template v-if="engineOnline">
                  {{ engineStatus.armed }} armed ·
                  {{ engineStatus.active_runs.length }} running
                </template>
                <template v-else>Engine offline</template>
              </v-chip>
            </template>
            <span v-if="engineOnline">
              Click to see every running flow. Flow engine is running. Triggers
              checked
              {{ engineStatus.triggers.watching }} ways.
              <span v-if="engineStatus.triggers.error">
                Last error: {{ engineStatus.triggers.error }}
              </span>
            </span>
            <span v-else>
              Can't reach the FLOW_ENGINE microservice at {{ routePrefix }}.
              {{ engineError }}
            </span>
          </v-tooltip>
          <v-btn
            size="small"
            color="error"
            variant="flat"
            prepend-icon="mdi-octagon"
            title="Disarm every flow, stop every run and pause the contact scheduler"
            @click="confirmStopAll = true"
          >
            Stop all
          </v-btn>
        </div>
      </div>

      <div v-if="view === 'flows'" class="fa-toolbar-row fa-run-row">
        <span class="fa-group-label">Triggers</span>
        <v-switch
          :model-value="armed"
          :disabled="!flowId"
          color="success"
          density="compact"
          hide-details
          inset
          class="flex-grow-0 mr-1"
          :title="
            armed
              ? 'Armed: triggers can start runs'
              : 'Disarmed: triggers are ignored'
          "
          @update:model-value="setArmed"
        />
        <span class="text-body-2 mr-3" :class="armed ? 'text-success' : ''">
          {{ armed ? 'Armed' : 'Disarmed' }}
        </span>
        <span class="fa-group-label">Mode</span>
        <v-btn-toggle
          v-model="flowSettings.mode"
          mandatory
          density="compact"
          variant="outlined"
          divided
          color="primary"
          class="fa-mode-toggle mr-3"
          @update:model-value="changed"
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
        <v-menu v-if="triggerNodes.length > 1">
          <template #activator="{ props: menuProps }">
            <v-btn
              v-bind="menuProps"
              size="small"
              color="primary"
              variant="flat"
              prepend-icon="mdi-play"
              append-icon="mdi-menu-down"
              class="mr-2"
            >
              Run now
            </v-btn>
          </template>
          <v-list density="compact">
            <v-list-item
              v-for="n in triggerNodes"
              :key="n.id"
              :title="n.data.config.label || nodeTypes[n.data.kind].label"
              :subtitle="'Start from this trigger'"
              @click="runNow(n.id)"
            />
          </v-list>
        </v-menu>
        <v-btn
          v-else
          size="small"
          color="primary"
          variant="flat"
          prepend-icon="mdi-play"
          class="mr-2"
          :disabled="!triggerNodes.length"
          @click="runNow(triggerNodes[0] && triggerNodes[0].id)"
        >
          Run now
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          color="info"
          prepend-icon="mdi-test-tube"
          class="mr-2 fa-dry-run-btn"
          title="Rehearse: check commands without sending them, optionally skipping waits"
          :disabled="!triggerNodes.length"
          @click="openRunDialog(manualTrigger, { dry_run: true })"
        >
          Dry run
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          color="warning"
          prepend-icon="mdi-stop"
          class="mr-2"
          title="Stop this flow's runs"
          :disabled="!activeRunIds.length"
          @click="stopFlowRuns"
        >
          Stop
        </v-btn>
        <v-spacer />
        <span class="fa-group-label">Show</span>
        <v-btn-toggle
          v-model="layout"
          mandatory
          density="compact"
          variant="outlined"
          divided
          color="primary"
          class="fa-layout-toggle"
        >
          <v-btn
            value="flow"
            size="small"
            prepend-icon="mdi-sitemap-outline"
            title="Only the flow: the log folds to a bar at the bottom"
          >
            Flow
          </v-btn>
          <v-btn
            value="split"
            size="small"
            prepend-icon="mdi-view-split-horizontal"
            title="The flow with the runs and log below it"
          >
            Both
          </v-btn>
          <v-btn
            value="log"
            size="small"
            prepend-icon="mdi-format-list-bulleted"
            title="Only the runs and log, full height"
          >
            Log
          </v-btn>
        </v-btn-toggle>
      </div>
    </v-sheet>

    <div v-if="view === 'contacts'" class="fa-contacts-wrap">
      <approval-panel
        :approvals="allApprovals"
        :now="engineStatus.time"
        show-flow
        class="fa-approvals-inline"
        @decided="pollStatus"
      />
      <contacts-view :flows="flows" @open-run="openActivityRun" />
    </div>
    <div v-show="view === 'flows'" class="fa-main">
      <template v-if="editing">
        <div v-if="showPalette" class="fa-palette">
          <!-- Header and filter stay put while the list scrolls -->
          <div class="fa-palette-top">
            <div class="fa-panel-head">
              <span>Add nodes</span>
              <v-spacer />
              <v-btn
                icon="mdi-chevron-double-left"
                size="x-small"
                variant="text"
                title="Hide the node palette"
                class="fa-palette-hide"
                @click="showPalette = false"
              />
            </div>
            <v-text-field
              v-model="paletteFilter"
              placeholder="Filter nodes"
              prepend-inner-icon="mdi-magnify"
              density="compact"
              variant="outlined"
              hide-details
              clearable
              class="mx-2 mb-1 fa-palette-filter"
            />
          </div>
          <div v-for="category in paletteCategories" :key="category.key">
            <div
              class="px-2 pt-2 pb-1 fa-category"
              :style="{ color: category.color }"
            >
              {{ category.label }}
            </div>
            <div
              v-for="(spec, type) in category.types"
              :key="type"
              class="fa-palette-item"
              :style="{ '--fa-color': category.color }"
              draggable="true"
              :title="spec.description"
              @dragstart="onDragStart($event, type)"
              @dblclick="addNode(type)"
            >
              <v-icon size="16" class="mr-2" :color="category.color">
                {{ spec.icon }}
              </v-icon>
              {{ spec.label }}
            </div>
          </div>
          <div class="text-caption fa-hint pa-2">
            Drag a node onto the canvas, or double-click it.
          </div>
        </div>
        <div
          v-else
          class="fa-rail fa-rail-left"
          title="Show the node palette"
          @click="showPalette = true"
        >
          <v-icon size="18">mdi-chevron-double-right</v-icon>
          <span class="fa-rail-label">Add nodes</span>
        </div>
      </template>

      <div ref="center" class="fa-center">
        <!-- Floats over the canvas; sits above the log instead of covering it in log-only -->
        <approval-panel
          :approvals="flowApprovals"
          :now="engineStatus.time"
          :class="{ 'fa-approvals-inline': layout === 'log' }"
          @decided="pollStatus"
        />
        <div
          v-show="layout !== 'log'"
          class="fa-canvas"
          @drop="onDrop"
          @dragover.prevent
        >
          <VueFlow
            id="flow-automator"
            v-model:nodes="nodes"
            v-model:edges="edges"
            :default-edge-options="defaultEdgeOptions"
            :delete-key-code="editing ? ['Delete', 'Backspace'] : null"
            :nodes-draggable="editing"
            :nodes-connectable="editing"
            :snap-to-grid="true"
            :snap-grid="[10, 10]"
            :min-zoom="0.2"
            :max-zoom="2"
            :is-valid-connection="isValidConnection"
            class="fa-flow"
            @connect="onConnect"
            @node-drag-stop="dirty = true"
            @nodes-change="onNodesChange"
            @edges-change="onEdgesChange"
            @nodes-initialized="onNodesInitialized"
            @pane-click="selectedNodeId = null"
            @node-click="selectedNodeId = $event.node.id"
          >
            <template #node-flow="nodeProps">
              <flow-node v-bind="nodeProps" />
            </template>
            <Background pattern-color="#3a4a5a" :gap="20" />
            <Controls :show-interactive="false" />
            <MiniMap pannable zoomable :node-color="miniMapColor" />
            <div v-if="!flowId && !nodes.length" class="fa-empty">
              <v-icon size="48" class="mb-2">mdi-sitemap-outline</v-icon>
              <div>Open a flow, or click New to start one.</div>
            </div>
            <div v-if="!editing && flowId" class="fa-view-badge">
              <v-icon size="14" class="mr-1">mdi-lock-outline</v-icon>
              Viewing · switch to Edit to change this flow
            </div>
          </VueFlow>
        </div>
        <div
          v-if="layout === 'split'"
          class="fa-splitter"
          title="Drag to resize"
          @pointerdown="startResize"
        />
        <div
          class="fa-bottom"
          :class="`fa-bottom-${layout}`"
          :style="layout === 'split' ? { height: `${logHeight}px` } : {}"
        >
          <run-panel
            ref="runPanel"
            :runs="runs"
            :run="selectedRun"
            :problems="problems"
            :node-label="nodeLabel"
            :active-runs="engineStatus.active_runs"
            :flow-id="flowId"
            :collapsed="layout === 'flow'"
            @select-run="selectRun"
            @stop-run="stopRun"
            @focus-node="focusNode"
            @open-version="openHistory"
            @open-run="openRunFromList"
            @expand="layout = 'split'"
          >
            <template #actions>
              <template v-if="layout === 'flow'">
                <v-btn
                  size="small"
                  variant="tonal"
                  color="primary"
                  prepend-icon="mdi-chevron-up"
                  class="fa-show-log"
                  title="Show the runs and log under the flow"
                  @click="layout = 'split'"
                >
                  Show log
                </v-btn>
              </template>
              <template v-else-if="layout === 'split'">
                <v-btn
                  size="small"
                  variant="text"
                  icon="mdi-chevron-down"
                  class="fa-hide-log"
                  title="Hide the log (Show > Flow)"
                  @click="layout = 'flow'"
                />
                <v-btn
                  size="small"
                  variant="text"
                  icon="mdi-arrow-expand-up"
                  class="fa-max-log"
                  title="Log full height (Show > Log)"
                  @click="layout = 'log'"
                />
              </template>
              <v-btn
                v-else
                size="small"
                variant="tonal"
                color="primary"
                prepend-icon="mdi-sitemap-outline"
                class="fa-show-flow"
                title="Show the flow above the log again"
                @click="layout = 'split'"
              >
                Show flow
              </v-btn>
            </template>
          </run-panel>
        </div>
      </div>

      <template v-if="editing">
        <div v-if="showInspector" class="fa-side">
          <div class="fa-panel-head">
            <span>{{ selectedNode ? 'Node settings' : 'Flow settings' }}</span>
            <v-spacer />
            <v-btn
              icon="mdi-chevron-double-right"
              size="x-small"
              variant="text"
              title="Hide the settings panel"
              class="fa-inspector-hide"
              @click="showInspector = false"
            />
          </div>
          <node-inspector
            :node="selectedNode"
            :flow="flowSettings"
            :flows="flows"
            :scripts="scripts"
            :problems="
              selectedNode ? flowContext.nodeProblems[selectedNode.id] : []
            "
            @changed="onInspectorChange"
            @delete-node="deleteSelected"
          />
        </div>
        <div
          v-else
          class="fa-rail fa-rail-right"
          title="Show the settings panel"
          @click="showInspector = true"
        >
          <v-icon size="18">mdi-chevron-double-left</v-icon>
          <span class="fa-rail-label">
            {{ selectedNode ? 'Node settings' : 'Flow settings' }}
          </span>
        </div>
      </template>
    </div>

    <v-dialog v-model="confirmStopAll" max-width="440">
      <v-card>
        <v-card-title class="text-error">
          <v-icon class="mr-2">mdi-octagon</v-icon>Stop all flows?
        </v-card-title>
        <v-card-text>
          This disarms every flow and stops every run in this scope, including
          flows you don't have open. Scripts started by runs that are waiting on
          them are stopped too. The contact scheduler is paused, so scheduled
          activities won't start until you resume it.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmStopAll = false">Cancel</v-btn>
          <v-btn color="error" variant="flat" @click="stopAll">Stop all</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="confirmDiscard.show" max-width="420">
      <v-card>
        <v-card-title>Discard unsaved changes?</v-card-title>
        <v-card-text>
          '{{ flowSettings.name }}' has changes that aren't saved.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmDiscard.show = false">
            Keep editing
          </v-btn>
          <v-btn color="warning" variant="flat" @click="discardAndContinue">
            Discard
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <audit-log v-model="showAudit" />

    <history-dialog
      v-model="showHistory"
      :flow-id="flowId"
      :flow-name="flowSettings.name"
      :current-version="flowVersion"
      :initial-version="historyVersion"
      :start-tab="historyTab"
      @restored="onRestored"
    />
    <run-history
      v-model="showRunHistory"
      :flows="flows"
      @open-run="openRunFromHistory"
    />
    <alerts-dialog v-model="showAlerts" />

    <v-dialog v-model="runDialog.show" max-width="560" scrollable>
      <v-card class="fa-run-dialog">
        <v-card-title>
          {{ runDialog.dry_run ? 'Dry run' : 'Run' }} '{{ flowSettings.name }}'
        </v-card-title>
        <v-card-text>
          <v-switch
            v-model="runDialog.dry_run"
            label="Dry run: check commands but send nothing"
            color="info"
            density="compact"
            hide-details
            :disabled="flowSettings.dry_run"
            class="fa-run-dry"
          />
          <v-switch
            v-if="runDialog.dry_run"
            v-model="runDialog.skip_waits"
            label="Skip waits (waits assume their conditions come true)"
            color="info"
            density="compact"
            hide-details
            class="fa-run-skip"
          />
          <div class="text-caption text-medium-emphasis mb-3">
            <template v-if="runDialog.dry_run">
              Commands are checked against the COSMOS command definitions but
              not sent; scripts, telemetry writes and notifications are skipped.
              Approvals still ask, so you can rehearse them.
            </template>
            <template v-else> A real run: commands are sent. </template>
          </div>
          <div class="d-flex align-center mb-3">
            <span class="text-body-2 mr-3">Mode</span>
            <v-btn-toggle
              v-model="runDialog.mode"
              mandatory
              density="compact"
              variant="outlined"
              divided
              color="primary"
            >
              <v-btn
                v-for="m in modes"
                :key="m.value"
                :value="m.value"
                size="small"
                :prepend-icon="m.icon"
              >
                {{ m.title }}
              </v-btn>
            </v-btn-toggle>
          </div>
          <template v-if="(flowSettings.parameters || []).length">
            <div class="text-caption mb-1">Parameters</div>
            <param-form
              v-model="runDialog.values"
              :parameters="flowSettings.parameters || []"
            />
          </template>
          <template v-if="flowSettings.template || usesContact">
            <div class="text-caption mb-1">Contact</div>
            <v-btn-toggle
              v-model="runDialog.contactKind"
              mandatory
              density="compact"
              variant="outlined"
              divided
              class="mb-2 fa-run-contact"
            >
              <v-btn value="none" size="small">None</v-btn>
              <v-btn value="simulated" size="small">Simulated</v-btn>
              <v-btn value="predicted" size="small">A predicted pass</v-btn>
            </v-btn-toggle>
            <div v-if="runDialog.contactKind === 'simulated'" class="d-flex">
              <v-text-field
                v-model.number="runDialog.aos_in"
                type="number"
                label="AOS in (s)"
                density="compact"
                variant="outlined"
                class="mr-2"
                data-field="sim-aos"
              />
              <v-text-field
                v-model.number="runDialog.duration"
                type="number"
                label="Lasting (s)"
                density="compact"
                variant="outlined"
              />
            </div>
            <v-select
              v-if="runDialog.contactKind === 'predicted'"
              v-model="runDialog.contact_id"
              :items="contactItems"
              label="Pass"
              density="compact"
              variant="outlined"
              no-data-text="No passes predicted in the next day"
            />
            <div
              v-if="runDialog.contactKind === 'none'"
              class="text-caption text-medium-emphasis"
            >
              Without a contact, Wait for contact and Contact time left fail and
              contact_* variables are empty.
            </div>
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="runDialog.show = false">Cancel</v-btn>
          <v-btn
            :color="runDialog.dry_run ? 'info' : 'primary'"
            variant="flat"
            :prepend-icon="runDialog.dry_run ? 'mdi-test-tube' : 'mdi-play'"
            class="fa-run-go"
            @click="runFromDialog"
          >
            {{ runDialog.dry_run ? 'Dry run' : 'Run' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="conflict.show" max-width="520">
      <v-card class="fa-conflict-dialog">
        <v-card-title class="text-warning">
          <v-icon class="mr-2">mdi-source-merge</v-icon>Someone else saved this
          flow
        </v-card-title>
        <v-card-text>
          {{ conflict.message }}. Saving now would overwrite their changes. You
          can reload their version (your unsaved changes are lost), or save
          yours over it (theirs stays in the history).
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="openHistory()">See history</v-btn>
          <v-spacer />
          <v-btn variant="text" @click="conflict.show = false">Cancel</v-btn>
          <v-btn variant="tonal" @click="reloadTheirs">Reload theirs</v-btn>
          <v-btn
            color="warning"
            variant="flat"
            class="fa-overwrite"
            @click="overwrite"
          >
            Save mine
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="noteDialog.show" max-width="480">
      <v-card class="fa-note-dialog">
        <v-card-title>Save with a note</v-card-title>
        <v-card-text>
          <v-textarea
            v-model="noteDialog.note"
            label="What changed, and why"
            rows="3"
            auto-grow
            autofocus
            density="compact"
            variant="outlined"
            data-field="save-note"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="noteDialog.show = false">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            class="fa-note-save"
            @click="saveWithNote"
          >
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <input
      ref="importInput"
      type="file"
      accept=".json,application/json"
      style="display: none"
      @change="importFile"
    />
  </div>
</template>

<script>
import { reactive } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { TopBar } from '@openc3/vue-common/components'
import FlowNode from './FlowNode.vue'
import NodeInspector from './NodeInspector.vue'
import RunPanel from './RunPanel.vue'
import ApprovalPanel from './ApprovalPanel.vue'
import ContactsView from './ContactsView.vue'
import ParamForm from './ParamForm.vue'
import AuditLog from './AuditLog.vue'
import HistoryDialog from './HistoryDialog.vue'
import RunHistory from './RunHistory.vue'
import AlertsDialog from './AlertsDialog.vue'
import flowApi, { ROUTE_PREFIX, errorInfo } from './flowApi'
import {
  CATEGORIES,
  MODES,
  NODE_TYPES,
  categoryColor,
  defaultData,
  outputsFor,
} from './nodeTypes'

const STATUS_POLL_MS = 1000
const RUN_POLL_MS = 500
const LAYOUT_KEY = 'openc3.flowautomator.layout'
const LAYOUTS = ['flow', 'split', 'log']
// Below this zoom node text is too small to read
const READABLE_ZOOM = 0.6

// Panel layout is a per-browser convenience; storage can be missing or blocked
function loadLayout() {
  try {
    return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}
  } catch {
    return {}
  }
}

function blankSettings() {
  return {
    name: 'New flow',
    description: '',
    policy: 'skip',
    mode: 'walk',
    approval_timeout: 0,
    variables: {},
    template: false,
    parameters: [],
    dry_run: false,
  }
}

export default {
  components: {
    TopBar,
    VueFlow,
    Background,
    Controls,
    MiniMap,
    FlowNode,
    NodeInspector,
    RunPanel,
    ApprovalPanel,
    ContactsView,
    ParamForm,
    AuditLog,
    HistoryDialog,
    RunHistory,
    AlertsDialog,
  },
  provide() {
    return { flowContext: this.flowContext }
  },
  setup() {
    const {
      screenToFlowCoordinate,
      fitView,
      setCenter,
      setViewport,
      findNode,
      viewport,
    } = useVueFlow('flow-automator')
    return {
      screenToFlowCoordinate,
      fitView,
      setCenter,
      setViewport,
      findNode,
      viewport,
    }
  },
  data() {
    const saved = loadLayout()
    return {
      title: 'Flow Automator',
      view: 'flows',
      editing: saved.editing !== false,
      showPalette: saved.showPalette !== false,
      showInspector: saved.showInspector !== false,
      layout: LAYOUTS.includes(saved.layout) ? saved.layout : 'split',
      logHeight: Number(saved.logHeight) || 260,
      paletteFilter: '',
      rootHeight: null,
      showAudit: false,
      demoLoading: false,
      showHistory: false,
      showRunHistory: false,
      showAlerts: false,
      historyVersion: null,
      historyTab: 'versions',
      flowVersion: null,
      runDialog: { show: false },
      contactItems: [],
      conflict: { show: false, message: '' },
      noteDialog: { show: false, note: '' },
      routePrefix: ROUTE_PREFIX,
      nodeTypes: NODE_TYPES,
      flows: [],
      flowSearch: '',
      flowId: null,
      armed: false,
      flowSettings: blankSettings(),
      nodes: [],
      edges: [],
      dirty: false,
      saving: false,
      problems: [],
      selectedNodeId: null,
      runs: [],
      selectedRun: null,
      followLive: true,
      engineStatus: { armed: 0, active_runs: [], approvals: [], triggers: {} },
      seenApprovals: new Set(),
      modes: MODES,
      engineOnline: true,
      engineError: '',
      scripts: [],
      confirmStopAll: false,
      confirmDiscard: { show: false, action: null },
      defaultEdgeOptions: {
        type: 'smoothstep',
        markerEnd: 'arrowclosed',
        style: { strokeWidth: 2 },
      },
      flowContext: reactive({
        nodeStatus: {},
        nodeProblems: {},
        flowName: (id) => this.flows.find((f) => f.id === id)?.name,
      }),
      timers: [],
    }
  },
  computed: {
    menus() {
      return [
        {
          label: 'File',
          items: [
            { label: 'New Flow', icon: 'mdi-plus', command: this.newFlow },
            {
              label: 'Save',
              icon: 'mdi-content-save',
              command: this.save,
            },
            {
              label: 'Save With Note...',
              icon: 'mdi-content-save-edit',
              command: () => {
                this.noteDialog = { show: true, note: '' }
              },
            },
            {
              label: 'Save As Copy',
              icon: 'mdi-content-copy',
              command: this.saveCopy,
            },
            { divider: true },
            {
              label: 'Import JSON...',
              icon: 'mdi-import',
              command: () => this.$refs.importInput.click(),
            },
            {
              label: 'Export JSON',
              icon: 'mdi-export',
              command: this.exportFile,
            },
            { divider: true },
            {
              label: 'History...',
              icon: 'mdi-history',
              command: () => this.openHistory(),
            },
            {
              label: 'Restore Deleted Flow...',
              icon: 'mdi-delete-restore',
              command: () => {
                this.historyVersion = null
                this.historyTab = 'deleted'
                this.showHistory = true
              },
            },
            {
              label: 'Delete Flow',
              icon: 'mdi-delete',
              command: this.deleteFlow,
            },
            { divider: true },
            {
              label: 'Load Demo...',
              icon: 'mdi-presentation-play',
              command: this.loadDemo,
            },
            {
              label: 'Remove Demo',
              icon: 'mdi-presentation',
              command: this.clearDemo,
            },
          ],
        },
        {
          label: 'View',
          items: [
            {
              label: 'Fit to Screen',
              icon: 'mdi-fit-to-screen',
              command: () => this.fitView({ padding: 0.2 }),
            },
            {
              label: 'Run History',
              icon: 'mdi-format-list-bulleted-type',
              command: () => {
                this.showRunHistory = true
              },
            },
            {
              label: 'Alerts...',
              icon: 'mdi-bell-ring',
              command: () => {
                this.showAlerts = true
              },
            },
            {
              label: 'Audit Log',
              icon: 'mdi-clipboard-text-clock',
              command: () => {
                this.showAudit = true
              },
            },
            { divider: true },
            {
              label: 'Edit Mode',
              checkbox: true,
              checked: this.editing,
              command: () => this.setEditing(!this.editing),
            },
            {
              label: 'Show Node Palette',
              checkbox: true,
              checked: this.editing && this.showPalette,
              command: () => this.togglePanel('showPalette'),
            },
            {
              label: 'Show Settings Panel',
              checkbox: true,
              checked: this.editing && this.showInspector,
              command: () => this.togglePanel('showInspector'),
            },
            { divider: true },
            {
              label: 'Flow Only',
              icon: 'mdi-arrow-collapse-down',
              command: () => {
                this.layout = 'flow'
              },
            },
            {
              label: 'Flow and Log',
              icon: 'mdi-view-split-horizontal',
              command: () => {
                this.layout = 'split'
              },
            },
            {
              label: 'Log Only',
              icon: 'mdi-arrow-collapse-up',
              command: () => {
                this.layout = 'log'
              },
            },
            {
              label: 'All Running Flows',
              icon: 'mdi-run-fast',
              command: this.showAllRunning,
            },
            { divider: true },
            {
              label: 'Follow Live Runs',
              checkbox: true,
              checked: this.followLive,
              command: () => {
                this.followLive = !this.followLive
              },
            },
          ],
        },
      ]
    },
    contactStatus() {
      return this.engineStatus.contacts || {}
    },
    paletteCategories() {
      const filter = (this.paletteFilter || '').trim().toLowerCase()
      return CATEGORIES.map((category) => ({
        ...category,
        types: Object.fromEntries(
          Object.entries(NODE_TYPES).filter(
            ([, s]) =>
              s.category === category.key &&
              (!filter ||
                s.label.toLowerCase().includes(filter) ||
                (s.description || '').toLowerCase().includes(filter)),
          ),
        ),
      })).filter((c) => Object.keys(c.types).length)
    },
    layoutState() {
      return {
        editing: this.editing,
        showPalette: this.showPalette,
        showInspector: this.showInspector,
        layout: this.layout,
        logHeight: this.logHeight,
      }
    },
    selectedNode() {
      return this.nodes.find((n) => n.id === this.selectedNodeId) || null
    },
    triggerNodes() {
      return this.nodes.filter((n) => NODE_TYPES[n.data.kind]?.trigger)
    },
    manualTrigger() {
      const manual = this.triggerNodes.find(
        (n) => n.data.kind === 'trigger_manual',
      )
      return (manual || this.triggerNodes[0])?.id
    },
    usesContact() {
      return this.nodes.some((n) =>
        ['wait_contact', 'contact_check'].includes(n.data.kind),
      )
    },
    allApprovals() {
      return this.engineStatus.approvals || []
    },
    flowApprovals() {
      // Approvals of this flow's runs, including sub-flows they called
      const runs = this.engineStatus.active_runs
      const ids = new Set(
        runs.filter((r) => r.flow_id === this.flowId).map((r) => r.id),
      )
      let grew = true
      while (grew) {
        grew = false
        for (const r of runs) {
          if (r.parent_id && ids.has(r.parent_id) && !ids.has(r.id)) {
            ids.add(r.id)
            grew = true
          }
        }
      }
      return this.allApprovals.filter((a) => ids.has(a.run_id))
    },
    activeRunIds() {
      return this.engineStatus.active_runs
        .filter((r) => r.flow_id === this.flowId && !r.parent_id)
        .map((r) => r.id)
    },
  },
  watch: {
    selectedRun(run) {
      this.flowContext.nodeStatus = this.statusFromRun(run)
    },
    layoutState(state) {
      try {
        localStorage.setItem(LAYOUT_KEY, JSON.stringify(state))
      } catch {
        // Storage blocked: the layout just isn't remembered
      }
    },
  },
  created() {
    const query = this.$route?.query || {}
    if (query.view === 'contacts') this.view = 'contacts'
    // Links from COSMOS notifications and webhooks: ?flow=<id>&run=<id>
    if (query.flow) this.openRunFromHistory(query.flow, query.run)
    this.refreshFlows()
    this.pollStatus()
    this.timers.push(setInterval(this.pollStatus, STATUS_POLL_MS))
    this.timers.push(setInterval(this.pollRun, RUN_POLL_MS))
    flowApi
      .listScripts()
      .then((files) => {
        this.scripts = (files || [])
          .map((f) => (typeof f === 'string' ? f : f.name))
          .filter((f) => f && /\.(py|rb)$/.test(f))
      })
      .catch(() => {})
    window.addEventListener('beforeunload', this.beforeUnload)
  },
  mounted() {
    window.addEventListener('resize', this.fitHeight)
    this.fitHeight()
    // COSMOS's nav and banners can finish laying out after the tool mounts
    this.fitHeightTimer = setTimeout(this.fitHeight, 600)
  },
  beforeUnmount() {
    window.removeEventListener('resize', this.fitHeight)
    clearTimeout(this.fitHeightTimer)
    this.stopResize()
    this.timers.forEach(clearInterval)
    clearTimeout(this.validateTimer)
    window.removeEventListener('beforeunload', this.beforeUnload)
  },
  methods: {
    // ---- flows --------------------------------------------------------
    async refreshFlows() {
      try {
        this.flows = await flowApi.listFlows()
        this.engineOnline = true
      } catch (error) {
        this.engineOnline = false
        this.engineError = errorInfo(error).message
      }
    },
    guardUnsaved(action) {
      if (this.dirty) {
        this.confirmDiscard = { show: true, action }
      } else {
        action()
      }
    },
    discardAndContinue() {
      const action = this.confirmDiscard.action
      this.confirmDiscard = { show: false, action: null }
      this.dirty = false
      action()
    },
    newFlow() {
      this.guardUnsaved(() => {
        this.setEditing(true)
        this.loadFlow(null)
        this.addNode('trigger_manual', { x: 80, y: 120 })
        this.dirty = false
      })
    },
    openFlow(id) {
      this.flowSearch = ''
      if (id === this.flowId) return
      this.guardUnsaved(async () => {
        try {
          const { flow } = await flowApi.getFlow(id)
          this.loadFlow(flow)
        } catch (error) {
          this.$notify.serious({
            title: 'Could not open flow',
            body: errorInfo(error).message,
          })
        }
      })
    },
    loadFlow(flow) {
      this.flowId = flow ? flow.id : null
      this.flowVersion = flow && flow.id ? flow.version || null : null
      this.armed = !!(flow && flow.armed)
      this.flowSettings = flow
        ? {
            name: flow.name,
            description: flow.description || '',
            policy: flow.policy || 'skip',
            mode: flow.mode || 'run',
            approval_timeout: flow.approval_timeout || 0,
            variables: { ...(flow.variables || {}) },
            template: !!flow.template,
            parameters: JSON.parse(JSON.stringify(flow.parameters || [])),
            dry_run: !!flow.dry_run,
          }
        : blankSettings()
      this.nodes = (flow ? flow.nodes : []).map((n) => ({
        id: n.id,
        type: 'flow',
        position: n.position || { x: 0, y: 0 },
        data: { kind: n.type, config: n.data || {} },
      }))
      this.edges = (flow ? flow.edges : []).map((e) => this.makeEdge(e))
      this.selectedNodeId = null
      this.selectedRun = null
      this.runs = []
      this.dirty = false
      this.problems = []
      this.flowContext.nodeProblems = {}
      this.validateSoon()
      this.refreshRuns()
      this.fitPending = true
    },
    toEngine() {
      return {
        ...this.flowSettings,
        nodes: this.nodes.map((n) => ({
          id: n.id,
          type: n.data.kind,
          position: { x: n.position.x, y: n.position.y },
          data: n.data.config,
        })),
        edges: this.edges.map((e) => ({
          id: e.id,
          source: e.source,
          sourceHandle: e.sourceHandle || 'out',
          target: e.target,
        })),
      }
    },
    // Saves a new version. base_version makes the engine refuse (409) if someone saved in between.
    async save(options = {}) {
      this.saving = true
      try {
        const body = { ...this.toEngine(), note: options.note || '' }
        if (this.flowId) {
          body.base_version = this.flowVersion
          body.force = !!options.force
        }
        const result = this.flowId
          ? await flowApi.updateFlow(this.flowId, body)
          : await flowApi.createFlow(body)
        const wasArmed = this.armed
        this.flowId = result.flow.id
        this.flowVersion = result.flow.version
        this.armed = result.flow.armed
        this.dirty = false
        this.applyProblems(result.problems)
        if (wasArmed && !this.armed) {
          this.$notify.caution({
            title: 'Flow disarmed',
            body: 'The saved flow has problems, so it was disarmed.',
          })
        }
        await this.refreshFlows()
        return true
      } catch (error) {
        if (error?.response?.status === 409) {
          this.conflict = { show: true, message: errorInfo(error).message }
          return false
        }
        this.$notify.serious({
          title: 'Save failed',
          body: errorInfo(error).message,
        })
        return false
      } finally {
        this.saving = false
      }
    },
    saveWithNote() {
      const note = this.noteDialog.note
      this.noteDialog.show = false
      this.save({ note })
    },
    async overwrite() {
      this.conflict.show = false
      await this.save({ force: true, note: 'Saved over a newer version' })
    },
    async reloadTheirs() {
      this.conflict.show = false
      const { flow } = await flowApi.getFlow(this.flowId)
      this.dirty = false
      this.loadFlow(flow)
    },
    async loadDemo() {
      if (
        !window.confirm(
          "Load the demo? It adds 'Demo: ' flows, a rule, DEMOSAT satellites and demo stations, replacing any earlier demo. Your own flows, satellites and stations are left alone. It needs the COSMOS demo plugin (INST target).",
        )
      )
        return
      try {
        this.demoLoading = true
        const summary = await flowApi.loadDemo()
        await this.refreshFlows()
        const inSeconds = summary.first_pass?.aos_in
        this.$notify.normal({
          title: 'Demo loaded',
          body: `${summary.flows} flows, ${summary.rule_activities} scheduled downlinks. ${
            inSeconds
              ? `DEMOSAT-1 rises over the Demo Pass Site in ${Math.round(inSeconds / 60)} min; its downlink starts a minute before.`
              : ''
          }${summary.script_problem ? ` ${summary.script_problem}` : ''}`,
        })
        this.view = 'contacts'
      } catch (error) {
        this.$notify.serious({
          title: 'Demo not loaded',
          body: errorInfo(error).message,
        })
      } finally {
        this.demoLoading = false
      }
    },
    async clearDemo() {
      if (!window.confirm('Remove everything the demo added?')) return
      const removed = await flowApi.clearDemo()
      if (this.flowSettings.name?.startsWith('Demo: ')) this.loadFlow(null)
      await this.refreshFlows()
      this.$notify.normal({
        title: 'Demo removed',
        body: `${removed.flows} flows, ${removed.satellites} satellites, ${removed.stations} stations`,
      })
    },
    openHistory(version = null) {
      this.historyVersion = typeof version === 'number' ? version : null
      this.historyTab = 'versions'
      this.conflict.show = false
      this.showHistory = true
    },
    onRestored(flow) {
      this.dirty = false
      this.loadFlow(flow)
      this.refreshFlows()
      this.$notify.normal({
        title: `Restored as v${flow.version}`,
        body: `'${flow.name}' is open. Arm it again if it should run on its triggers.`,
      })
    },
    async saveCopy() {
      this.flowId = null
      this.flowVersion = null
      this.armed = false
      this.flowSettings.name = `${this.flowSettings.name} (copy)`
      await this.save()
    },
    async deleteFlow() {
      if (!this.flowId) return

      if (
        !window.confirm(
          `Delete flow '${this.flowSettings.name}'? You can restore it from File > Restore Deleted Flow.`,
        )
      )
        return
      await flowApi.deleteFlow(this.flowId)
      this.dirty = false
      this.loadFlow(null)
      this.refreshFlows()
    },
    exportFile() {
      const flow = this.toEngine()
      const blob = new Blob([JSON.stringify(flow, null, 2)], {
        type: 'application/json',
      })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `${flow.name.replace(/[^\w-]+/g, '_')}.json`
      link.click()
      URL.revokeObjectURL(link.href)
    },
    importFile(event) {
      const file = event.target.files[0]
      event.target.value = ''
      if (!file) return
      file.text().then((text) => {
        this.guardUnsaved(() => {
          try {
            const flow = JSON.parse(text)
            delete flow.id
            flow.armed = false
            this.setEditing(true)
            this.loadFlow(flow)
            this.dirty = true
          } catch (error) {
            this.$notify.serious({
              title: 'Import failed',
              body: String(error),
            })
          }
        })
      })
    },
    beforeUnload(event) {
      if (this.dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    },

    // ---- arming and running --------------------------------------------
    ensureSaved() {
      if (this.dirty || !this.flowId) {
        return this.save()
      }
      return Promise.resolve(true)
    },
    async setArmed(value) {
      if (value && !(await this.ensureSaved())) return
      try {
        const { flow } = value
          ? await flowApi.arm(this.flowId)
          : await flowApi.disarm(this.flowId)
        this.armed = flow.armed
        this.refreshFlows()
      } catch (error) {
        const info = errorInfo(error)
        this.armed = false
        this.applyProblems(info.problems.length ? info.problems : this.problems)
        this.$refs.runPanel.show('problems')
        this.$notify.caution({ title: 'Not armed', body: info.message })
      }
    },
    async runNow(triggerNode, variables = null, options = null) {
      if (!(await this.ensureSaved())) return
      const parameters = this.flowSettings.parameters || []
      if (
        options === null &&
        (parameters.length || this.flowSettings.template)
      ) {
        return this.openRunDialog(triggerNode)
      }
      try {
        const result = await flowApi.run(
          this.flowId,
          triggerNode,
          variables || {},
          options || {},
        )
        if (result.result === 'started') {
          this.selectRun(result.run.id)
        } else {
          this.$notify.normal({
            title: `Run ${result.result}`,
            body: result.message,
          })
        }
        this.refreshRuns()
      } catch (error) {
        const info = errorInfo(error)
        if (info.problems.length) {
          this.applyProblems(info.problems)
          this.$refs.runPanel.show('problems')
        }
        this.$notify.caution({ title: 'Run not started', body: info.message })
      }
    },
    async openRunDialog(triggerNode, preset = {}) {
      if (!(await this.ensureSaved())) return
      this.runDialog = {
        show: true,
        triggerNode,
        values: {},
        dry_run: this.flowSettings.dry_run || !!preset.dry_run,
        skip_waits: true,
        mode: this.flowSettings.mode || 'run',
        contactKind:
          this.flowSettings.template || this.usesContact ? 'simulated' : 'none',
        aos_in: 30,
        duration: 300,
        contact_id: null,
      }
      if (this.flowSettings.template || this.usesContact) {
        const now = Date.now() / 1000
        flowApi
          .schedule(Math.floor(now), Math.ceil(now + 86400))
          .then((s) => {
            this.contactItems = s.contacts
              .filter((c) => c.aos > now)
              .map((c) => ({
                value: c.id,
                title: `${c.satellite} / ${c.station} · AOS ${new Date(c.aos * 1000).toISOString().slice(11, 19)} UTC · ${Math.round(c.duration / 60)} min`,
              }))
          })
          .catch(() => {
            this.contactItems = []
          })
      }
    },
    runFromDialog() {
      const d = this.runDialog
      d.show = false
      const options = {
        mode: d.mode,
        dry_run: d.dry_run,
        skip_waits: d.dry_run && d.skip_waits,
      }
      if (d.contactKind === 'simulated') {
        options.contact = { aos_in: d.aos_in, duration: d.duration }
      } else if (d.contactKind === 'predicted' && d.contact_id) {
        options.contact = { contact_id: d.contact_id }
      }
      this.runNow(d.triggerNode, d.values, options)
    },
    openRunFromHistory(flowId, runId) {
      this.showRunHistory = false
      if (runId) this.showLog()
      this.openActivityRun(flowId, runId)
    },
    viewChanged(view) {
      if (view === 'contacts') this.refreshFlows()
    },
    openActivityRun(flowId, runId) {
      this.view = 'flows'
      if (flowId === this.flowId) {
        if (runId) this.selectRun(runId)
        return
      }
      this.guardUnsaved(async () => {
        try {
          const { flow } = await flowApi.getFlow(flowId)
          this.loadFlow(flow)
          if (runId) this.selectRun(runId)
        } catch (error) {
          this.$notify.caution({
            title: 'Could not open the template',
            body: errorInfo(error).message,
          })
        }
      })
    },
    async stopRun(id) {
      try {
        await flowApi.stopRun(id)
      } catch (error) {
        this.$notify.caution({
          title: 'Stop failed',
          body: errorInfo(error).message,
        })
      }
    },
    stopFlowRuns() {
      this.activeRunIds.forEach(this.stopRun)
    },
    async stopAll() {
      this.confirmStopAll = false
      const result = await flowApi.stopAll()
      this.armed = false
      this.$notify.caution({
        title: 'All flows stopped',
        body: `Disarmed ${result.disarmed} flows, stopped ${result.stopped} runs and paused the contact scheduler.`,
      })
      this.refreshFlows()
    },

    // ---- live status ----------------------------------------------------
    async pollStatus() {
      try {
        const status = await flowApi.status()
        this.engineStatus = status
        try {
          this.announceApprovals(status.approvals || [])
        } catch (error) {
          // A toast failing must not look like the engine going offline
          console.error(error)
        }
        // Someone else added or deleted a flow: keep the selector current
        if (status.flows !== this.flows.length) this.refreshFlows()
        this.engineOnline = true
        const active = status.active_runs.filter(
          (r) => r.flow_id === this.flowId && !r.parent_id,
        )
        const known = new Set(this.runs.map((r) => r.id))
        if (active.some((r) => !known.has(r.id))) {
          // A trigger started a run: show it
          await this.refreshRuns()
          if (
            this.followLive &&
            (!this.selectedRun || this.selectedRun.status !== 'running')
          ) {
            this.selectRun(active[active.length - 1].id)
          }
        } else if (this.runs.some((r) => r.status === 'running')) {
          await this.refreshRuns()
        }
      } catch (error) {
        this.engineOnline = false
        this.engineError = errorInfo(error).message
      }
    },
    async refreshRuns() {
      if (!this.flowId) {
        this.runs = []
        return
      }
      try {
        this.runs = await flowApi.listRuns(this.flowId, 50)
      } catch {
        // pollStatus reports the engine being down
      }
    },
    async selectRun(id) {
      try {
        this.selectedRun = await flowApi.getRun(id)
        this.$refs.runPanel.show('steps')
      } catch (error) {
        this.$notify.caution({
          title: 'Run not found',
          body: errorInfo(error).message,
        })
      }
    },
    async pollRun() {
      const run = this.selectedRun
      if (!run || run.status !== 'running' || this.pollingRun) return
      this.pollingRun = true
      try {
        const updated = await flowApi.getRun(run.id)
        if (this.selectedRun && this.selectedRun.id === updated.id) {
          this.selectedRun = updated
        }
      } catch {
        // Engine restarted or run expired from history
      } finally {
        this.pollingRun = false
      }
    },
    statusFromRun(run) {
      const status = {}
      if (!run || run.flow_id !== this.flowId) return status
      for (const step of run.steps || []) {
        status[step.node] = step.status
      }
      for (const id of run.active_nodes || []) {
        status[id] = 'running'
      }
      for (const approval of run.approvals || []) {
        status[approval.node] = 'waiting'
      }
      return status
    },
    announceApprovals(approvals) {
      // Toast each new approval once, wherever it is, so operators notice
      // runs that triggers started in other flows
      // Skip the ones already on screen as cards: the flow that's open, or all of them in Contacts
      const onScreen = new Set(
        (this.view === 'contacts' ? approvals : this.flowApprovals).map(
          (a) => a.id,
        ),
      )
      for (const a of approvals) {
        if (this.seenApprovals.has(a.id)) continue
        this.seenApprovals.add(a.id)
        if (onScreen.has(a.id)) continue
        const level = a.reason.includes('HAZARDOUS') ? 'serious' : 'caution'
        this.$notify[level]({
          title: `Approval needed: ${a.flow_name}`,
          body: `${a.label}: ${a.preview}`,
        })
      }
    },
    openApproval(approval) {
      if (approval.flow_id === this.flowId) return
      const run = this.engineStatus.active_runs.find(
        (r) => r.id === approval.run_id,
      )
      // A sub-flow's approval is answered from the flow that called it
      let flowId = approval.flow_id
      let top = run
      while (top && top.parent_id) {
        top = this.engineStatus.active_runs.find((r) => r.id === top.parent_id)
        if (top) flowId = top.flow_id
      }
      this.openFlow(flowId)
    },

    // ---- validation -----------------------------------------------------
    validateSoon() {
      clearTimeout(this.validateTimer)
      this.validateTimer = setTimeout(async () => {
        try {
          const { problems } = await flowApi.validate(this.toEngine())
          this.applyProblems(problems)
        } catch {
          // Engine offline: shown in the toolbar
        }
      }, 400)
    },
    applyProblems(problems) {
      this.problems = problems || []
      const byNode = {}
      for (const p of this.problems) {
        if (p.node) (byNode[p.node] = byNode[p.node] || []).push(p.message)
      }
      this.flowContext.nodeProblems = byNode
    },

    // ---- layout ---------------------------------------------------------
    // Fill the window down to the COSMOS footer. A fixed calc(100vh - N) is wrong whenever the
    // header, banners or footer differ, and pushed the log's bottom bar under the footer
    fitHeight() {
      if (!this.$el?.getBoundingClientRect) return
      const top = this.$el.getBoundingClientRect().top + window.scrollY
      const footer = document.getElementById('footer')
      const footerHeight = footer ? footer.getBoundingClientRect().height : 0
      this.rootHeight = Math.max(
        420,
        Math.floor(window.innerHeight - top - footerHeight - 8),
      )
    },
    // View mode hides the palette and settings and locks the canvas; Edit brings back whichever
    // side panels were open (both, if both had been folded away)
    setEditing(value) {
      this.editing = value
      if (value && !this.showPalette && !this.showInspector) {
        this.showPalette = true
        this.showInspector = true
      }
    },
    togglePanel(key) {
      if (!this.editing) {
        this.editing = true
        this[key] = true
      } else {
        this[key] = !this[key]
      }
    },
    showLog(tab) {
      if (this.layout === 'flow') this.layout = 'split'
      if (tab) this.$refs.runPanel.show(tab)
    },
    showAllRunning() {
      this.view = 'flows'
      this.showLog('running')
    },
    openRunFromList(flowId, runId) {
      this.showLog()
      this.openActivityRun(flowId, runId)
    },
    startResize(event) {
      const center = this.$refs.center
      if (!center) return
      event.preventDefault()
      const startY = event.clientY
      const startHeight = this.logHeight
      const max = center.clientHeight - 190 // keep the flow at its 180px minimum
      this.resizeMove = (e) => {
        const height = startHeight - (e.clientY - startY)
        this.logHeight = Math.round(Math.max(90, Math.min(max, height)))
      }
      this.resizeEnd = () => this.stopResize()
      window.addEventListener('pointermove', this.resizeMove)
      window.addEventListener('pointerup', this.resizeEnd)
      document.body.style.cursor = 'row-resize'
    },
    stopResize() {
      if (!this.resizeMove) return
      window.removeEventListener('pointermove', this.resizeMove)
      window.removeEventListener('pointerup', this.resizeEnd)
      this.resizeMove = null
      document.body.style.cursor = ''
    },

    // ---- editing --------------------------------------------------------
    onDragStart(event, type) {
      event.dataTransfer.setData('application/x-flow-node', type)
      event.dataTransfer.effectAllowed = 'move'
    },
    onDrop(event) {
      const type = event.dataTransfer.getData('application/x-flow-node')
      if (!type || !this.editing) return
      event.preventDefault()
      const position = this.screenToFlowCoordinate({
        x: event.clientX,
        y: event.clientY,
      })
      this.addNode(type, { x: position.x - 115, y: position.y - 20 })
    },
    addNode(type, position) {
      const placed = !position
      if (placed) position = this.freeSpotNear(this.selectedNode)
      const id = `${type}_${Math.random().toString(36).slice(2, 8)}`
      this.nodes = [
        ...this.nodes,
        {
          id,
          type: 'flow',
          position,
          data: { kind: type, config: defaultData(type) },
        },
      ]
      this.selectedNodeId = id
      this.changed()
      if (placed && this.nodes.length > 1) {
        // Bring the new node into view at the current zoom
        this.$nextTick(() =>
          this.setCenter(position.x + 115, position.y + 40, {
            zoom: this.viewport?.zoom || 1,
            duration: 200,
          }),
        )
      }
    },
    // Where a double-clicked node goes: right of the selected (or last) node, moving down past
    // any node already there, so new nodes never land on top of each other
    freeSpotNear(anchor) {
      const near = anchor || this.nodes[this.nodes.length - 1]
      const spot = near
        ? { x: near.position.x + 280, y: near.position.y }
        : { x: 200, y: 100 }
      const taken = (p) =>
        this.nodes.some(
          (n) =>
            Math.abs(n.position.x - p.x) < 240 &&
            Math.abs(n.position.y - p.y) < 110,
        )
      for (let i = 0; i < 50 && taken(spot); i++) spot.y += 120
      return spot
    },
    makeEdge(edge) {
      const handle = edge.sourceHandle || 'out'
      return {
        id: edge.id || `e_${Math.random().toString(36).slice(2, 10)}`,
        source: edge.source,
        target: edge.target,
        sourceHandle: handle,
        targetHandle: 'in',
        type: 'smoothstep',
        markerEnd: 'arrowclosed',
        class: handle === 'error' ? 'fa-edge-error' : '',
        animated: handle === 'error',
      }
    },
    isValidConnection(connection) {
      return (
        connection.source !== connection.target ||
        // A loop node may wire its body straight back into itself
        this.findNode(connection.source)?.data.kind === 'loop'
      )
    },
    onConnect(connection) {
      const source = this.nodes.find((n) => n.id === connection.source)
      const handle = connection.sourceHandle || 'out'
      // One wire per output, except Parallel which branches on every wire:
      // connecting again replaces the old wire
      let edges = this.edges
      if (source && source.data.kind !== 'parallel') {
        edges = edges.filter(
          (e) =>
            !(
              e.source === connection.source &&
              (e.sourceHandle || 'out') === handle
            ),
        )
      }
      this.edges = [...edges, this.makeEdge(connection)]
      this.changed()
    },
    onNodesChange(changes) {
      if (changes.some((c) => c.type === 'remove')) {
        if (changes.some((c) => c.id === this.selectedNodeId)) {
          this.selectedNodeId = null
        }
        this.changed()
      }
    },
    onEdgesChange(changes) {
      if (changes.some((c) => c.type === 'remove')) this.changed()
    },
    onInspectorChange() {
      // Switch cases change a node's outputs: drop wires from outputs that are gone
      const node = this.selectedNode
      if (node) {
        const outputs = outputsFor(node.data.kind, node.data.config)
        this.edges = this.edges.filter(
          (e) =>
            e.source !== node.id || outputs.includes(e.sourceHandle || 'out'),
        )
      }
      this.changed()
    },
    deleteSelected() {
      const id = this.selectedNodeId
      this.nodes = this.nodes.filter((n) => n.id !== id)
      this.edges = this.edges.filter((e) => e.source !== id && e.target !== id)
      this.selectedNodeId = null
      this.changed()
    },
    changed() {
      this.dirty = true
      this.validateSoon()
    },
    nodeLabel(id) {
      const node = this.nodes.find((n) => n.id === id)
      if (!node) return id
      return node.data.config.label || NODE_TYPES[node.data.kind]?.label || id
    },
    focusNode(id) {
      // From the log-only layout, bring the flow back so the node can be seen
      if (this.layout === 'log') {
        this.layout = 'split'
        this.$nextTick(() => this.focusNode(id))
        return
      }
      const node = this.findNode(id)
      if (!node) return
      this.selectedNodeId = id
      this.setCenter(node.position.x + 115, node.position.y + 40, {
        zoom: 1.2,
        duration: 300,
      })
    },
    onNodesInitialized() {
      if (this.fitPending) {
        this.fitPending = false
        this.readableFit()
      }
    },
    // Fit the flow, but don't zoom out so far that node text can't be read: a wide flow opens at
    // a readable zoom showing its start (the left), and View > Fit to Screen shows all of it
    async readableFit() {
      await this.fitView({ padding: 0.15, maxZoom: 1 })
      if (!this.nodes.length || (this.viewport?.zoom || 1) >= READABLE_ZOOM) {
        return
      }
      const canvas = this.$el.querySelector('.fa-canvas')
      if (!canvas) return
      const xs = this.nodes.map((n) => n.position.x)
      const ys = this.nodes.map((n) => n.position.y)
      const zoom = 0.8
      const midY = (Math.min(...ys) + Math.max(...ys) + 90) / 2
      this.setViewport({
        x: 40 - Math.min(...xs) * zoom,
        y: canvas.clientHeight / 2 - midY * zoom,
        zoom,
      })
    },
    miniMapColor(node) {
      return categoryColor(node.data?.kind)
    },
  },
}
</script>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';

.fa-root {
  display: flex;
  flex-direction: column;
  /* Until fitHeight() measures the real space */
  height: calc(100vh - 118px);
}
.fa-toolbar {
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
/* Each row wraps on narrow screens instead of clipping buttons */
.fa-toolbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  row-gap: 6px;
  padding: 6px 8px;
}
.fa-run-row {
  padding-top: 0;
}
.fa-toolbar-row > .v-divider--vertical {
  align-self: stretch;
}
.fa-group-label {
  margin-right: 6px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: #a9b8c6;
}
.fa-status-text {
  color: #c9d4de;
}
/* The open flow's name is the placeholder: show it as normal text, not faded */
.fa-flow-open input::placeholder {
  color: #e6edf3;
  opacity: 1;
  font-weight: 600;
}
.fa-flow-select {
  flex: 0 1 320px;
  min-width: 240px;
}
.fa-toolbar-end {
  margin-left: auto;
  display: flex;
  align-items: center;
}
/* Make the chosen option of every toggle obvious */
.fa-toolbar .v-btn-group .v-btn.v-btn--active {
  background: rgba(77, 172, 255, 0.3);
  color: #fff;
  font-weight: 700;
}
.fa-toolbar .v-btn-group .v-btn.v-btn--active .v-btn__overlay {
  opacity: 0;
}
.fa-engine-chip {
  cursor: pointer;
}
.fa-panel-head {
  display: flex;
  align-items: center;
  padding: 6px 4px 6px 10px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #e6edf3;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-rail {
  flex: 0 0 30px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 8px;
  cursor: pointer;
  color: #c9d4de;
  background: rgba(255, 255, 255, 0.03);
  user-select: none;
}
.fa-rail:hover {
  background: rgba(255, 255, 255, 0.09);
  color: #fff;
}
.fa-rail-left {
  border-right: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-rail-right {
  border-left: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-rail-label {
  writing-mode: vertical-rl;
  margin-top: 8px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.fa-rail-left .fa-rail-label {
  transform: rotate(180deg);
}
.fa-hint {
  color: #a9b8c6;
}
.fa-view-badge {
  position: absolute;
  left: 12px;
  top: 10px;
  z-index: 5;
  display: flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  color: #dbe5ee;
  background: rgba(31, 47, 64, 0.92);
  border: 1px solid #44515e;
  pointer-events: none;
}
.fa-splitter {
  flex: 0 0 6px;
  cursor: row-resize;
  background: rgba(255, 255, 255, 0.06);
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-splitter:hover {
  background: rgba(77, 172, 255, 0.5);
}
.fa-main {
  flex: 1;
  display: flex;
  min-height: 0;
}
.fa-contacts-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
}
.fa-palette {
  flex: 0 0 200px;
  overflow-y: auto;
  border-right: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-palette-top {
  position: sticky;
  top: 0;
  z-index: 2;
  padding-bottom: 6px;
  background: rgb(var(--v-theme-surface));
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.fa-palette-filter {
  margin-top: 6px;
}
.fa-category {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  filter: brightness(1.25);
}
.fa-palette-item {
  display: flex;
  align-items: center;
  margin: 2px 8px;
  padding: 5px 8px;
  font-size: 13px;
  line-height: 1.3;
  color: #e6edf3;
  border-radius: 4px;
  border-left: 3px solid var(--fa-color);
  background: rgba(255, 255, 255, 0.04);
  cursor: grab;
  user-select: none;
}
.fa-palette-item:hover {
  background: rgba(255, 255, 255, 0.1);
}
.fa-center {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
}
.fa-canvas {
  flex: 1;
  /* The log shrinks before the flow goes below this (a remembered log height can be too tall
     for a smaller window) */
  min-height: 180px;
  position: relative;
}
.fa-flow {
  height: 100%;
  width: 100%;
  background-color: rgb(23, 38, 53);
}
.fa-flow .vue-flow__edge-path {
  stroke: #8fa3b5;
}
.fa-flow .vue-flow__edge.selected .vue-flow__edge-path {
  stroke: #4dacff;
}
.fa-flow .fa-edge-error .vue-flow__edge-path {
  stroke: #e57373;
}
.fa-flow .vue-flow__node-flow {
  padding: 0;
  border: none;
  background: transparent;
}
.fa-flow .vue-flow__minimap {
  background: rgb(31, 47, 64);
}
/* On a small canvas the minimap would cover the flow: hide it (View > Fit to Screen still works) */
.fa-canvas {
  container-type: size;
}
@container (max-height: 360px) or (max-width: 700px) {
  .fa-flow .vue-flow__minimap {
    display: none;
  }
}
.fa-flow .vue-flow__minimap svg {
  width: 160px;
  height: 100px;
}
.fa-flow .vue-flow__controls-button {
  background: #2c3e50;
  border-bottom-color: #3a4a5a;
  fill: #e6edf3;
}
.fa-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #6b7f92;
  pointer-events: none;
}
.fa-bottom {
  flex: 0 1 auto;
  min-height: 0;
}
.fa-bottom-split {
  min-height: 110px;
}
.fa-bottom-flow {
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}
.fa-bottom-log {
  flex: 1 1 0;
}
.fa-approvals.fa-approvals-inline {
  position: static;
  transform: none;
  flex: 0 0 auto;
  width: min(560px, 96%);
  max-height: 45%;
  margin: 8px auto 0;
}
.fa-side {
  flex: 0 0 340px;
  overflow-y: auto;
  border-left: 1px solid rgba(255, 255, 255, 0.12);
}
/* Laptop widths: slimmer side panels so the flow keeps room */
@media (max-width: 1500px) {
  .fa-palette {
    flex-basis: 170px;
  }
  .fa-side {
    flex-basis: 290px;
  }
}
</style>
