<script setup>
import { computed, onMounted, ref, shallowRef } from 'vue'
import { formatMetric, useAnalysisRequest } from '../../api/analysis.mjs'
import { buildQuery } from '../../api/dashboard.mjs'
import DateRangeField from '../../components/DateRangeField.vue'
import WorkspaceHeading from '../../components/WorkspaceHeading.vue'

const start = ref('')
const end = ref('')
const reports = shallowRef([])
const latest = computed(() => reports.value[0])
const { busy, error, run } = useAnalysisRequest()
const gateLabels = { PASS: '通过', FAIL: '未通过', BLOCKED_INSUFFICIENT_EVIDENCE: '证据不足' }

async function load() {
  if (busy.value) return
  reports.value = []
  if (Boolean(start.value) !== Boolean(end.value)) {
    error.value = '请同时填写开始和结束批次日期。'
    return
  }
  let query
  try {
    query = buildQuery({ start_date: start.value, end_date: end.value })
  } catch (cause) {
    error.value = cause.message
    return
  }
  const data = await run(`/api/ops/quality${query}`, {
    invalid: '请核对批次日期，查询范围最多 366 天。',
    read: rows => {
      if (!Array.isArray(rows)) throw new Error('Invalid quality response')
      return [...rows].sort((left, right) => right.batch_date.localeCompare(left.batch_date))
    },
  })
  if (data) reports.value = data
}

function reset() {
  start.value = end.value = ''
  return load()
}

onMounted(load)
</script>

<template>
  <main class="workspace section-page">
    <WorkspaceHeading><h1>数据质量</h1></WorkspaceHeading>
    <section class="panel filter-panel" aria-labelledby="quality-filter">
      <div class="section-heading"><h2 id="quality-filter">批次日期</h2></div>
      <form class="quality-filter" @submit.prevent="load">
        <DateRangeField v-model:start="start" v-model:end="end" :disabled="busy" />
        <div class="filter-actions">
          <button type="submit" class="button primary" :disabled="busy">查询</button>
          <button type="button" class="button secondary" :disabled="busy || (!start && !end)" @click="reset">重置</button>
        </div>
      </form>
    </section>

    <p v-if="busy" class="business-section panel filter-panel" role="status" aria-busy="true">正在读取质检报告…</p>
    <p v-else-if="error" class="business-section panel report-error" role="alert">{{ error }}</p>
    <template v-else-if="latest">
      <section class="business-section" aria-labelledby="quality-overview">
        <div class="section-heading">
          <h2 id="quality-overview">批次概览</h2>
          <time :datetime="latest.batch_date">{{ latest.batch_date }}</time>
        </div>
        <dl class="metric-tiles">
          <div><dt>质检结论</dt><dd><span class="status-chip"
            :class="{ 'status-chip-attention': latest.gate_status !== 'PASS' }">{{ gateLabels[latest.gate_status] }}</span></dd></div>
          <div><dt>质量评分</dt><dd class="quality-score">{{ formatMetric(latest.score, '分') }}</dd></div>
          <div><dt>规则覆盖率</dt><dd>{{ formatMetric(latest.coverage, '%') }}</dd></div>
          <div><dt>已执行 / 计划规则</dt><dd>{{ latest.executed_rules }} / {{ latest.planned_rules }}</dd></div>
        </dl>
      </section>
      <section class="business-section panel filter-panel" aria-labelledby="quality-history">
        <div class="section-heading"><h2 id="quality-history">质检记录</h2></div>
        <div class="table-scroll" tabindex="0" aria-label="质检记录，可滚动查看">
          <table>
            <thead><tr>
              <th scope="col">批次日期</th><th scope="col">结论</th><th scope="col">评分</th><th scope="col">规则覆盖率</th>
              <th scope="col">计划规则</th><th scope="col">已执行</th><th scope="col">已评估</th>
            </tr></thead>
            <tbody>
              <tr v-for="report in reports" :key="report.batch_date">
                <th scope="row"><time :datetime="report.batch_date">{{ report.batch_date }}</time></th>
                <td><span class="status-chip" :class="{ 'status-chip-attention': report.gate_status !== 'PASS' }">
                  {{ gateLabels[report.gate_status] }}</span></td>
                <td>{{ formatMetric(report.score, '分') }}</td><td>{{ formatMetric(report.coverage, '%') }}</td>
                <td>{{ report.planned_rules }}</td><td>{{ report.executed_rules }}</td><td>{{ report.evaluated_rules }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
    <p v-else class="business-section panel filter-panel" role="status">当前范围暂无质检报告。</p>
  </main>
</template>

<style scoped>
.quality-filter { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.quality-filter .overview-date-range { min-width: min(235px, 100%); }
.metric-tiles dd { font-variant-numeric: tabular-nums; }
.metric-tiles .quality-score { color: var(--brand-blue); }
#quality-overview + time { color: var(--muted); font-size: .84rem; }
.table-scroll th, .table-scroll td { white-space: nowrap; }
</style>
