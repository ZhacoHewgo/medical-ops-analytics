<script setup>
import { provideChartMotion, reducedMotion } from '../styles/chartMotion.mjs'
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, shallowRef } from 'vue'
import { formatMetric, isMetricValue, useAnalysisRequest } from '../api/analysis.mjs'
import { departmentChartColor, outpatientDepartmentName } from '../api/departmentLabels.mjs'
import { reportNarrative, reportPeriod } from '../api/report.mjs'
import { useSession } from '../api/session.mjs'
import DashboardLoading from '../components/DashboardLoading.vue'
import DashboardChart from '../components/DashboardChart.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'
import WorkspaceIcon from '../components/WorkspaceIcon.vue'
import { chartTheme } from '../styles/theme.js'

defineOptions({ name: 'ReportGeneratorView' })

const department = useSession().current?.user.department ?? null
const chartMotionScope = provideChartMotion(() => Boolean(result.value))
const selection = reactive({ type: 'monthly', month: '2025-06', week: '2025-W26', dept_code: department || '' })
const { busy, error, run } = useAnalysisRequest()
const result = shallowRef(null)
const article = ref(null)
const exporting = ref(false)
const printed = ref(0)
const exportController = new AbortController()
const theme = chartTheme
const names = { monthly: '综合运营月报', weekly: '综合运营周报' }
const count = value => formatMetric(value, '')
const selectedPeriod = computed(() => reportPeriod(selection.type, selection.type === 'monthly' ? selection.month : selection.week))
const withDepartmentNames = rows => rows.map(row => ({
  ...row, dept_label: outpatientDepartmentName(row.dept_code), dept_color: departmentChartColor(row.dept_code),
}))
const rankedDepartments = computed(() => withDepartmentNames(result.value?.ranking.filter(row => row.visits > 0) ?? []))
const compareDischarges = computed(() => result.value?.ranking.some(row => row.discharges != null))
const compareCharges = computed(() => result.value?.ranking.some(row => row.charge_amount != null))
const departmentChanges = computed(() => withDepartmentNames(result.value?.anomaly.top_changes ?? []))
const chapters = computed(() => {
  let end = 0
  return (result.value ? reportNarrative(result.value) : []).map(chapter => {
    const start = end
    end += chapter.text.length
    return { ...chapter, start, end }
  })
})
const length = computed(() => chapters.value.at(-1)?.end ?? 0)
const typing = computed(() => printed.value < length.value)
let frame
let active = true

function showFullReport() {
  cancelAnimationFrame(frame)
  printed.value = length.value
}

function reveal() {
  if (!active || !typing.value) return
  if (reducedMotion.value) return showFullReport()
  const initial = printed.value
  const remaining = length.value - initial
  const duration = Math.min(1100, remaining * 12)
  const start = performance.now()
  function print(now) {
    if (reducedMotion.value) return showFullReport()
    const count = Math.max(1, Math.floor(remaining * (now - start) / duration))
    printed.value = initial + Math.min(count, remaining)
    if (typing.value) frame = requestAnimationFrame(print)
  }
  frame = requestAnimationFrame(print)
}

onActivated(() => { active = true; reveal() })
onDeactivated(() => { active = false; cancelAnimationFrame(frame) })
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  exportController.abort()
})

async function showChapter(id) {
  if (typing.value) {
    showFullReport()
    await nextTick()
  }
  const chapter = article.value?.querySelector('#report-chapter-' + id)
  chapter?.focus({ preventScroll: true })
  chapter?.scrollIntoView({ block: 'start' })
}

async function download() {
  if (exporting.value || !result.value) return
  exporting.value = true
  error.value = ''
  showFullReport()
  const { type, period } = result.value
  try {
    await nextTick()
    const { downloadReportPdf } = await import('../api/reportPdf.mjs')
    await new Promise(resolve => requestAnimationFrame(resolve))
    await nextTick()
    await downloadReportPdf(article.value, `${names[type]}-${period.start_date}-${period.end_date}.pdf`, exportController.signal)
  } catch {
    if (!exportController.signal.aborted) error.value = 'PDF 下载失败，请重试。'
  } finally {
    exporting.value = false
  }
}

function readReport(data, type, scope, period) {
  if (data.type !== type || data.scope?.department !== scope
      || data.period?.start_date !== period.start_date || data.period.end_date !== period.end_date
      || !Array.isArray(data.sections)) throw new Error('Invalid report')
  const sections = new Map(data.sections.map(section => [section.id, section.data]))
  const overview = sections.get('overview')
  const trend = sections.get('trend')
  const ranking = sections.get('department_comparison')
  const anomaly = sections.get('anomalies_and_actions')
  if (!Number.isSafeInteger(overview?.outpatient_visits) || !Number.isSafeInteger(overview.admissions)
      || !Number.isSafeInteger(overview.discharges)
      || ['avg_stay_days', 'charge_amount', 'outpatient_charge', 'inpatient_charge', 'drug_share', 'consumable_share']
        .some(key => overview[key] != null && !isMetricValue(overview[key]))
      || !Array.isArray(trend) || trend.some(row => typeof row.date !== 'string' || !Number.isSafeInteger(row.visits))
      || !Array.isArray(ranking) || ranking.some(row => typeof row.dept_code !== 'string' || !Number.isSafeInteger(row.visits)
        || (scope && row.dept_code !== scope) || !Number.isSafeInteger(row.discharges)
        || (row.charge_amount != null && !isMetricValue(row.charge_amount)))
      || !['PRIOR_UNAVAILABLE', 'AVAILABLE'].includes(anomaly?.status)
      || !Array.isArray(anomaly.top_changes)
      || anomaly.top_changes.some(row => typeof row.dept_code !== 'string'
        || ![row.current, row.previous, row.difference].every(Number.isSafeInteger))) {
    throw new Error('Invalid report')
  }
  return { type, scope, period, overview, trend, ranking, anomaly }
}

async function generate() {
  if (busy.value || exporting.value) return
  const { type, month, week, dept_code } = selection
  const scope = department || dept_code || null
  cancelAnimationFrame(frame)
  printed.value = 0
  result.value = null
  const period = reportPeriod(type, type === 'weekly' ? week : month)
  if (!period) {
    error.value = '请选择有效的报告期间。'
    return
  }
  const report = await run('/api/llm/report', {
    body: { type, mode: 'operations', dept_code: scope, ...(type === 'weekly' ? { week } : { month }) },
    waitForPreload: false,
    read: data => readReport(data, type, scope, period),
    invalid: '请核对报表期间及科室。',
  })
  if (report) {
    chartMotionScope.commit()
    result.value = report
    reveal()
  }
}
</script>

<template>
  <main class="workspace section-page report-generator">
    <WorkspaceHeading><h1>报告生成</h1></WorkspaceHeading>
    <section class="panel filter-panel generator-controls" aria-label="报告设置">
      <form @submit.prevent="generate">
        <fieldset class="generator-types" :disabled="busy || exporting">
          <legend class="report-visually-hidden">报告类型</legend>
          <label v-for="(label, type) in names" :key="type">
            <input v-model="selection.type" type="radio" name="report-type" :value="type" class="report-visually-hidden"><span>{{ label }}</span>
          </label>
        </fieldset>
        <div class="generator-fields">
          <div v-if="selection.type === 'monthly'" class="field">
            <label for="generate-month">月份</label><input id="generate-month" v-model="selection.month" type="month"
              required pattern="[0-9]{4}-[0-9]{2}" :disabled="busy || exporting">
          </div>
          <div v-else class="field">
            <label for="generate-week">周次</label><input id="generate-week" v-model="selection.week" type="week"
              required pattern="[0-9]{4}-W[0-9]{2}" :disabled="busy || exporting">
          </div>
          <div class="field"><label for="generate-department">科室代码</label><input id="generate-department" v-model.trim="selection.dept_code"
            maxlength="64" placeholder="全院" :readonly="Boolean(department)" :disabled="busy || exporting"></div>
          <button class="button primary" type="submit" :disabled="busy || exporting">生成报告</button>
          <span v-if="selectedPeriod && selection.type === 'weekly'" class="generator-period">{{ selectedPeriod.start_date }} 至 {{ selectedPeriod.end_date }}</span>
        </div>
      </form>
    </section>
    <DashboardLoading :active="busy" layout="paper" label="正在生成报告…">
      <p v-if="error" class="report-error business-section" role="alert">{{ error }}</p>
      <section v-if="!result && !error" class="panel generator-empty" aria-label="报告预览">
        <WorkspaceIcon name="file" /><h2>报告预览</h2><span>待生成</span>
      </section>
      <div v-if="result" class="generator-preview">
        <nav class="panel generator-index" aria-label="报告目录">
          <h2>目录</h2>
          <button v-for="chapter in chapters" :key="chapter.id" type="button" @click="showChapter(chapter.id)">{{ chapter.title }}</button>
        </nav>
        <article ref="article" class="panel report-paper generator-paper" aria-label="报告正文">
          <header class="report-paper-header">
            <div>
              <h2>{{ names[result.type] }}</h2>
              <p>{{ result.period.start_date }} 至 {{ result.period.end_date }} · {{ result.scope ? outpatientDepartmentName(result.scope) : '全院' }}</p>
            </div>
            <div class="report-actions">
              <button v-if="typing" type="button" class="button secondary" @click="showFullReport">显示全文</button>
              <button type="button" class="button secondary" :disabled="exporting" @click="download">
                {{ exporting ? '正在生成 PDF…' : '下载 PDF' }}
              </button>
            </div>
          </header>
          <section v-for="chapter in chapters.filter(item => printed > item.start)" :id="'report-chapter-' + chapter.id"
            :key="chapter.id" class="report-chapter" tabindex="-1">
            <h3>{{ chapter.title }}</h3>
            <p class="report-narrative">
              <span v-if="printed < chapter.end" aria-hidden="true">{{ chapter.text.slice(0, printed - chapter.start) }}<span class="report-cursor"></span></span>
              <span :class="{ 'report-visually-hidden': printed < chapter.end }">{{ chapter.text }}</span>
            </p>
            <figure v-if="printed >= chapter.end && chapter.id === 'trend' && result.trend.some(row => row.visits)">
              <figcaption>每日门诊人次</figcaption>
              <DashboardChart kind="trend" :rows="result.trend" :theme="theme" label="门诊人次" unit="人次" period="日" snapshot :exporting="exporting" />
            </figure>
            <template v-else-if="printed >= chapter.end && chapter.id === 'ranking'">
              <figure v-if="rankedDepartments.length">
                <figcaption>各科室门诊人次</figcaption>
                <DashboardChart kind="ranking" :rows="rankedDepartments" :theme="theme" label="门诊人次"
                  unit="人次" category="科室" compact-ranking snapshot :exporting="exporting" />
              </figure>
              <div class="table-scroll generator-departments" tabindex="0" aria-label="科室运营对比">
                <table>
                  <thead><tr><th scope="col">科室</th><th scope="col">门诊人次</th>
                    <th v-if="compareDischarges" scope="col">出院人次</th><th v-if="compareCharges" scope="col">收费金额（元）</th>
                  </tr></thead>
                  <tbody><tr v-for="row in result.ranking" :key="row.dept_code">
                    <th scope="row">{{ outpatientDepartmentName(row.dept_code) }}</th><td>{{ count(row.visits) }}</td>
                    <td v-if="compareDischarges">{{ row.discharges == null ? '' : count(row.discharges) }}</td>
                    <td v-if="compareCharges">{{ row.charge_amount == null ? '' : formatMetric(row.charge_amount, '元') }}</td>
                  </tr></tbody>
                </table>
              </div>
            </template>
            <template v-else-if="printed >= chapter.end && chapter.id === 'anomaly' && result.anomaly.top_changes.length">
              <figure>
                <figcaption>门诊人次变化最大的科室</figcaption>
                <DashboardChart kind="comparison" :rows="departmentChanges" :theme="theme"
                  label="门诊人次" unit="人次" category="科室" snapshot :exporting="exporting" />
              </figure>
              <div class="table-scroll" tabindex="0" aria-label="科室波动明细">
                <table>
                  <thead><tr><th scope="col">科室</th><th scope="col">本期</th><th scope="col">上期</th><th scope="col">变化（人次）</th><th scope="col">环比</th></tr></thead>
                  <tbody><tr v-for="row in result.anomaly.top_changes" :key="row.dept_code">
                    <th scope="row">{{ outpatientDepartmentName(row.dept_code) }}</th><td>{{ count(row.current) }}</td><td>{{ count(row.previous) }}</td>
                    <td>{{ count(row.difference) }}</td><td>{{ formatMetric(row.change_pct, '%') }}</td>
                  </tr></tbody>
                </table>
              </div>
            </template>
          </section>
        </article>
      </div>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.generator-controls { margin-bottom: 14px; }
.generator-types { display: inline-flex; gap: 4px; margin: 0 0 12px; padding: 3px; border: 1px solid var(--border); border-radius: 10px; background: var(--page-base); }
.generator-types label { position: relative; cursor: pointer; }
.generator-types span { display: block; padding: 5px 12px; border-radius: 7px; color: var(--muted); font-size: .82rem; }
.generator-types :checked + span { background: var(--surface-tint); color: var(--brand-blue); font-weight: 650; }
.generator-types :focus-visible + span { outline: 2px solid var(--brand-blue); outline-offset: 2px; }
.generator-fields { display: flex; flex-wrap: wrap; align-items: end; gap: 9px 12px; }
.generator-fields .field { flex: 0 1 170px; }
.generator-period { align-self: center; color: var(--muted); font-size: .78rem; }
.generator-empty { display: grid; place-content: center; justify-items: center; gap: 7px; min-height: 220px; color: var(--muted); }
.generator-empty .workspace-icon { color: var(--brand-blue); }
.generator-empty h2 { margin: 0; color: var(--ink); font-size: 1rem; }
.generator-empty span { font-size: .8rem; }
.generator-preview { display: grid; grid-template-columns: 152px minmax(0, 900px); gap: 14px; justify-content: center; align-items: start; }
.generator-index { position: sticky; top: 14px; display: grid; gap: 4px; padding: 13px; }
.generator-index h2 { margin: 0 0 2px; font-size: .86rem; }
.generator-index button { padding: 6px 7px; border: 0; border-radius: 7px; background: transparent; color: var(--ink); text-align: left; font-size: .82rem; }
.generator-index button:hover { background: var(--surface-tint); color: var(--brand-blue); }
.section-page .generator-paper { min-width: 0; margin: 0; border: 1px solid var(--border); }
.generator-paper .report-chapter { scroll-margin-top: 18px; }
.generator-paper .report-chapter h3 { padding: 0; border: 0; }
.generator-paper .report-chapter figure { border-radius: 10px; }
.generator-departments { max-height: none; }
.generator-departments :is(td, th):not(:first-child) { text-align: right; white-space: nowrap; }
@media (max-width: 780px) {
  .generator-preview { grid-template-columns: minmax(0, 1fr); }
  .generator-index { position: static; display: flex; flex-wrap: wrap; align-items: center; }
  .generator-index h2 { margin: 0 6px 0 0; }
}
@media (max-width: 520px) { .generator-fields .field { flex: 1 1 132px; } }
</style>
