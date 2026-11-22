<script setup>
import { computed, ref } from 'vue'
import { departmentChartColor, outpatientDepartmentName as departmentName } from '../api/departmentLabels.mjs'
import { revenueCareColors as careColor } from '../styles/theme.js'
import MotionNumber from './charts/MotionNumber.vue'
import RankBar from './charts/RankBar.vue'
import TrendChart from './charts/TrendChart.vue'

const props = defineProps({ summary: { type: Object, required: true }, drillable: Boolean })
defineEmits(['select'])

const care = { OUTPATIENT: '门诊', INPATIENT: '住院', OTHER: '其他' }
const currency = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const compact = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })
const integer = new Intl.NumberFormat('zh-CN')
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
const money = value => value === null ? '—' : currency.format(value)
const feeScope = ref('all')
const details = computed(() => props.summary.details)
const visitTypes = computed(() => details.value.visit_types ?? [])
const charge = computed(() => props.summary.metrics.charge_amount.value)
const chargeCount = computed(() => props.summary.metrics.charge_count.value)
const finance = computed(() => [
  ...(details.value.income_totals ?? []),
  { name: 'net_revenue', label: '净医疗收入', value: props.summary.metrics.net_revenue.value },
].filter(item => item.value !== null))
const inDepartment = computed(() => props.summary.group_by === 'department')
const daily = computed(() => props.summary.rows.map(row => ({
  date: row.group, visits: row.value === null ? null : Number(row.value),
})))
const peak = computed(() => daily.value.reduce((best, row) => {
  if (row.visits === null) return best
  return !best || row.visits > best.visits ? row : best
}, null))
const departments = computed(() => props.summary.rows.filter(row => row.value !== null).map(row => ({
  dept_code: row.group, dept_label: departmentName(row.group), dept_color: departmentChartColor(row.group), visits: Number(row.value),
})).sort((a, b) => b.visits - a.visits))

function asCents(value) {
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(value ?? '')
  if (!match) return null
  const magnitude = BigInt(match[2]) * 100n + BigInt((match[3] ?? '').padEnd(2, '0'))
  return match[1] ? -magnitude : magnitude
}

function amountInYuan(cents) {
  const magnitude = cents < 0n ? -cents : cents
  return `${cents < 0n ? '-' : ''}${magnitude / 100n}.${String(magnitude % 100n).padStart(2, '0')}`
}

function formatCents(value) {
  return value === null ? '—' : money(amountInYuan(value))
}

const visitShares = computed(() => {
  const total = asCents(charge.value)
  const amounts = visitTypes.value.map(item => asCents(item.value))
  if (total === null || total <= 0n || amounts.some(value => value === null || value < 0n)) return []
  if (amounts.reduce((sum, value) => sum + value, 0n) !== total) return []
  return amounts.map(value => Number(value) / Number(total))
})

const fees = computed(() => {
  const categories = new Map()
  for (const entry of details.value.fee_types ?? []) {
    if (!categories.has(entry.name)) categories.set(entry.name, { label: entry.label, values: new Map() })
    const values = categories.get(entry.name).values
    const current = values.has(entry.group) ? values.get(entry.group) : 0n
    const amount = asCents(entry.value)
    values.set(entry.group, current === null || amount === null ? null : current + amount)
  }
  return [...categories].map(([key, { label, values }]) => {
    const amounts = visitTypes.value.map(row => values.has(row.name) ? values.get(row.name) : 0n)
    const total = amounts.includes(null) ? null : amounts.reduce((sum, value) => sum + value, 0n)
    return { key, label, total, amounts }
  }).sort(compareFees)
})

function compareFees(left, right) {
  if (left.total === right.total) return left.label.localeCompare(right.label, 'zh-CN')
  if (left.total === null) return 1
  if (right.total === null) return -1
  return left.total > right.total ? -1 : 1
}

const feeBars = computed(() => {
  const groupIndex = visitTypes.value.findIndex(row => row.name === feeScope.value)
  return fees.value.map(row => {
    const amounts = row.amounts.map((amount, index) => ({ amount, group: visitTypes.value[index].name }))
    return groupIndex < 0 ? { ...row, amounts } : {
      ...row, amounts: [amounts[groupIndex]], total: row.amounts[groupIndex],
    }
  }).sort(compareFees)
})
const largestFee = computed(() => feeBars.value.reduce((largest, row) => row.total !== null && row.total > largest ? row.total : largest, 0n))
const highlightedShares = computed(() => {
  const selected = visitTypes.value.find(row => row.name === feeScope.value)
  const total = asCents(selected ? selected.value : charge.value)
  if (total === null || total <= 0n || feeBars.value.some(row => row.total === null || row.total < 0n)) return []
  return feeBars.value.filter(row => row.key === 'DRUG' || row.key === 'CONSUMABLE').map(row => ({
    label: row.label, share: percent.format(Number(row.total) / Number(total)),
  }))
})
</script>

<template>
  <div class="revenue-dashboard">
    <section class="panel revenue-panel revenue-overview" aria-label="收费总览">
      <dl class="revenue-kpis">
        <div class="revenue-total">
          <dt>收费合计</dt>
          <dd><MotionNumber :value="charge" :format="money" /> <small v-if="charge !== null">元</small></dd>
          <dd v-if="chargeCount !== null" class="revenue-meta">{{ integer.format(chargeCount) }} 条收费明细</dd>
        </div>
        <div v-for="(item, index) in visitTypes" :key="item.name">
          <dt><i class="revenue-dot" :style="{ background: careColor[item.name] }" aria-hidden="true"></i>{{ item.label }}</dt>
          <dd><MotionNumber :value="item.value" :format="money" /> <small v-if="item.value !== null">元</small></dd>
          <dd class="revenue-meta">{{ integer.format(item.count) }} 条收费明细<span v-if="visitShares.length"> · {{ percent.format(visitShares[index]) }}</span></dd>
        </div>
      </dl>
      <div v-if="visitShares.length" class="revenue-split" aria-hidden="true">
        <span v-for="(item, index) in visitTypes" :key="item.name"
          :style="{ background: careColor[item.name], width: visitShares[index] * 100 + '%' }"></span>
      </div>
    </section>

    <div class="revenue-charts">
      <section class="panel revenue-panel" aria-labelledby="revenue-trend">
        <div class="revenue-panel-head">
          <h2 id="revenue-trend">{{ inDepartment ? '当日科室收费' : '每日收费趋势' }}</h2>
          <button v-if="!inDepartment && peak && daily.length > 1 && drillable" class="revenue-peak" type="button"
            @click="$emit('select', peak.date)">峰值 {{ peak.date.slice(5) }} <strong>{{ compact.format(peak.visits / 10000) }} 万元</strong></button>
        </div>
        <RankBar v-if="inDepartment && departments.length" :data="departments" label="收费金额" unit="元"
          category="科室" compact :drillable="drillable" @select-row="row => $emit('select', row.dept_code)" />
        <TrendChart v-else-if="!inDepartment && peak" :data="daily" label="收费金额" unit="元" period="日"
          :highlight-date="peak.date" :drillable="drillable" @select-row="row => $emit('select', row.date)" />
        <div v-else-if="summary.rows.length" class="table-scroll">
          <table>
            <thead><tr><th scope="col">{{ inDepartment ? '科室' : '日期' }}</th><th scope="col">收费金额（元）</th></tr></thead>
            <tbody><tr v-for="row in summary.rows" :key="row.group">
              <th scope="row">
                <button v-if="drillable" class="revenue-peak" type="button" @click="$emit('select', row.group)">
                  {{ inDepartment ? departmentName(row.group) : row.group }}
                </button>
                <span v-else>{{ inDepartment ? departmentName(row.group) : row.group }}</span>
              </th>
              <td>{{ money(row.value) }}</td>
            </tr></tbody>
          </table>
        </div>
        <p v-else class="chart-empty">当前范围暂无收费记录</p>
      </section>

      <section class="panel revenue-panel" aria-labelledby="revenue-structure">
        <div class="revenue-panel-head">
          <h2 id="revenue-structure">费用构成</h2>
          <div v-if="fees.length && visitTypes.length > 1" class="revenue-switch" role="group" aria-label="费用构成业务">
            <button type="button" :aria-pressed="feeScope === 'all'" @click="feeScope = 'all'">全部</button>
            <button v-for="item in visitTypes" :key="item.name" type="button" :aria-pressed="feeScope === item.name"
              @click="feeScope = item.name">{{ care[item.name] }}</button>
          </div>
        </div>
        <template v-if="feeBars.length">
          <div class="revenue-fee-meta">
            <span v-for="item in visitTypes.filter(row => feeScope === 'all' || row.name === feeScope)" :key="item.name">
              <i class="revenue-dot" :style="{ background: careColor[item.name] }" aria-hidden="true"></i>{{ care[item.name] }}
            </span>
            <span class="fee-unit">万元</span>
          </div>
          <ul class="revenue-fee-bars">
            <li v-for="row in feeBars" :key="row.key" :title="row.total === null ? '—' : formatCents(row.total) + ' 元'">
              <span>{{ row.label }}</span>
              <div class="revenue-fee-track" aria-hidden="true">
                <template v-if="largestFee > 0n && row.total !== null && row.amounts.every(item => item.amount >= 0n)">
                  <span v-for="item in row.amounts" :key="item.group"
                    :style="{ background: careColor[item.group], width: Number(item.amount) / Number(largestFee) * 100 + '%' }"></span>
                </template>
              </div>
              <strong>{{ row.total === null ? '—' : compact.format(Number(row.total) / 1000000) }}</strong>
            </li>
          </ul>
          <div v-if="highlightedShares.length" class="revenue-highlights">
            <span v-for="row in highlightedShares" :key="row.label">{{ row.label }} <strong>{{ row.share }}</strong></span>
          </div>
        </template>
        <p v-else class="chart-empty">当前范围暂无费用明细</p>
      </section>
    </div>

    <section v-if="details.visit_costs?.length" class="panel revenue-panel revenue-finance" aria-labelledby="revenue-visit-cost">
      <h2 id="revenue-visit-cost">次均费用</h2>
      <dl>
        <div v-for="item in details.visit_costs" :key="item.name">
          <dt>{{ item.label }}</dt>
          <dd><MotionNumber :value="item.value" :format="money" /> <small v-if="item.value !== null">元/人次</small></dd>
        </div>
      </dl>
    </section>

    <section v-if="finance.length" class="panel revenue-panel revenue-finance" aria-label="账单与台账">
      <h2>账单与台账</h2>
      <dl>
        <div v-for="item in finance" :key="item.name">
          <dt>{{ item.label }}</dt><dd><MotionNumber :value="item.value" :format="money" /> <small>元</small></dd>
        </div>
      </dl>
    </section>

    <section v-if="fees.length" class="panel revenue-panel" aria-labelledby="revenue-detail">
      <h2 id="revenue-detail">费用类别明细 <small>元</small></h2>
      <div class="table-scroll revenue-fees" tabindex="0" aria-label="费用类别明细，可滚动">
        <table>
          <thead><tr>
            <th scope="col">费用类别</th><th v-for="group in visitTypes" :key="group.name" scope="col">{{ care[group.name] }}</th>
            <th scope="col">合计</th>
          </tr></thead>
          <tbody><tr v-for="row in fees" :key="row.key">
            <th scope="row">{{ row.label }}</th>
            <td v-for="(value, index) in row.amounts" :key="visitTypes[index].name">{{ formatCents(value) }}</td>
            <td>{{ formatCents(row.total) }}</td>
          </tr></tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.revenue-dashboard { display: grid; gap: 14px; }
.revenue-panel { min-width: 0; padding: 17px 18px; background: white; }
.revenue-panel h2 { margin: 0; color: var(--ink); font-size: 1rem; }
.revenue-panel small { color: var(--muted); font-size: .75rem; font-weight: 500; }
.revenue-kpis { display: grid; grid-template-columns: minmax(0, 1.16fr); grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 12px 24px; margin: 0; }
.revenue-kpis > div, .revenue-finance dl > div { min-width: 0; }
.revenue-kpis dt, .revenue-finance dt { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: .8rem; }
.revenue-kpis dd, .revenue-finance dd { margin: 4px 0; color: var(--ink); font-size: clamp(1.02rem, 1.7vw, 1.55rem); font-variant-numeric: tabular-nums; }
.revenue-total dd:first-of-type { color: var(--brand-blue); font-size: clamp(1.35rem, 2.05vw, 2rem); line-height: 1.11; }
.revenue-kpis .revenue-meta { color: var(--muted); font-size: .76rem; }
.revenue-dot { display: inline-block; flex: none; width: 8px; height: 8px; border-radius: 50%; }
.revenue-split { display: flex; height: 6px; margin-top: 14px; overflow: hidden; border-radius: 5px; background: var(--page-base); }
.revenue-charts { display: grid; grid-template-columns: minmax(0, 1.24fr) minmax(0, 1fr); gap: 14px; }
.revenue-panel-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; min-height: 30px; margin-bottom: 10px; }
.revenue-peak { padding: 4px 8px; border: 0; border-radius: 8px; background: var(--surface-tint); color: var(--brand-blue); font-size: .75rem; }
.revenue-peak:hover { text-decoration: underline; }
.revenue-switch { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 8px; background: var(--page-base); }
.revenue-switch button { min-height: 26px; padding: 2px 8px; border: 0; background: transparent; color: var(--muted); font-size: .76rem; }
.revenue-switch button[aria-pressed='true'] { background: var(--surface-tint); color: var(--brand-blue); font-weight: 650; }
.revenue-fee-meta { display: flex; gap: 10px; margin-bottom: 8px; color: var(--muted); font-size: .73rem; }
.revenue-fee-meta span { display: inline-flex; align-items: center; gap: 4px; }
.revenue-fee-meta .fee-unit { margin-left: auto; }
.revenue-fee-bars { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.revenue-fee-bars li { display: grid; grid-template-columns: minmax(52px, max-content) minmax(0, 1fr) 52px; gap: 8px; align-items: center; min-height: 29px; font-size: .8rem; }
.revenue-fee-bars strong { text-align: right; font-variant-numeric: tabular-nums; }
.revenue-fee-track { display: flex; height: 8px; overflow: hidden; border-radius: 4px; background: var(--page-base); }
.revenue-highlights { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); color: var(--muted); font-size: .75rem; }
.revenue-highlights strong { color: var(--ink); }
.revenue-finance { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 30px; }
.revenue-finance dl { display: flex; flex-wrap: wrap; gap: 10px 30px; margin: 0; }
.revenue-finance dd { margin-bottom: 0; font-size: 1.05rem; }
.revenue-fees table { min-width: 460px; }
@media (max-width: 1050px) {
  .revenue-charts { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 640px) {
  .revenue-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); grid-auto-flow: row; }
  .revenue-total { grid-column: 1 / -1; }
  .revenue-panel { padding: 14px; }
}
</style>
