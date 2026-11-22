import test from 'node:test'
import assert from 'node:assert/strict'
import { importOverviewCsv, filterImportedOverview, appendedScope, appendOverview, overviewCsvTemplate } from '../src/api/overviewImport.mjs'
import { overviewDays, overviewPeriods } from '../src/api/overview.mjs'

test('one 100-row download fills all three trends for the next full month', () => {
  const files = [decodeURIComponent(overviewCsvTemplate('2025-06-30').split(',')[1])]
  const imported = importOverviewCsv(files)
  assert.equal(imported.added, 100)
  assert.equal(imported.start, '2025-07-01')
  assert.equal(imported.end, '2025-07-31')
  assert.equal(imported.departments.size, 10)
  assert.equal(importOverviewCsv(files, imported).skipped, 100)
  const filtered = filterImportedOverview(imported)
  const days = overviewDays(filtered)
  assert.equal(days.length, 31)
  assert(days.every(day => day.outpatient > 0 && day.inpatient > 0 && day.amount > 0))
  assert.deepEqual(filtered.charges.map(row => row.name), ['OUTPATIENT', 'INPATIENT'])
  const [month] = overviewPeriods(days, 'month')
  assert(month.outpatient > 2800 && month.inpatient > 450 && month.amount > 1_000_000_000)
})

test('imports and deduplicates a summary row as one record with all metrics', () => {
  const header = 'record_id,date,dept_code,outpatient_visits,discharges,outpatient_charge,inpatient_charge\n'
  const record = 'demo,2025-07-01,HN,24,5,125.10,10.05'
  const imported = importOverviewCsv([header + record])
  const [day] = overviewDays(filterImportedOverview(imported))
  assert.deepEqual([imported.added, day.outpatient, day.inpatient, day.amount], [1, 24, 5, 13515])
  assert.equal(importOverviewCsv([header + record], imported).skipped, 1)
  assert.throws(() => importOverviewCsv([header + record.replace('125.10', '0.001')]), /第 2 行记录无效/)
  assert.throws(() => importOverviewCsv([header + record.replace(',24,', ',-1,')]), /第 2 行记录无效/)
  assert.throws(() => importOverviewCsv([header + record.replace(',24,', ',25,')], imported), /重复编号/)
  assert.throws(() => importOverviewCsv([header + record.replace('demo,', 'second,')], imported), /同日同科室/)
  const detail = 'visit_id,visit_date,dept_code\nvisit,2025-07-01,HN'
  assert.throws(() => importOverviewCsv([detail], imported), /同日同科室/)
  assert.throws(() => importOverviewCsv([detail, header + record]), /同日同科室/)
})

test('imports course CSV layouts, skips repeat IDs and filters the dashboard', () => {
  const imported = importOverviewCsv([
    '\ufeffvisit_id,visit_date,dept_code,patient_name\r\n1,2025-08-01,HN,"测试,姓名"\r\n2,2025-08-02,HX,示例\r\n',
    'visit_id,visit_date,dept_code\n1,2025-08-01,HN\n',
    'visit_id,discharge_date,dept_code\n1,2025-08-02,HX',
    'charge_id,fee_date,dept_code,visit_type,amount\n1,2025-08-01,HN,门诊,0.10\n2,2025-08-01,HN,门诊,0.20\n3,2025-08-02,HX,住院,10.05',
  ])
  assert.equal(imported.added, 6)
  assert.equal(imported.skipped, 1)
  assert.doesNotMatch(JSON.stringify(imported), /测试|patient_name/)
  const full = filterImportedOverview(imported)
  assert.equal(full.charge_total, 10.35)
  assert.deepEqual(overviewDays(full).map(row => [row.outpatient, row.inpatient, row.amount]), [[1, null, 30], [1, 1, 1005]])
  const scoped = filterImportedOverview(imported, { start_date: '2025-08-02', dept_code: 'HX' })
  assert.equal(scoped.charge_total, 10.05)
  assert.equal(scoped.departments.length, 1)
  assert.equal(scoped.departments[0].inpatient, 1)
  assert.equal(scoped.departments[0].label, 'HX')
})

test('preserves unavailable metrics and rejects invalid or conflicting records', () => {
  const header = 'visit_id,visit_date,dept_code\n'
  const daily = overviewDays(filterImportedOverview(importOverviewCsv([header + '1,2025-08-01,HN'])))
  assert.deepEqual(overviewPeriods(daily, 'month').map(row => [row.outpatient, row.inpatient, row.amount]), [[1, null, null]])
  assert.throws(() => importOverviewCsv([header + '1,2025-02-29,HN']), /第 2 行/)
  assert.throws(() => importOverviewCsv([header + '1,2025-08-01,"HN']), /CSV 格式/)
  assert.throws(() => importOverviewCsv([header + '1,2025-08-01,HN\n1,2025-08-01,HX']), /重复编号/)
})

test('keeps different source date ranges distinct', () => {
  const imported = importOverviewCsv([
    'visit_id,visit_date,dept_code\n1,2025-08-01,HN\n2,2025-08-02,HN',
    'charge_id,fee_date,dept_code,visit_type,amount\n1,2025-08-02,HN,门诊,1\n2,2025-08-03,HN,门诊,2',
    'visit_id,visit_date,dept_code\n3,2025-08-04,HN',
  ])
  const data = filterImportedOverview(imported)
  const days = overviewDays(data)
  assert.deepEqual(days.map(day => [day.outpatient, day.amount]), [[1, null], [1, 100], [null, 200], [1, null]])
  assert.deepEqual(overviewPeriods(days, 'month', true).map(row => [row.outpatient, row.amount]), [[3, 300]])
  assert.equal(filterImportedOverview(imported, { end_date: '2025-08-01' }).charge_total, null)
  assert.equal(filterImportedOverview(imported, { start_date: '2025-08-03', end_date: '2025-08-03' }).departments[0].outpatient, null)
})

test('one file extends course figures and successive imports skip duplicates', () => {
  const csv = 'visit_id,visit_date,dept_code\n101,2025-08-01,HN'
  const initial = importOverviewCsv([csv])
  const imported = importOverviewCsv([csv], initial)
  assert.deepEqual([imported.added, imported.skipped], [1, 1])
  const publication = { available_start_date: '2025-06-29', available_end_date: '2025-06-30' }
  const scope = appendedScope(publication, imported)
  const course = {
    filters: scope.course,
    activity: [{ date: '2025-06-30', kind: 'OUTPATIENT', count: 10 }],
    charge_total: '10.00',
    charges: [{ name: 'OUTPATIENT', value: '10.00' }],
    daily_charges: [{ date: '2025-06-30', amount: '10.00' }],
    departments: [{ code: 'HN', label: '心血管内科', outpatient: 10, inpatient: 2, amount: '10.00' }],
  }
  const combined = appendOverview(course, imported, scope.filters)
  const days = overviewDays(combined)
  assert.deepEqual([combined.departments[0].outpatient, combined.departments[0].inpatient, combined.charge_total], [11, 2, 10])
  assert.deepEqual(days.slice(-2).map(row => [row.outpatient, row.amount]), [[null, null], [1, null]])
  assert.equal(appendedScope(publication, imported, { start_date: '2025-08-01' }).course, null)
  const wider = importOverviewCsv([csv + '\n102,2025-08-03,HN'], imported)
  assert.deepEqual(overviewDays(filterImportedOverview(wider)).map(row => row.outpatient), [1, 0, 1])
  const withCharge = importOverviewCsv(['charge_id,fee_date,dept_code,visit_type,amount\n1,2025-08-01,HN,门诊,0.05'], imported)
  const charged = appendOverview(course, withCharge, scope.filters)
  assert.deepEqual([charged.charge_total, charged.charges[0].value, charged.departments[0].amount], [10.05, 10.05, 10.05])
  assert.equal(appendOverview({ ...course, charge_total: null }, withCharge, scope.filters).charge_total, null)
  const overlap = importOverviewCsv(['visit_id,visit_date,dept_code\n102,2025-06-30,HN'])
  assert.throws(() => appendedScope(publication, overlap), /晚于/)
})
