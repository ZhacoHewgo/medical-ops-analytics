import test from 'node:test'
import assert from 'node:assert/strict'
import { comparisonRange, drillSelection, readReport } from '../src/api/report.mjs'

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
