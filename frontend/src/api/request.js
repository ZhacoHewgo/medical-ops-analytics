const base = (import.meta.env?.VITE_API_BASE || '').replace(/\/+$/, '')
const origin = 'http://request.invalid'
const preloaded = new Map()
const preloadKey = path => path.replace(/\?$/, '')
const pageRequests = new Set()
let activePreload

const interceptors = {
  request(options, token) {
    const headers = new Headers(options.headers)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return { ...options, headers, redirect: 'error', cache: 'no-store' }
  },
  response(response, token, onUnauthorized) {
    if (response.status === 401 && token) onUnauthorized()
    return response
  },
}

export async function request(path, options = {}, token = null, onUnauthorized = () => {}) {
  const url = new URL(path, origin)
  if (!path.startsWith('/api/') || url.origin !== origin || !url.pathname.startsWith('/api/')) {
    throw new Error('Invalid API path')
  }
  const response = await fetch(`${base}${url.pathname}${url.search}`, interceptors.request(options, token))
  return interceptors.response(response, token, onUnauthorized)
}

export function clearPreloadedRequests() {
  for (const { controller } of preloaded.values()) controller.abort()
  preloaded.clear()
}

export async function waitForPageRequest() {
  while (pageRequests.size) await Promise.allSettled(pageRequests)
}

export function waitForBackgroundRequest() {
  return activePreload
}

export function preloadWithSession(session, path, maxAge = 120000) {
  const token = session.current?.access_token
  if (!token) return
  const controller = new AbortController()
  const expiresAt = Date.now() + maxAge
  const response = request(path, {
    headers: { Accept: 'application/json' },
    priority: 'low',
    signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
  }, token).then(async result => result.status === 200
    ? new Response(await result.arrayBuffer(), result) : null).catch(() => null)
  activePreload = response
  void response.then(() => { if (activePreload === response) activePreload = null })
  preloaded.set(preloadKey(path), { token, controller, response, expiresAt })
  return response
}

export async function requestWithSession(session, path, options = {}, onExpired = () => {}) {
  const token = session.current?.access_token
  if (!token || !path.startsWith('/api/')) throw new Error('Authentication required')
  options.signal?.throwIfAborted()
  if (!options.method || options.method === 'GET') {
    const key = preloadKey(path)
    const entry = preloaded.get(key)
    if (entry) preloaded.delete(key)
    if (entry?.token === token && entry.expiresAt > Date.now()) {
      const abort = () => entry.controller.abort()
      options.signal?.addEventListener('abort', abort, { once: true })
      const response = await entry.response
      options.signal?.removeEventListener('abort', abort)
      options.signal?.throwIfAborted()
      if (session.current?.access_token !== token) throw new Error('Authentication required')
      if (response) return response
    } else {
      entry?.controller.abort()
    }
  }
  const response = request(path, options, token, () => {
    if (session.current?.access_token === token) {
      session.setSession(null)
      onExpired()
    }
  })
  pageRequests.add(response)
  try {
    return await response
  } finally {
    pageRequests.delete(response)
  }
}
