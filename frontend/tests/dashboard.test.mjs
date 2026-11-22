import test from 'node:test'
import assert from 'node:assert/strict'
import { buildQuery, createDashboardLoader, trendTransitionSeed, validateDashboard } from '../src/api/dashboard.mjs'

test('trend interpolation only seeds dates within the same authorized scope and published build', () => {
  const rows = ['01', '02', '03'].map((day, index) => ({ date: `2025-04-${day}`, visits: index + 1 }))
  const previous = { filters: { dept_code: 'HN' }, meta: { batch_date: '2025-06-30', build_run_id: 'a' }, daily_trend: rows }
  const filtered = { ...previous, daily_trend: rows.slice(1) }
  assert.equal(trendTransitionSeed(undefined, filtered), undefined)
  assert.deepEqual(trendTransitionSeed(previous, filtered), [{ ...rows[0], visits: null }, ...rows.slice(1)])
  assert.deepEqual(trendTransitionSeed(previous, { ...filtered, daily_trend: rows.slice(2) }), [])
  assert.deepEqual(trendTransitionSeed(previous, { ...filtered, filters: { dept_code: 'HX' } }), [])
  assert.deepEqual(trendTransitionSeed(previous, { ...filtered, meta: { ...filtered.meta, build_run_id: 'b' } }), [])
})

// Synthetic aggregates are test-only; the application never imports this file.
const dashboard = () => ({
  filters: { start_date: '2026-01-01', end_date: '2026-01-02', dept_code: null },
  outpatient_visits: 2,
  daily_trend: [{ date: '2026-01-01', visits: 2 }],
  department_ranking: [{ dept_code: '__UNKNOWN__', visits: 2 }],
  meta: {
    schema_version: '1.0', simulated: true,
    source_file: 'data/mock_his/his_outpatient.csv', source_sha256: 'a'.repeat(64),
    generated_at: '2026-01-03T00:00:00+08:00',
    available_start_date: '2026-01-01', available_end_date: '2026-01-02',
    department_codes: ['__UNKNOWN__'], quality_scope: 'whole_source',
    quality: { raw_rows: 2, invalid_id_rows: 0, invalid_date_rows: 0,
      conflicting_id_rows: 0, duplicate_rows_removed: 0, accepted_visits: 2,
      unknown_department_visits: 2 },
  },
})
const ok = (data = dashboard()) => ({ ok: true, status: 200, json: async () => ({ code: 0, data }) })

test('default query is empty; filters encode codes and omit whole-hospital selection', () => {
  assert.equal(buildQuery({}), '')
  assert.equal(buildQuery({ start_date: '2026-01-01', end_date: '2026-01-02', dept_code: '' }),
    '?start_date=2026-01-01&end_date=2026-01-02')
  assert.equal(new URLSearchParams(buildQuery({ dept_code: 'A&B +' }).slice(1)).get('dept_code'), 'A&B +')
  assert.match(buildQuery({ dept_code: '__UNKNOWN__' }), /dept_code=__UNKNOWN__/)
})

test('query filters survive browsers without URLSearchParams.size', () => {
  const descriptor = Object.getOwnPropertyDescriptor(URLSearchParams.prototype, 'size')
  try {
    delete URLSearchParams.prototype.size
    assert.equal('size' in URLSearchParams.prototype, false)
    assert.equal(buildQuery({}), '')
    assert.equal(buildQuery({ start_date: '2026-01-01', end_date: '2026-01-02', dept_code: 'A&B +' }),
      '?start_date=2026-01-01&end_date=2026-01-02&dept_code=A%26B+%2B')
  } finally {
    if (descriptor) Object.defineProperty(URLSearchParams.prototype, 'size', descriptor)
    else delete URLSearchParams.prototype.size
  }
  assert.deepEqual(Object.getOwnPropertyDescriptor(URLSearchParams.prototype, 'size'), descriptor)
})

test('reject impossible dates and reversed ranges before fetching', () => {
  for (const date of ['2026-02-29', '2026-1-01', 'wrong']) {
    assert.throws(() => buildQuery({ start_date: date }))
  }
  assert.doesNotThrow(() => buildQuery({ start_date: '2024-02-29' }))
  assert.throws(() => buildQuery({ start_date: '2026-02-01', end_date: '2026-01-01' }))
})

test('response validation rejects broken aggregates, quality and schema; accepts empty results', () => {
  assert.equal(validateDashboard(dashboard()).outpatient_visits, 2)
  const empty = dashboard()
  Object.assign(empty, { outpatient_visits: 0, daily_trend: [], department_ranking: [] })
  assert.equal(validateDashboard(empty).outpatient_visits, 0)
  const badSum = dashboard()
  badSum.outpatient_visits = 3
  assert.throws(() => validateDashboard(badSum))
  const badQuality = dashboard()
  badQuality.meta.quality.raw_rows = 5
  assert.throws(() => validateDashboard(badQuality))
  const badSchema = dashboard()
  badSchema.meta.schema_version = '2.0'
  assert.throws(() => validateDashboard(badSchema))
})

test('new request clears old numbers; HTTP failure stays explicit; retry uses same filters', async () => {
  const states = []
  const urls = []
  let fail = false
  const loader = createDashboardLoader({
    onChange: state => states.push(state),
    fetchImpl: async url => {
      urls.push(url)
      const result = dashboard()
      result.filters.dept_code = new URL(url, 'http://localhost').searchParams.get('dept_code')
      return fail ? { ok: false, status: 503 } : ok(result)
    },
  })
  await loader.load({})
  assert.equal(states.at(-1).status, 'success')
  fail = true
  await loader.load({ dept_code: '__UNKNOWN__' })
  assert.equal(states.at(-2).status, 'loading')
  assert.equal(states.at(-2).data, null)
  assert.equal(states.at(-1).status, 'error')
  assert.equal(states.at(-1).data, null)
  assert.match(states.at(-1).error, /503/)
  fail = false
  await loader.retry()
  assert.equal(urls.at(-1), '/api/dashboard/overview?dept_code=__UNKNOWN__')
  assert.equal(states.at(-1).status, 'success')
  assert.equal(states.at(-1).data.filters.dept_code, '__UNKNOWN__')
  loader.dispose()
})

test('late response cannot replace newer selection, even if transport ignores abort', async () => {
  const pending = []
  let state
  const loader = createDashboardLoader({
    onChange: value => { state = value },
    fetchImpl: () => new Promise(resolve => pending.push(resolve)),
  })
  const first = loader.load({})
  const second = loader.load({ dept_code: '__UNKNOWN__' })
  const newer = dashboard()
  newer.filters.dept_code = '__UNKNOWN__'
  pending[1](ok(newer))
  await second
  pending[0](ok())
  await first
  assert.equal(state.data.filters.dept_code, '__UNKNOWN__')
  assert.equal(state.status, 'success')
  loader.dispose()
})

test('network, invalid JSON and invalid payload never become a successful zero', async () => {
  for (const fetchImpl of [
    async () => { throw new TypeError('private transport detail') },
    async () => ({ ok: true, json: async () => { throw new SyntaxError('private body') } }),
    async () => ({ ok: true, json: async () => ({ code: 0, data: {} }) }),
  ]) {
    let state
    const loader = createDashboardLoader({ fetchImpl, onChange: value => { state = value } })
    await loader.load({})
    assert.equal(state.status, 'error')
    assert.equal(state.data, null)
    assert.doesNotMatch(state.error, /private/)
    loader.dispose()
  }
})

test('successful empty results stay distinct from errors', async () => {
  let state
  const empty = dashboard()
  Object.assign(empty, { outpatient_visits: 0, daily_trend: [], department_ranking: [] })
  const loader = createDashboardLoader({ fetchImpl: async () => ok(empty), onChange: value => { state = value } })
  await loader.load({})
  assert.equal(state.status, 'success')
  assert.equal(state.data.outpatient_visits, 0)
  assert.equal(state.error, '')
  loader.dispose()
})

test('response for a different effective selection is rejected', async () => {
  let state
  const loader = createDashboardLoader({ fetchImpl: async () => ok(), onChange: value => { state = value } })
  await loader.load({ dept_code: '__UNKNOWN__' })
  assert.equal(state.status, 'error')
  assert.equal(state.data, null)
  assert.match(state.error, /筛选与请求不一致/)
  loader.dispose()
})

test('disposing during a request prevents later UI updates', async () => {
  let resolve
  const states = []
  const loader = createDashboardLoader({
    fetchImpl: () => new Promise(done => { resolve = done }),
    onChange: state => states.push(state),
  })
  const loading = loader.load({})
  loader.dispose()
  resolve(ok())
  await loading
  assert.equal(states.length, 1)
  assert.equal(states[0].status, 'loading')
})

function previewDashboard() {
  const data = dashboard()
  data.meta = {
    source: 'dwd_visit_detail', batch_date: '2026-01-03',
    build_run_id: 'b171a123-514b-4566-a955-e29c4794d21e',
    available_start_date: '2026-01-01', available_end_date: '2026-01-02',
    department_codes: ['__UNKNOWN__'],
  }
  return data
}

test('preview loader requests the wide-table endpoint and preserves build provenance', async () => {
  let state
  const loader = createDashboardLoader({
    source: 'preview',
    onChange: value => { state = value },
    fetchImpl: async url => {
      assert.equal(url, '/api/dashboard/outpatient-preview')
      return ok(previewDashboard())
    },
  })
  await loader.load()
  assert.equal(state.status, 'success')
  assert.equal(state.data.meta.source, 'dwd_visit_detail')
})

test('preview rejects a fallback CSV response or missing build identity', () => {
  assert.throws(() => validateDashboard(dashboard(), 'preview'))
  assert.throws(() => validateDashboard(previewDashboard(), 'csv'))
  const stale = previewDashboard()
  stale.meta.build_run_id = ''
  assert.throws(() => validateDashboard(stale, 'preview'))
})

test('an empty preview response must still be inside the certified coverage', () => {
  const empty = previewDashboard()
  Object.assign(empty, { outpatient_visits: 0, daily_trend: [], department_ranking: [] })
  assert.equal(validateDashboard(empty, 'preview').outpatient_visits, 0)
  empty.filters.end_date = '2026-01-03'
  assert.throws(() => validateDashboard(empty, 'preview'))
})

test('authenticated dashboard uses only the ADS endpoint and enforces returned department scope', async () => {
  const data = dashboard()
  data.filters.dept_code = '__UNKNOWN__'
  data.meta = {
    available_start_date: data.meta.available_start_date,
    available_end_date: data.meta.available_end_date,
    department_codes: ['__UNKNOWN__'],
    source: 'ads_outpatient_theme',
    batch_date: '2026-01-02',
    build_run_id: '8c3194e3-c15a-40eb-be75-46435f32d542',
  }
  assert.equal(validateDashboard(data, 'ads', '__UNKNOWN__').outpatient_visits, 2)
  const states = []
  const requests = []
  const loader = createDashboardLoader({
    source: 'ads',
    department: '__UNKNOWN__',
    fetchImpl: async url => { requests.push(url); return ok(data) },
    onChange: state => states.push(state),
  })
  assert.equal((await loader.load()).outpatient_visits, 2)
  assert.deepEqual(requests, ['/api/v1/outpatient/summary?dept_code=__UNKNOWN__'])
  data.meta.department_codes.push('OTHER')
  assert.equal(await loader.retry(), null)
  assert.equal(states.at(-1).data, null)
  assert.equal(states.at(-1).status, 'error')
})
