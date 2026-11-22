<script setup>
import { computed, onBeforeUnmount, provide, watch } from 'vue'
import { reducedMotion, skeletonHandoff } from '../styles/chartMotion.mjs'

const props = defineProps({
  active: Boolean,
  label: { type: String, default: '正在读取数据…' },
  layout: { type: String, required: true },
  cards: { type: Number, default: 4 },
  singleDay: Boolean,
  scoped: Boolean,
  heatmap: Boolean,
})

const panel = (kind, height, style = 'chart-panel', count = 1) => ({ kind, height, style, count })
const groups = computed(() => {
  const { layout, singleDay, scoped, cards } = props
  if (layout === 'outpatient' || layout === 'inpatient') {
    const inpatient = layout === 'inpatient'
    const standard = inpatient ? 'inpatient-panel' : 'skeleton-analysis-card'
    const main = inpatient ? 'inpatient-main' : 'outpatient-grid outpatient-main'
    const lower = inpatient ? 'inpatient-secondary' : 'outpatient-grid outpatient-secondary'
    return [
      [`${main} ${singleDay && scoped ? inpatient ? 'inpatient-single' : 'outpatient-grid-single' : ''}`, [
        panel('summary', singleDay ? 228 : 340, standard),
        ...(!singleDay ? [panel('trend', 340, standard)] : scoped ? [] : [panel('rank', 246, standard)]),
      ]],
      ...(!singleDay && (!scoped || !inpatient) ? [[`${lower} ${scoped ? 'outpatient-grid-single' : ''}`, [
        ...(!scoped ? [panel('ring', 294, standard)] : []), panel('rank', 294, standard),
      ]]] : []),
      ['skeleton-grid', [panel('table', 238, standard)]],
    ]
  }
  if (layout === 'revenue') {
    return [
      ['panel skeleton-revenue-kpis', [panel('kpi', 60, 'skeleton-insight', cards)]],
      ['skeleton-revenue-charts', [panel(singleDay ? 'rank' : 'trend', 332), panel('rank', 332)]],
      ['panel skeleton-revenue-finance', [panel('kpi', 38, 'skeleton-insight', 2)]],
      ['', [panel('table', 230)]],
    ]
  }
  if (layout === 'disease') {
    return [
      ['skeleton-disease-charts', [panel('rank', 270, 'chart-panel skeleton-disease-rank'), panel('kpi', 106), panel('list', 140)]],
      ['', [panel('table', 239)]],
    ]
  }
  if (layout === 'resource') {
    return [
      ['skeleton-resource-main', [panel('table', 262), panel('summary', 262)]],
      ['skeleton-resource-ward', [...(scoped ? [] : [panel('rank', 282)]), panel('table', 282)]],
    ]
  }
  return {
    overview: [
      ['overview-kpis', [panel('kpi', 119, 'overview-kpi', 4)]],
      ['overview-middle', [panel('summary', 307, 'overview-panel'), panel('trend', 307, 'overview-panel')]],
      ['overview-bottom', [panel('ring', 174, 'overview-panel'), panel('rank', 174, 'overview-panel', 2)]],
    ],
    metric: [['', [panel('kpi', 104, 'kpi-card')]], ['business-section', [panel('trend', 350)]]],
    multidimensional: [['panel skeleton-cube-report', [panel('kpi', 58, 'skeleton-insight', cards)]], ['business-section', [panel('table', 335)]]],
    report: [
      ['report-result', [panel('table', 420, 'filter-panel')]],
      ['charts-grid report-result', [panel('trend', 350), panel('ring', 338)]],
      ...(props.heatmap ? [['report-result', [panel('heatmap', 320)]]] : []),
    ],
    paper: [['business-section', [panel('paper', 390, 'report-paper')]]],
    chat: [['', [panel('chat', 52, 'chat-answer')]]],
  }[layout] ?? []
})

const exitDuration = 140
let fade

provide(skeletonHandoff, () => fade?.playState === 'running' ? Math.max(0, exitDuration - (fade.currentTime ?? 0)) : 0)

function leave(element, done) {
  if (reducedMotion.value) {
    done()
    return
  }
  element.classList.add('dashboard-skeleton-leaving')
  fade = element.animate([{ opacity: 1 }, { opacity: 0 }], { duration: exitDuration, easing: 'ease-out', fill: 'forwards' })
  fade.finished.then(done, done)
}

watch(reducedMotion, reduce => { if (reduce) fade?.finish() })
onBeforeUnmount(() => fade?.cancel())

const widths = [88, 67, 75, 52, 63, 43]
</script>

<template>
  <div class="dashboard-stage" :class="{ 'dashboard-stage-chat': layout === 'chat' }">
    <span class="report-visually-hidden" role="status">{{ active ? label : '' }}</span>
    <Transition :css="false" @leave="leave">
      <div v-if="active" class="dashboard-skeleton" :data-skeleton="layout" aria-hidden="true">
        <div v-for="([grid, panels], index) in groups" :key="index" :class="grid">
          <template v-for="(item, block) in panels" :key="block">
            <div v-for="number in item.count" :key="number" class="skeleton-panel" :class="[
              item.style, 'skeleton-' + item.kind,
              { panel: !['skeleton-insight', 'chat-answer'].includes(item.style) },
            ]" :style="{ '--skeleton-height': item.height + 'px' }">
              <i class="skeleton-shape skeleton-title"></i>
              <i v-if="item.kind === 'kpi'" class="skeleton-shape skeleton-value"></i>
              <div v-else-if="item.kind === 'trend'" class="skeleton-gridlines"><i class="skeleton-shape skeleton-series"></i></div>
              <div v-else-if="item.kind === 'ring'" class="skeleton-ring-grid">
                <i class="skeleton-shape skeleton-circle"></i>
                <div class="skeleton-lines"><i v-for="width in widths.slice(1, 4)" :key="width" class="skeleton-shape" :style="{ width: width + '%' }"></i></div>
              </div>
              <div v-else-if="item.kind === 'table'" class="skeleton-table-rows">
                <div v-for="row in 6" :key="row"><i v-for="column in 4" :key="column" class="skeleton-shape"></i></div>
              </div>
              <i v-else-if="item.kind === 'heatmap'" class="skeleton-shape"></i>
              <div v-else class="skeleton-lines">
                <i v-if="item.kind === 'summary'" class="skeleton-shape skeleton-total"></i>
                <i v-for="width in (item.kind === 'chat' ? widths.slice(1, 2) : item.kind === 'list' ? widths.slice(0, 3) : widths)"
                  :key="width" class="skeleton-shape" :style="{ width: width + '%' }"></i>
                <div v-if="item.kind === 'paper'" class="skeleton-gridlines"><i class="skeleton-shape skeleton-series"></i></div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </Transition>
    <div v-show="!active" class="dashboard-stage-content" :aria-busy="active"><slot /></div>
  </div>
</template>
