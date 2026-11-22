import test from 'node:test'
import assert from 'node:assert/strict'
import { comparisonRange, drillSelection, readReport, reportDateChoices, reportNarrative, reportPeriod } from '../src/api/report.mjs'

test('report narrative states observed counts and changes without inventing a comparison', () => {
  const report = {
    overview: { outpatient_visits: 8, previous_visits: 10, change_pct: '-20.00' },
    trend: [{ date: '2025-06-01', visits: 5 }, { date: '2025-06-02', visits: 3 }],
    ranking: [{ dept_code: 'HN', visits: 6 }, { dept_code: 'HX', visits: 2 }],
    anomaly: { status: 'AVAILABLE', top_changes: [{ dept_code: 'HN', current: 6, previous: 8, difference: -2 }] },
  }
  assert.deepEqual(reportNarrative(report).map(section => section.text), [
    '本期门诊就诊 8 人次，较上期减少 2 人次（20%）。上期门诊就诊 10 人次。',
    '日均门诊 4 人次。单日最高为 2025-06-01 的 5 人次。日门诊量在 3 至 5 人次之间，有 1 天高于本期日均。',
    '本期 2 个科室有门诊记录。心血管内科 6 人次，呼吸内科 2 人次。心血管内科占本期门诊量的 75%。',
    '与上期相比，心血管内科的门诊量变动最大，减少 2 人次。该科室上期为 8 人次，本期为 6 人次。',
  ])
  assert.match(reportNarrative({
    ...report,
    ranking: [
      { dept_code: 'HN', visits: 4 }, { dept_code: 'HX', visits: 2 },
      { dept_code: 'GK', visits: 1 }, { dept_code: 'EK', visits: 1 },
    ],
  })[2].text, /门诊量前三的科室合计 7 人次，占本期门诊量的 87.5%。$/)
  assert.deepEqual(reportNarrative({
    overview: { outpatient_visits: 0, previous_visits: null, change_pct: null },
    trend: [{ date: '2025-06-01', visits: 0 }], ranking: [],
    anomaly: { status: 'PRIOR_UNAVAILABLE', top_changes: [] },
  }).map(section => section.text), ['本期门诊就诊 0 人次。', '本期无门诊记录。', '暂无可比上期。'])
})

test('report date choices clip months to published bounds', () => {
  assert.deepEqual(reportDateChoices('2024-02-28', '2024-03-02'), {
    days: ['2024-02-28', '2024-02-29', '2024-03-01', '2024-03-02'],
    months: [
      { month: '2024-02', start_date: '2024-02-28', end_date: '2024-02-29' },
      { month: '2024-03', start_date: '2024-03-01', end_date: '2024-03-02' },
    ],
  })
  assert.throws(() => reportDateChoices('2025-01-02', '2025-01-01'))
  assert.throws(() => reportDateChoices('2025-02-29', '2025-03-01'))
})

test('operations narrative includes available metrics and omits missing values', () => {
  const report = reportNarrative({
    overview: { outpatient_visits: 5, admissions: 3, discharges: 2, charge_amount: '900.99',
      drug_share: '22.09', consumable_share: null, outpatient_charge: null },
    trend: [{ date: '2025-06-01', visits: 5, discharges: 2, charge_amount: '900.99' }],
    ranking: [], anomaly: { status: 'PRIOR_UNAVAILABLE', top_changes: [] },
  })
  assert.match(report[0].text, /入院 3 人次，出院 2 人次，收费合计 900.99 元，药品费占比 22.09%/)
  assert.doesNotMatch(report[0].text, /材料费占比|门诊收费/)
  assert.equal(report[1].title, '运营趋势')
  assert.match(report[1].text, /单日收费金额最高为 2025-06-01 的 900.99 元/)
})

test('generated reports use exact natural months and ISO weeks', () => {
  assert.deepEqual(reportPeriod('monthly', '2024-02'), { start_date: '2024-02-01', end_date: '2024-02-29' })
  assert.deepEqual(reportPeriod('weekly', '2025-W26'), { start_date: '2025-06-23', end_date: '2025-06-29' })
  assert.deepEqual(reportPeriod('weekly', '2020-W53'), { start_date: '2020-12-28', end_date: '2021-01-03' })
  assert.equal(reportPeriod('weekly', '2025-W53'), null)
  assert.equal(reportPeriod('weekly', '9999-W52'), null)
  assert.equal(reportPeriod('monthly', '2025-13'), null)
})

test('comparison aligns full calendar months across year rollover and leap years', () => {
  assert.deepEqual(comparisonRange({ start_date: '2024-01-01', end_date: '2024-01-31', dept_code: 'A' }, 'previous'), {
    start_date: '2023-12-01', end_date: '2023-12-31', dept_code: 'A',
  })
  assert.deepEqual(comparisonRange({ start_date: '2024-03-01', end_date: '2024-03-31', dept_code: null }, 'previous'), {
    start_date: '2024-02-01', end_date: '2024-02-29', dept_code: null,
  })
  assert.deepEqual(comparisonRange({ start_date: '2024-02-01', end_date: '2024-02-29', dept_code: null }, 'year'), {
    start_date: '2023-02-01', end_date: '2023-02-28', dept_code: null,
  })
  assert.equal(comparisonRange({ start_date: '2024-02-01', end_date: '2024-02-28' }, 'year'), null)
  assert.equal(comparisonRange({ start_date: '2024-02-01', end_date: '2024-03-31' }, 'previous'), null)
})

test('month and department drill keeps the intersection with the active selection', () => {
  const current = { start_date: '2024-02-13', end_date: '2024-03-04', dept_code: null }
  assert.deepEqual(drillSelection(current, { date: '2024-03', dept_code: 'A' }), {
    start_date: '2024-03-01', end_date: '2024-03-04', dept_code: 'A',
  })
  assert.deepEqual(drillSelection(current, { date: '2024-02-20', dept_code: null }), {
    start_date: '2024-02-20', end_date: '2024-02-20', dept_code: null,
  })
})

test('report contract rejects wrong groups, duplicate rows or edits that widen scope', async () => {
  const filters = { start_date: '2024-02-01', end_date: '2024-02-01', dept_code: 'A' }
  const data = {
    filters, outpatient_visits: 2, daily_trend: [{ date: '2024-02-01', visits: 2 }],
    department_ranking: [{ dept_code: 'A', visits: 2 }],
    meta: {
      source: 'ads_outpatient_theme', batch_date: '2024-02-02',
      build_run_id: 'a23f6721-546a-4425-b9a5-5671c114fca4',
      available_start_date: '2024-02-01', available_end_date: '2024-02-02', department_codes: ['A'],
    },
    group_by: 'day_department', rows: [{ date: '2024-02-01', dept_code: 'A', visits: 2 }],
  }
  const request = async () => ({ ok: true, json: async () => ({ code: 0, data }) })
  assert.equal(await readReport(request, filters, 'day_department', 'A'), data)
  await assert.rejects(readReport(request, filters, 'department', 'A'))
  data.rows = [{ ...data.rows[0], visits: 1 }, { ...data.rows[0], visits: 1 }]
  await assert.rejects(readReport(request, filters, 'day_department', 'A'))
  data.rows = [{ date: '2024-02-01', dept_code: 'B', visits: 2 }]
  await assert.rejects(readReport(request, filters, 'day_department', 'A'))
})
