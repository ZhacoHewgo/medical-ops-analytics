<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import DashboardChart from '../components/DashboardChart.vue'
import { useSession } from '../api/session.mjs'
import { comparisonRange, drillSelection, readReport, readReportExport, reportGroups, ReportError } from '../api/report.mjs'

const router = useRouter()
const session = useSession()
const department = session.current?.user.department ?? null
const initial = { start_date: '', end_date: '', dept_code: department || '', group: 'day_department' }
const draft = reactive({ ...initial })
const current = shallowRef(null)
const bounds = shallowRef(null)
const busy = ref(false)
const error = ref('')
const exporting = ref(false)
const exportError = ref('')
const mode = ref('previous')
const comparison = ref('')
const page = ref(1)
const pageSize = 25
const pageCount = computed(() => Math.ceil((current.value?.rows.length || 0) / pageSize))
const visibleRows = computed(() => current.value?.rows.slice((page.value - 1) * pageSize, page.value * pageSize))
const group = computed(() => current.value && reportGroups[current.value.group_by])
const request = (path, options) => session.request(path, options, () => {
  void router.replace({ name: 'login', query: { expired: '1' } })
})
const format = number => number.toLocaleString('zh-CN')
const scopedLabel = code => code || '全院'
let pending
let pendingExport
let lastSelection = initial
const trail = shallowRef([])

function requestSignal(controller) {
  return AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
}

async function load(selection = draft) {
  lastSelection = { ...selection }
  pending?.abort()
  pendingExport?.abort()
  const controller = pending = new AbortController()
  current.value = null
  busy.value = true
  error.value = exportError.value = ''
  page.value = 1
  try {
    const result = await readReport(request, lastSelection, lastSelection.group, department, requestSignal(controller))
    if (controller.signal.aborted) return
    bounds.value = result.meta
    current.value = result
    Object.assign(draft, result.filters, { dept_code: result.filters.dept_code || '', group: result.group_by })
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause instanceof ReportError ? cause.message : '查询失败，请检查日期、网络或服务后重试。'
    }
  } finally {
    if (pending === controller) busy.value = false
  }
}

function apply(selection) {
  trail.value = []
  return load(selection)
}

function drill(row) {
  const data = current.value
  if (!data) return
  const filters = drillSelection(data.filters, row)
  if (data.group_by === 'day_department' && Object.keys(filters).every(key => filters[key] === data.filters[key])) {
    return
  }
  trail.value = [...trail.value, { ...data.filters, group: data.group_by }]
  return load({ ...filters, group: 'day_department' })
}

function back() {
  const selection = trail.value.at(-1)
  trail.value = trail.value.slice(0, -1)
  return load(selection)
}

watch([current, mode], async ([data, method], _, onCleanup) => {
  comparison.value = ''
  if (!data) return
  const previous = comparisonRange(data.filters, method)
  if (!previous) {
    comparison.value = '选择一个完整自然月后可比较。'
    return
  }
  if (previous.start_date < data.meta.available_start_date || previous.end_date > data.meta.available_end_date) {
    comparison.value = '暂无可比数据。对比月超出数据覆盖范围。'
    return
  }
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  comparison.value = '正在读取对比期…'
  try {
    const baseline = await readReport(request, previous, 'day', department, requestSignal(controller))
    if (controller.signal.aborted) return
    if (baseline.meta.build_run_id !== data.meta.build_run_id) {
      comparison.value = '数据批次已变化，请重新查询。'
    } else if (baseline.outpatient_visits === 0) {
      comparison.value = '暂无可比增长率。对比期门诊量为 0。'
    } else {
      const rate = (data.outpatient_visits - baseline.outpatient_visits) / baseline.outpatient_visits
      comparison.value = `${new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 2,
        signDisplay: 'exceptZero' }).format(rate)}（对比 ${previous.start_date.slice(0, 7)} 的 ${format(baseline.outpatient_visits)} 人次）`
    }
  } catch {
    if (!controller.signal.aborted) comparison.value = '暂无可比数据。对比期读取失败。'
  }
})

async function download(format) {
  const data = current.value
  if (!data || exporting.value) return
  exporting.value = true
  exportError.value = ''
  const controller = pendingExport = new AbortController()
  try {
    const file = await readReportExport(request, data, format, requestSignal(controller))
    if (controller.signal.aborted || current.value !== data) return
    const url = URL.createObjectURL(file)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `门诊人次.${format}`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (cause) {
    if (!controller.signal.aborted) {
      exportError.value = cause instanceof ReportError ? cause.message : '导出失败，请检查网络后重试。'
    }
  } finally {
    exporting.value = false
  }
}

onMounted(() => load(initial))
onBeforeUnmount(() => { pending?.abort(); pendingExport?.abort() })
</script>

<template>
  <header class="topbar">
    <nav class="topbar-inner" aria-label="报表导航">
      <RouterLink :to="{ name: 'outpatient' }">返回门诊驾驶舱</RouterLink>
    </nav>
  </header>
  <main class="workspace">
    <section class="page-heading"><div><h1>门诊报表</h1><p class="lead">按汇总日期和科室查看门诊人次。</p></div></section>
    <section class="panel filter-panel">
      <form class="filter-form report-filters" @submit.prevent="apply(draft)">
        <div class="field"><label for="report-start">开始日期</label><input id="report-start" v-model="draft.start_date" type="date"></div>
        <div class="field"><label for="report-end">结束日期</label><input id="report-end" v-model="draft.end_date" type="date"></div>
        <div class="field">
          <label for="report-dept">科室</label>
          <select id="report-dept" v-model="draft.dept_code" :disabled="department !== null || !bounds">
            <option v-if="department === null" value="">全院</option>
            <option v-for="code in bounds?.department_codes || (department ? [department] : [])" :key="code">{{ code }}</option>
          </select>
        </div>
        <div class="field">
          <label for="report-group">汇总方式</label>
          <select id="report-group" v-model="draft.group">
            <option v-for="(option, key) in reportGroups" :key="key" :value="key">{{ option.label }}</option>
          </select>
        </div>
        <div class="filter-actions">
          <button class="button primary" type="submit">查询</button>
          <button class="button secondary" type="button" @click="apply(initial)">重置</button>
        </div>
      </form>
    </section>

    <section v-if="busy" class="panel state-panel report-result" role="status" aria-busy="true">正在查询门诊报表…</section>
    <section v-else-if="error" class="panel state-panel report-result" role="alert">
      <p>{{ error }}</p>
      <button class="button secondary" type="button" @click="load(lastSelection)">重试查询</button>
    </section>
    <template v-else-if="current">
      <section class="panel filter-panel report-result">
        <div class="section-heading report-heading">
          <div>
            <h2>{{ group.label }} · {{ format(current.outpatient_visits) }} 人次</h2>
            <p>{{ current.filters.start_date }} 至 {{ current.filters.end_date }} · {{ scopedLabel(current.filters.dept_code) }}</p>
          </div>
          <div class="filter-actions">
            <button v-if="trail.length" class="button secondary" type="button" @click="back">返回上一级</button>
            <button class="button primary" type="button" :disabled="exporting" @click="download('xlsx')">导出 Excel</button>
            <button class="button secondary" type="button" :disabled="exporting" @click="download('pdf')">导出 PDF</button>
          </div>
        </div>
        <p v-if="exportError" class="report-error" role="alert">{{ exportError }}</p>
        <div class="report-comparison">
          <label for="report-compare">变化率</label>
          <select id="report-compare" v-model="mode"><option value="previous">自然月环比</option><option value="year">自然月同比</option></select>
          <span role="status">{{ comparison }}</span>
        </div>
        <p v-if="!current.rows.length" class="chart-empty">当前范围暂无门诊记录</p>
        <template v-else>
          <div class="table-scroll">
            <table data-testid="report-table">
              <thead><tr><th v-if="group.time" scope="col">{{ group.time }}</th><th v-if="group.department" scope="col">科室</th><th scope="col">门诊人次</th><th v-if="current.group_by !== 'day_department'" scope="col">详情</th></tr></thead>
              <tbody>
                <tr v-for="row in visibleRows" :key="JSON.stringify([row.date, row.dept_code])">
                  <td v-if="group.time">{{ row.date }}</td><td v-if="group.department">{{ row.dept_code }}</td>
                  <td>{{ format(row.visits) }}</td>
                  <td v-if="current.group_by !== 'day_department'"><button class="button secondary" type="button" @click="drill(row)">下钻</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="pageCount > 1" class="report-pagination">
            <button class="button secondary" type="button" :disabled="page === 1" @click="page--">上一页</button>
            <span>第 {{ page }} / {{ pageCount }} 页</span>
            <button class="button secondary" type="button" :disabled="page === pageCount" @click="page++">下一页</button>
          </div>
        </template>
      </section>
      <section v-if="current.daily_trend.length" class="panel chart-panel report-result">
        <div class="section-heading"><div><h2>门诊日趋势</h2><p>点击数据点查看当日明细。</p></div></div>
        <DashboardChart kind="trend" :rows="current.daily_trend" drillable @select-row="drill" />
      </section>
    </template>
  </main>
</template>
