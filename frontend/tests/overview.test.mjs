import test from 'node:test'
import assert from 'node:assert/strict'
import { overviewBedPeriods, overviewDays, overviewFocus, overviewPeriods } from '../src/api/overview.mjs'

test('daily overview fills empty days and preserves explicit unknown charges', () => {
  const rows = overviewDays({
    filters: { start_date: '2025-06-29', end_date: '2025-07-02' },
    activity: [{ date: '2025-06-29', kind: 'OUTPATIENT', count: 4 }, { date: '2025-07-01', kind: 'INPATIENT', count: 2 }],
    daily_charges: [{ date: '2025-06-29', amount: '0.15' }, { date: '2025-06-30', amount: '0.20' },
      { date: '2025-07-02', amount: null }],
  })
  assert.deepEqual(rows.map(row => row.amount), [15, 20, 0, null])
  assert.deepEqual(rows.map(row => row.chargeRecorded), [true, true, false, false])
  assert.deepEqual(overviewPeriods(rows, 'week'), [
    { date: '2025-06-29', end: '2025-06-29', outpatient: 4, inpatient: 0, amount: 15 },
    { date: '2025-06-30', end: '2025-07-02', outpatient: 0, inpatient: 2, amount: null },
  ])
  assert.equal(overviewPeriods(rows, 'month')[0].amount, 35)
})

test('focus rates require two comparable weeks and a positive baseline', () => {
  const rows = Array.from({ length: 14 }, (_, i) => ({ outpatient: i < 7 ? 10 : 8, inpatient: 0, amount: 150 }))
  assert.equal(overviewFocus(rows.slice(1))[0].change, null)
  const [visits, discharges, charges] = overviewFocus(rows)
  assert.equal(visits.value, 56)
  assert.ok(Math.abs(visits.change + .2) < 1e-10)
  assert.equal(discharges.change, null)
  assert.equal(charges.change, 0)
})

test('bed usage periods use total occupied and available bed days', () => {
  const rows = [
    { date: '2025-04-30', occupied: 2, available: 4 },
    { date: '2025-05-01', occupied: 1, available: 2 },
    { date: '2025-05-02', occupied: 5, available: 8 },
  ]
  assert.deepEqual(overviewBedPeriods(rows, 'month'), [
    { date: '2025-04', visits: 50 }, { date: '2025-05', visits: 60 },
  ])
  assert.deepEqual(overviewBedPeriods(rows, 'week'), [{ date: '2025-04-30', visits: 57.14 }])
})
