import test from 'node:test'
import assert from 'node:assert/strict'
import { describeAnswer, formatMetric } from '../src/api/analysis.mjs'
import { indicators, readMetric } from '../src/api/metricQuery.mjs'

const filters = { start_date: '2025-06-01', end_date: '2025-06-30', dept_code: 'HN' }
const data = {
  filters,
  meta: { source: 'ads_income_theme', available_start_date: '2025-06-01', available_end_date: '2025-06-30' },
  metrics: { charge_amount: { value: '100.00' } },
  rows: [{ group: '2025-06-01', value: '100.00' }, { group: '2025-06-02', value: null }],
  details: { fee_types: [
    { name: 'DRUG', value: '25.00' },
    { name: 'CONSUMABLE', value: '15.00' },
    { name: 'EXAM', value: '60.00' },
  ] },
}

test('metric queries preserve date and department scope and do not convert null trend points to zero', () => {
  const result = readMetric(data, indicators.charge, filters)
  assert.deepEqual(result.trend, [{ date: '2025-06-01', visits: 100 }, { date: '2025-06-02', visits: null }])
  assert.throws(() => readMetric(data, indicators.charge, { ...filters, dept_code: 'HX' }), /Scope mismatch/)
  assert.throws(() => readMetric(data, indicators.charge, { ...filters, start_date: '2025-06-02' }), /Scope mismatch/)
})

test('fee share uses the stable fee codes and omits incomplete data', () => {
  assert.equal(formatMetric(readMetric(data, indicators.drug, filters).value, '%'), '25%')
  assert.equal(readMetric(data, indicators.supply, filters).value, 15)
  const partial = { ...data, details: { fee_types: [...data.details.fee_types, { name: 'DRUG', value: null }] } }
  assert.equal(readMetric(partial, indicators.drug, filters).value, null)
})

test('bed utilization and length-of-stay trends use the certified periods', () => {
  const overview = { filters, meta: { ...data.meta, source: 'dwd_visit_detail' },
    bed_usage_rate: { value: '32.15' }, bed_trend: [{ date: '2025-06-01', occupied: 8, available: 50 }] }
  assert.deepEqual(readMetric(overview, indicators.bed, filters).trend, [{ date: '2025-06-01', visits: 16 }])
  const inpatient = { filters, meta: { ...data.meta, source: 'ads_inpatient_theme' }, avg_stay_days: { value: '10' },
    daily_trend: [{ date: '2025-06-01', avg_stay_days: null }, { date: '2025-06-02', avg_stay_days: '10' }] }
  assert.deepEqual(readMetric(inpatient, indicators.stay, filters).trend,
    [{ date: '2025-06-01', visits: null }, { date: '2025-06-02', visits: 10 }])
})

test('chat summaries preserve measured values and omit missing months', () => {
  assert.equal(describeAnswer({ label: '门诊人次', unit: '人次', points: [{ value: 2956 }], trend: [] }),
    '所查期间门诊人次为 2,956 人次。')
  assert.equal(describeAnswer({ label: '门诊人次', unit: '人次', points: [{ value: 0 }], trend: [] }),
    '所查期间门诊人次为 0 人次。')
  assert.equal(describeAnswer({ label: '床位使用率', unit: '%', points: [{ value: null }], trend: [] }),
    '所查期间暂无床位使用率数据。')
  assert.equal(describeAnswer({ label: '门诊人次环比', unit: '%', points: [{ value: null }], trend: [], nullReason: 'ZERO_BASE' }),
    '上期门诊人次为 0，无法计算环比。')
  assert.equal(describeAnswer({ label: '指标结果', unit: '%', points: [{ value: null }], trend: [], unavailable: true }),
    '当前无法计算该指标。')
  const points = [
    { start_date: '2025-04-01', value: 314 },
    { start_date: '2025-05-01', value: null },
    { start_date: '2025-06-01', value: 675 },
  ]
  assert.equal(describeAnswer({ label: '出院人次', unit: '人次', points, trend: points }),
    '出院人次在 2025 年 4 月为 314 人次，2025 年 6 月为 675 人次。')
})
