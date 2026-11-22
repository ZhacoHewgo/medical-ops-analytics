import { isMetricValue } from './analysis.mjs'

const outpatient = { endpoint: 'outpatient', source: 'ads_outpatient_theme' }
const inpatient = { endpoint: 'inpatient', source: 'ads_inpatient_theme' }
const revenue = { endpoint: 'revenue', source: 'ads_income_theme' }
const overview = { endpoint: 'overview', source: 'dwd_visit_detail' }

function feeShare(data, code) {
  const total = Number(data.metrics.charge_amount.value)
  if (total <= 0) return null
  const rows = data.details.fee_types
  if (!rows.length || rows.some(row => !isMetricValue(row.value) || Number(row.value) < 0)) return null
  const amount = rows.filter(row => row.name === code).reduce((sum, row) => sum + Number(row.value), 0)
  return amount * 100 / total
}

export const indicators = {
  outpatient: { ...outpatient, label: '门诊人次', unit: '人次', value: data => data.outpatient_visits,
    trend: data => data.daily_trend.map(row => ({ date: row.date, visits: row.visits })) },
  discharge: { ...inpatient, label: '出院人次', unit: '人次', value: data => data.discharges,
    trend: data => data.daily_trend.map(row => ({ date: row.date, visits: row.discharges })) },
  stay: { ...inpatient, label: '平均住院日', unit: '天', value: data => data.avg_stay_days.value,
    trend: data => data.daily_trend.map(row => ({ date: row.date, visits: row.avg_stay_days ?? null })) },
  bed: { ...overview, label: '床位使用率', unit: '%', value: data => data.bed_usage_rate?.value ?? null,
    trend: data => (data.bed_trend ?? []).map(row => ({ date: row.date,
      visits: row.available > 0 ? Number((row.occupied * 100 / row.available).toFixed(2)) : null })) },
  cost: { ...inpatient, label: '住院次均费用', unit: '元/人次', value: data => data.avg_cost.value },
  charge: { ...revenue, label: '收费明细金额', unit: '元', value: data => data.metrics.charge_amount.value,
    trend: data => data.rows.map(row => ({ date: row.group, visits: row.value })) },
  drug: { ...revenue, label: '药品费占比', unit: '%', value: data => feeShare(data, 'DRUG') },
  supply: { ...revenue, label: '材料费占比', unit: '%', value: data => feeShare(data, 'CONSUMABLE') },
  departments: { ...outpatient, label: '门诊覆盖科室', unit: '个', value: data => data.department_ranking.length },
}

export function readMetric(data, indicator, selected) {
  if (data.meta?.source !== indicator.source || !data.filters
      || data.filters.dept_code !== (selected.dept_code || null)
      || data.filters.start_date !== (selected.start_date || data.meta.available_start_date)
      || data.filters.end_date !== (selected.end_date || data.meta.available_end_date)) throw new Error('Scope mismatch')
  const value = indicator.value(data)
  const trend = indicator.trend?.(data) ?? []
  if (value !== null && !isMetricValue(value)
      || trend.some(row => row.visits !== null && !isMetricValue(row.visits))) throw new Error('Invalid metric')
  return { filters: data.filters, value, trend: trend.map(row => ({
    date: row.date,
    visits: row.visits === null ? null : Number(row.visits),
  })) }
}
