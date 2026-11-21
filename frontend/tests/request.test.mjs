import test from 'node:test'
import assert from 'node:assert/strict'
import { request } from '../src/api/request.js'

test('request restricts API paths, adds the bearer header and invalidates unauthorized sessions', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(path, '/api/v1/outpatient/summary')
    assert.equal(options.headers.get('Authorization'), 'Bearer TEST_ONLY')
    assert.equal(options.redirect, 'error')
    return { status: 401 }
  })
  let expired = 0
  await assert.rejects(request('/api/../other'), /Invalid API path/)
  await request('/api/v1/outpatient/summary', {}, 'TEST_ONLY', () => expired++)
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(expired, 1)
})
