const base = (import.meta.env?.VITE_API_BASE || '').replace(/\/+$/, '')
const origin = 'http://request.invalid'

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

export function requestWithSession(session, path, options, onExpired) {
  const token = session.current?.access_token
  if (!token || !path.startsWith('/api/')) throw new Error('Authentication required')
  return request(path, options, token, () => {
    if (session.current?.access_token === token) {
      session.setSession(null)
      onExpired()
    }
  })
}
