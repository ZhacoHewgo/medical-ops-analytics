<script setup>
import { nextTick, ref } from 'vue'
import ThemeBoard from '../components/ThemeBoard.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import RankBar from '../components/charts/RankBar.vue'
import { requestWithSession } from '../api/request.js'

defineOptions({ name: 'DiseaseView' })

const labels = {
  source_diagnosis_cases: ['有源诊断编码的出院人次', '人次'],
  discharges: ['出院人次', '人次'],
  drg_grouped_cases: ['DRG 入组病例', '例'],
  cmi: ['病例组合指数（CMI）', ''],
}
const search = ref('')
const detailPanel = ref(null)
const count = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
const cmiFormat = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 3, maximumFractionDigits: 3 })
const estimateMethod = {
  cmi_estimate: '以全院全期各病种的次均费用为权重依据，按病例数加权计算。',
  drg_rate_estimate: '有病种编码的出院病例数 ÷ 出院病例总数。',
}
const courseDiseaseNames = new Map([
  ['A09', '感染性腹泻'], ['C16', '胃癌'], ['E11', '糖尿病'], ['I10', '原发性高血压'],
  ['I25', '慢性缺血性心脏病'], ['I63', '脑梗死'], ['J18', '肺炎'], ['J44', '慢阻肺'],
  ['K35', '急性阑尾炎'], ['N18', '慢性肾脏病'],
])

const encoded = rows => rows.filter(row => row.name !== 'UNKNOWN')
const diagnosisName = row => row.label && row.label !== row.name ? row.label : (courseDiseaseNames.get(row.name) ?? row.name)
const hasNames = rows => rows.some(row => row.name !== 'UNKNOWN' && diagnosisName(row) !== row.name)
const rankRows = rows => encoded(rows).slice(0, 10).map(row => ({
  dept_code: row.name, dept_label: diagnosisName(row), visits: row.count,
}))
const topShare = (rows, total) => encoded(rows).slice(0, 3).reduce((sum, row) => sum + row.count, 0) / Number(total)
const share = (value, total) => value !== null && Number(total) > 0 ? percent.format(Number(value) / Number(total)) : '—'

function diseaseIndicators(metrics, estimates = []) {
  const cmi = metrics.cmi.value
  const grouped = metrics.drg_grouped_cases.value
  return [
    {
      label: '病例组合指数（CMI）', value: cmi ?? estimates.find(row => row.name === 'cmi_estimate')?.value,
      format: cmiFormat.format, estimate: cmi === null ? 'cmi_estimate' : null,
    },
    grouped !== null
      ? { label: 'DRG 入组病例', value: grouped, format: count.format, unit: '例' }
      : { label: 'DRG 入组率', value: estimates.find(row => row.name === 'drg_rate_estimate')?.value,
        format: count.format, unit: '%', estimate: 'drg_rate_estimate' },
    ...[['cost_index', '费用消耗指数'], ['time_index', '时间消耗指数']].map(([key, label]) => ({
      label, value: estimates.find(row => row.name === key)?.value, format: cmiFormat.format, estimate: key,
    })),
  ].filter(item => item.value != null)
}

function matches(rows) {
  const term = search.value.trim().toLowerCase()
  const exact = rows.find(row => row.name.toLowerCase() === term)
  if (exact) return [exact]
  return rows.filter(row => row.name.toLowerCase().includes(term) || diagnosisName(row).toLowerCase().includes(term))
}

async function selectDiagnosis(code) {
  search.value = code
  await nextTick()
  detailPanel.value?.scrollIntoView({ block: 'nearest' })
}

function loadData(session, query, options, onExpired) {
  search.value = ''
  return requestWithSession(session, `/api/v1/disease/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard class="disease-page" title="病种分析" source="ads_drg_theme"
    :labels="labels" :load-data="loadData" loading-layout="disease" :loading-cards="2">
    <template #dashboard="{ summary: { details, metrics } }">
      <div class="disease-board">
        <dl v-if="diseaseIndicators(metrics, details.disease_estimates).length" class="panel disease-panel disease-estimates" aria-label="DRG 与 CMI">
          <div v-for="item in diseaseIndicators(metrics, details.disease_estimates)" :key="item.label" :title="estimateMethod[item.estimate]">
            <dt>{{ item.label }} <small v-if="item.estimate">课程口径</small></dt>
            <dd class="coverage-value"><MotionNumber :value="item.value" :format="item.format" /><span v-if="item.unit">{{ item.unit }}</span></dd>
          </div>
        </dl>
        <div class="disease-main">
          <section class="panel disease-panel" aria-labelledby="diagnosis-rank">
            <div class="disease-heading">
              <h2 id="diagnosis-rank">出院诊断排行</h2>
              <span v-if="encoded(details.diagnoses).length">{{ encoded(details.diagnoses).length }} 个诊断编码</span>
            </div>
            <RankBar v-if="encoded(details.diagnoses).length" :data="rankRows(details.diagnoses)"
              label="出院人次" unit="人次" category="诊断" compact drillable @select-row="row => selectDiagnosis(row.dept_code)" />
            <p v-else class="chart-empty">当前范围暂无诊断编码</p>
          </section>

          <div class="disease-aside">
            <section class="panel disease-panel" aria-labelledby="diagnosis-coverage">
              <h2 id="diagnosis-coverage">诊断编码覆盖</h2>
              <div class="coverage-value">{{ share(metrics.source_diagnosis_cases.value, metrics.discharges.value) }}</div>
              <div v-if="metrics.source_diagnosis_cases.value !== null && Number(metrics.discharges.value) > 0">
                <progress :value="Number(metrics.source_diagnosis_cases.value)" :max="Number(metrics.discharges.value)" aria-label="诊断编码覆盖率"></progress>
                <p class="coverage-count">{{ count.format(metrics.source_diagnosis_cases.value) }} / {{ count.format(metrics.discharges.value) }} 出院人次</p>
              </div>
            </section>

            <section v-if="encoded(details.diagnoses).length" class="panel disease-panel" aria-labelledby="diagnosis-top">
              <div class="disease-heading">
                <h2 id="diagnosis-top">主要诊断</h2>
                <span v-if="Number(metrics.discharges.value) > 0">合计占 {{ percent.format(topShare(details.diagnoses, metrics.discharges.value)) }}</span>
              </div>
              <ol class="disease-top">
                <li v-for="row in encoded(details.diagnoses).slice(0, 3)" :key="row.name">
                  <button type="button" @click="selectDiagnosis(row.name)">
                    <span>{{ diagnosisName(row) }}</span><strong>{{ count.format(row.count) }} <small>人次</small></strong>
                  </button>
                </li>
              </ol>
            </section>
          </div>
        </div>

        <section ref="detailPanel" class="panel disease-panel" aria-labelledby="diagnosis-detail">
          <div class="disease-heading">
            <h2 id="diagnosis-detail">病种明细</h2>
            <div class="disease-search">
              <input v-model.trim="search" type="search" aria-label="搜索诊断"
                :placeholder="hasNames(details.diagnoses) ? '搜索诊断编码或名称' : '搜索诊断编码'">
              <button v-if="search" type="button" class="button secondary" @click="search = ''">查看全部</button>
            </div>
          </div>
          <div v-if="matches(details.diagnoses).length" class="table-scroll disease-table" tabindex="0" aria-label="病种明细，可滚动">
            <table>
              <thead><tr><th scope="col">编码</th><th v-if="hasNames(details.diagnoses)" scope="col">诊断名称</th>
                <th scope="col">出院人次</th><th scope="col">出院占比</th></tr></thead>
              <tbody><tr v-for="row in matches(details.diagnoses)" :key="row.name">
                <th scope="row">{{ row.name === 'UNKNOWN' ? row.label : row.name }}</th>
                <td v-if="hasNames(details.diagnoses)">{{ row.name === 'UNKNOWN' || diagnosisName(row) === row.name ? '—' : diagnosisName(row) }}</td>
                <td>{{ count.format(row.count) }}</td>
                <td>{{ share(row.count, metrics.discharges.value) }}</td>
              </tr></tbody>
            </table>
          </div>
          <p v-else class="chart-empty">{{ search ? '未找到匹配的诊断' : '当前范围暂无诊断明细' }}</p>
        </section>
      </div>
    </template>
  </ThemeBoard>
</template>

<style scoped>
.disease-board, .disease-main, .disease-aside { display: grid; gap: 14px; }
.disease-main { grid-template-columns: minmax(0, 1.65fr) minmax(0, 1fr); }
.disease-aside { align-content: start; }
.disease-panel { min-width: 0; padding: 17px 18px; background: white; }
.disease-panel h2 { margin: 0; color: var(--ink); font-size: 1rem; }
.disease-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.disease-heading > span, .coverage-count { color: var(--muted); font-size: .78rem; }
.coverage-value { margin: 5px 0 7px; color: var(--brand-blue); font-size: clamp(1.85rem, 3.2vw, 2.5rem); font-weight: 720; line-height: 1.1; font-variant-numeric: tabular-nums; }
.disease-panel progress { display: block; width: 100%; height: 7px; overflow: hidden; border: 0; border-radius: 8px; background: var(--surface-tint); color: var(--brand-blue); }
.disease-panel progress::-webkit-progress-bar { background: var(--surface-tint); }
.disease-panel progress::-webkit-progress-value { background: var(--brand-blue); }
.disease-panel progress::-moz-progress-bar { background: var(--brand-blue); }
.coverage-count { margin: 7px 0 0; }
.disease-estimates { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 10px; margin: 0; }
.disease-estimates > div { padding: 10px 12px; border-radius: 10px; background: var(--surface-tint); }
.disease-estimates dt { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 6px; font-size: .88rem; font-weight: 680; }
.disease-estimates small { color: var(--muted); font-size: .74rem; font-weight: 500; }
.disease-estimates dd > span { margin-left: 3px; color: var(--muted); font-size: .9rem; font-weight: 600; }
.disease-estimates dd { margin-bottom: 0; }
.disease-top { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.disease-top button { display: flex; justify-content: space-between; align-items: center; width: 100%; min-height: 32px; padding: 5px 9px; border: 0; border-radius: 8px; background: var(--page-base); color: var(--ink); font-size: .82rem; text-align: left; }
.disease-top button:hover { background: var(--surface-tint); }
.disease-top small { color: var(--muted); font-weight: 500; }
.disease-search { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.disease-search input { width: 170px; min-height: 36px; padding: 6px 9px; border: 1px solid var(--border); background: white; }
.disease-search .button { min-height: 36px; padding: 6px 10px; }
.disease-table { max-height: none; margin-top: 0; }
.disease-table table { min-width: 300px; }
.disease-table :is(th, td):nth-last-child(-n + 2) { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 920px) { .disease-main { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 640px) { .disease-panel { padding: 14px; } }
</style>
