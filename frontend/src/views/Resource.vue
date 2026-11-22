<script setup>
import { ref } from 'vue'
import ThemeBoard from '../components/ThemeBoard.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import RankBar from '../components/charts/RankBar.vue'
import { requestWithSession } from '../api/request.js'
import { departmentChartColor, outpatientDepartmentName } from '../api/departmentLabels.mjs'

defineOptions({ name: 'ResourceView' })

const labels = {
  discharges: ['出院人次', '人次'],
  available_bed_days: ['开放床日', '床日'],
  occupied_bed_days: ['占用床日', '床日'],
  bed_usage_rate: ['床位使用率', '%'],
}
const bedMetrics = ['bed_usage_rate', 'available_bed_days', 'occupied_bed_days']
const bedColumns = { bed_count: '床位数', available_bed_days: '开放床日', occupied_bed_days: '占用床日',
  bed_usage_rate: '床位使用率', bed_turnover: '床位周转次数' }
const ranking = ref('bed_usage_rate')
const rankings = {
  bed_usage_rate: ['床位使用率', '%'], bed_turnover: ['床位周转次数', '次'], stay: ['平均住院日', '天'],
}
const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
const recorded = rows => rows.filter(row => row.value !== null)
const patients = rows => rows.reduce((sum, row) => sum + row.count, 0)
const days = rows => rows.reduce((sum, row) => sum + Number(row.value), 0)
const ratio = (value, total) => total > 0 ? Number(value) / total : null
const renderRatio = (value, total) => total > 0 ? percent.format(ratio(value, total)) : '—'
const width = (value, total) => (ratio(value, total) ?? 0) * 100 + '%'

const rankRows = rows => recorded(rows).map(row => ({
  dept_code: row.name,
  dept_label: row.label !== row.name ? row.label : outpatientDepartmentName(row.name),
  dept_color: departmentChartColor(row.name),
  visits: Number(row.value),
})).sort((a, b) => b.visits - a.visits)

function departmentRanking(details, beds) {
  if (!hasDepartmentBeds(beds) || ranking.value === 'stay') return rankRows(details.departments)
  return rankRows(Object.entries(beds).map(([code, values]) => ({
    name: code, label: code, value: values[ranking.value] == null ? null : Number(Number(values[ranking.value]).toFixed(2)),
  })))
}

const availableBeds = metrics => bedMetrics.filter(key => metrics[key].value !== null)
const hasDepartmentBeds = beds => Object.keys(beds ?? {}).length > 0

function wardRows(departments, beds) {
  const known = new Set(departments.map(row => row.name))
  const withoutDischarges = Object.keys(beds ?? {}).filter(code => !known.has(code))
  return [...departments, ...withoutDischarges.map(code => ({ name: code, label: code, count: 0, value: null }))]
}

function bedCell(key, value) {
  if (value == null) return '—'
  return decimal.format(value) + (key === 'bed_usage_rate' ? '%' : '')
}

function loadData(session, query, options, onExpired) {
  return requestWithSession(session, `/api/v1/resource/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard class="resource-page" title="资源效率" source="ads_efficiency_theme"
    :labels="labels" :load-data="loadData" loading-layout="resource" :loading-cards="1">
    <template #dashboard="{ summary: { details, metrics, filters, department_beds }, select }">
      <div class="resource-board">
        <div class="resource-main">
          <section class="panel resource-panel" aria-labelledby="stay-distribution">
            <h2 id="stay-distribution">住院日区间分布</h2>
            <template v-if="recorded(details.stay_bands).length">
              <div class="stay-table-wrap" tabindex="0" aria-label="住院日区间分布，可滚动">
                <table class="stay-table">
                  <thead><tr><th scope="col">住院日区间</th><th scope="col">出院人次</th><th scope="col">住院日合计</th></tr></thead>
                  <tbody><tr v-for="row in recorded(details.stay_bands)" :key="row.name">
                    <th scope="row">{{ row.label }}</th>
                    <td>
                      <div class="stay-amount"><strong>{{ decimal.format(row.count) }} <small>人次</small></strong>
                        <span>{{ renderRatio(row.count, patients(recorded(details.stay_bands))) }}</span></div>
                      <div class="stay-track" aria-hidden="true"><span :style="{ width: width(row.count, patients(recorded(details.stay_bands))) }"></span></div>
                    </td>
                    <td>
                      <div class="stay-amount"><strong>{{ decimal.format(row.value) }} <small>天</small></strong>
                        <span>{{ renderRatio(row.value, days(recorded(details.stay_bands))) }}</span></div>
                      <div class="stay-track stay-days" aria-hidden="true"><span :style="{ width: width(row.value, days(recorded(details.stay_bands))) }"></span></div>
                    </td>
                  </tr></tbody>
                </table>
              </div>
              <p v-if="details.stay_bands.some(row => row.value === null)" class="resource-note">
                缺住院日记录 {{ decimal.format(patients(details.stay_bands.filter(row => row.value === null))) }} 人次
              </p>
            </template>
            <p v-else class="chart-empty">当前范围暂无住院日记录</p>
          </section>

          <section class="panel resource-panel" aria-labelledby="stay-metrics">
            <h2 id="stay-metrics">住院周转概况</h2>
            <dl class="stay-kpis">
              <div class="stay-average">
                <dt>平均住院日</dt>
                <dd><MotionNumber :value="ratio(days(recorded(details.stay_bands)), patients(recorded(details.stay_bands)))" :format="decimal.format" />
                  <small v-if="patients(recorded(details.stay_bands))">天</small></dd>
              </div>
              <div>
                <dt>出院人次</dt><dd><MotionNumber :value="metrics.discharges.value" :format="decimal.format" /> <small>人次</small></dd>
              </div>
              <div>
                <dt>住院日合计</dt>
                <dd v-if="recorded(details.stay_bands).length"><MotionNumber :value="days(recorded(details.stay_bands))" :format="decimal.format" /> <small>天</small></dd>
                <dd v-else>—</dd>
              </div>
            </dl>
          </section>
        </div>

        <section v-if="availableBeds(metrics).length" class="panel resource-panel" aria-labelledby="bed-metrics">
          <h2 id="bed-metrics">床位使用</h2>
          <dl class="stay-kpis bed-kpis">
            <div v-for="key in availableBeds(metrics)" :key="key" :class="{ 'bed-usage': key === 'bed_usage_rate' }">
              <dt>{{ labels[key][0] }}</dt>
              <dd><MotionNumber :value="metrics[key].value" :format="decimal.format" /> <small>{{ labels[key][1] }}</small></dd>
            </div>
          </dl>
        </section>

        <div v-if="details.departments.length || hasDepartmentBeds(department_beds)" class="resource-ward"
          :class="{ 'resource-ward-beds': hasDepartmentBeds(department_beds) }">
          <section v-if="!filters.dept_code && (hasDepartmentBeds(department_beds) || rankRows(details.departments).length)"
            class="panel resource-panel" aria-labelledby="ward-rank">
            <div class="ward-rank-head">
              <h2 id="ward-rank">科室对比</h2>
              <select v-if="hasDepartmentBeds(department_beds)" v-model="ranking" aria-label="科室对比指标">
                <option v-for="([label], key) in rankings" :key="key" :value="key">{{ label }}</option>
              </select>
            </div>
            <RankBar :data="departmentRanking(details, department_beds)"
              :label="rankings[hasDepartmentBeds(department_beds) ? ranking : 'stay'][0]"
              :unit="rankings[hasDepartmentBeds(department_beds) ? ranking : 'stay'][1]" category="科室" compact drillable
              @select-row="row => select(row.dept_code, 'department')" />
          </section>

          <section class="panel resource-panel" aria-labelledby="ward-detail">
            <h2 id="ward-detail">科室周转明细</h2>
            <div class="table-scroll ward-table" tabindex="0" aria-label="科室周转明细，可滚动">
              <table>
                <thead><tr>
                  <th scope="col">科室</th><th scope="col">出院人次</th><th scope="col">平均住院日（天）</th>
                  <template v-if="hasDepartmentBeds(department_beds)">
                    <th v-for="(label, key) in bedColumns" :key="key" scope="col">{{ label }}</th>
                  </template>
                </tr></thead>
                <tbody><tr v-for="row in wardRows(details.departments, department_beds)" :key="row.name">
                  <th scope="row">
                    <button v-if="!filters.dept_code" type="button" class="ward-link" @click="select(row.name, 'department')">
                      <span class="ward-dot" :style="{ backgroundColor: departmentChartColor(row.name) }" aria-hidden="true"></span>
                      {{ row.label === row.name ? outpatientDepartmentName(row.name) : row.label }}
                    </button>
                    <span v-else>{{ row.label === row.name ? outpatientDepartmentName(row.name) : row.label }}</span>
                  </th>
                  <td>{{ decimal.format(row.count) }}</td>
                  <td>{{ row.value === null ? '—' : decimal.format(row.value) }}</td>
                  <template v-if="hasDepartmentBeds(department_beds)">
                    <td v-for="(_, key) in bedColumns" :key="key">{{ bedCell(key, department_beds[row.name]?.[key]) }}</td>
                  </template>
                </tr></tbody>
              </table>
            </div>
          </section>
        </div>

        <section v-if="recorded(details.department_charges ?? []).length" class="panel resource-panel" aria-labelledby="department-charges">
          <h2 id="department-charges">科室收费明细</h2>
          <div class="table-scroll ward-table" tabindex="0" aria-label="科室收费明细，可滚动">
            <table>
              <thead><tr><th scope="col">科室</th><th scope="col">收费金额（元）</th></tr></thead>
              <tbody>
                <tr v-for="row in recorded(details.department_charges ?? [])" :key="row.name">
                  <th scope="row">{{ row.label === row.name ? outpatientDepartmentName(row.name) : row.label }}</th>
                  <td>{{ money.format(row.value) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </template>
  </ThemeBoard>
</template>

<style scoped>
.resource-board, .resource-main, .resource-ward { display: grid; gap: 14px; }
.resource-main { grid-template-columns: minmax(0, 2fr) minmax(235px, 1fr); }
.resource-ward { grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); }
.resource-ward-beds { grid-template-columns: minmax(0, 1fr) minmax(0, 2.3fr); }
.resource-ward:has(> :only-child) { grid-template-columns: minmax(0, 1fr); }
.resource-page :deep(.skeleton-resource-main), .resource-page :deep(.skeleton-resource-ward) { display: grid; gap: 14px; }
.resource-page :deep(.skeleton-resource-main) { grid-template-columns: minmax(0, 2fr) minmax(235px, 1fr); margin-bottom: 14px; }
.resource-page :deep(.skeleton-resource-ward:has(> :nth-child(2))) { grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); }
.resource-panel { min-width: 0; padding: 17px 18px; background: white; }
.resource-panel h2 { margin: 0 0 12px; color: var(--ink); font-size: 1rem; }
.stay-table-wrap { overflow-x: auto; }
.stay-table { width: 100%; min-width: 310px; table-layout: fixed; border-collapse: collapse; }
.stay-table th:first-child { width: 27%; }
.stay-table thead th { padding-top: 5px; background: transparent; color: var(--muted); text-align: left; }
.stay-table :is(th, td) { padding: 8px 8px 10px; border-bottom: 1px solid var(--border); font-size: .8rem; }
.stay-table tr:last-child :is(th, td) { border-bottom: 0; }
.stay-amount { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 6px; margin-bottom: 5px; font-variant-numeric: tabular-nums; }
.stay-amount span, .stay-amount small, .resource-note, .stay-kpis small { color: var(--muted); font-size: .73rem; }
.stay-amount small, .stay-kpis small { font-weight: 500; }
.stay-track { height: 6px; overflow: hidden; border-radius: 5px; background: var(--page-base); }
.stay-track > span { display: block; height: 100%; border-radius: inherit; background: var(--brand-blue); }
.stay-days > span { background: var(--warning-orange); }
.resource-note { margin: 9px 0 0; }
.stay-kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
.stay-kpis > div { padding: 10px; border-radius: 11px; background: var(--page-base); }
.stay-kpis .stay-average { grid-column: 1 / -1; padding: 12px; background: var(--surface-tint); }
.stay-kpis dt { color: var(--muted); font-size: .76rem; }
.stay-kpis dd { margin: 3px 0 0; color: var(--ink); font-size: 1.18rem; font-variant-numeric: tabular-nums; }
.stay-average dd { color: var(--brand-blue); font-size: clamp(1.85rem, 2.5vw, 2.4rem); line-height: 1.1; }
.bed-kpis { grid-template-columns: repeat(auto-fit, minmax(min(145px, 100%), 1fr)); }
.bed-kpis .bed-usage { background: var(--surface-tint); }
.bed-kpis .bed-usage dd { color: var(--brand-blue); }
.ward-link { display: inline-flex; align-items: center; gap: 6px; padding: 0; border: 0; background: transparent; color: var(--brand-blue); text-align: left; }
.ward-link:hover { text-decoration: underline; }
.ward-rank-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.ward-rank-head h2 { margin: 0; }
.ward-rank-head select { width: auto; max-width: 100%; min-height: 32px; height: auto; padding: 4px 6px; }
.ward-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.ward-table { max-height: none; margin-top: 0; }
.resource-ward-beds .ward-table :is(th, td) { white-space: nowrap; }
.ward-table :is(th, td):not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 950px) {
  .resource-main, .resource-ward, .resource-page :deep(.skeleton-resource-main), .resource-page :deep(.skeleton-resource-ward:has(> :nth-child(2))) {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 640px) { .resource-panel { padding: 14px; } }
</style>
