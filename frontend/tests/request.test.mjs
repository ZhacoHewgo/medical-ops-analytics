import test from 'node:test'
import assert from 'node:assert/strict'
import { clearPreloadedRequests, preloadWithSession, request, requestWithSession, waitForBackgroundRequest } from '../src/api/request.js'

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

test('first page read uses the in-flight preload once, and refresh makes a new request', async t => {
  const first = Promise.withResolvers()
  const fetch = t.mock.method(globalThis, 'fetch', async () => fetch.mock.callCount() === 0
    ? first.promise : Response.json({ count: 2 }))
  const session = { current: { access_token: 'TEST_ONLY' } }
  t.after(clearPreloadedRequests)
  const path = '/api/v1/inpatient/summary'

  const preload = preloadWithSession(session, path)
  assert.equal(waitForBackgroundRequest(), preload)
  const loaded = requestWithSession(session, path + '?', {})
  first.resolve(Response.json({ count: 1 }))
  assert.deepEqual(await (await loaded).json(), { count: 1 })
  assert.equal(waitForBackgroundRequest(), null)
  assert.equal(fetch.mock.callCount(), 1)
  assert.deepEqual(await (await requestWithSession(session, path, {})).json(), { count: 2 })
  assert.equal(fetch.mock.callCount(), 2)
})

test('failed and previous-account preloads fall back to the active session request', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async (_url, { headers }) => {
    if (fetch.mock.callCount() === 0) return new Response(null, { status: 503 })
    return Response.json({ account: headers.get('Authorization') })
  })
  t.after(clearPreloadedRequests)
  const session = { current: { access_token: 'OLD' } }
  const path = '/api/v1/resource/summary'

  preloadWithSession(session, path)
  assert.deepEqual(await (await requestWithSession(session, path, {})).json(), { account: 'Bearer OLD' })
  preloadWithSession(session, path)
  session.current = { access_token: 'NEW' }
  assert.deepEqual(await (await requestWithSession(session, path, {})).json(), { account: 'Bearer NEW' })
  assert.equal(fetch.mock.callCount(), 4)
})
