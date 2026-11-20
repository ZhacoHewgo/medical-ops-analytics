import { defineStore } from 'pinia'

const storageKey = 'mops.auth.session'

function sessionFrom(data) {
  const user = data?.user
  if (data?.token_type !== 'bearer' || typeof data.access_token !== 'string'
      || !/^[\w.-]+$/.test(data.access_token) || typeof user?.username !== 'string'
      || !['admin', 'analyst', 'department'].includes(user.role)
      || (user.role === 'department' ? !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(user.department ?? '')
        : user.department !== null)) {
    return null
  }
  return { token_type: 'bearer', access_token: data.access_token, user: {
    username: user.username, role: user.role, department: user.department,
  } }
}

function restore() {
  try {
    return sessionFrom(JSON.parse(sessionStorage.getItem(storageKey)))
  } catch {
    return null
  }
}

export const useSession = defineStore('session', {
  state: () => ({ current: restore() }),
  actions: {
    setSession(value) {
      this.current = value
      try {
        if (value) sessionStorage.setItem(storageKey, JSON.stringify(value))
        else sessionStorage.removeItem(storageKey)
      } catch {
        // Browsers that block storage can still use the current session in memory.
      }
    },
    async login(username, password) {
      const response = await fetch('/api/auth/login', {
        method: 'POST', redirect: 'error', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ username, password }),
        signal: AbortSignal.timeout(15000),
      })
      if (response.status === 401) return '用户名或密码错误，或账号已停用。'
      if (response.status === 422) return '请输入有效的用户名和密码。'
      if (!response.ok) return '暂时无法登录，请检查服务后重试。'
      const payload = await response.json()
      const session = payload?.code === 0 ? sessionFrom(payload.data) : null
      if (!session) return '登录响应无效，请检查服务。'
      this.setSession(session)
      return ''
    },
    async request(path, options, onExpired) {
      const token = this.current?.access_token
      if (!token || !path.startsWith('/api/')) throw new Error('Authentication required')
      const headers = new Headers(options.headers)
      headers.set('Authorization', `Bearer ${token}`)
      const response = await fetch(path, { ...options, headers, redirect: 'error', cache: 'no-store' })
      if (response.status === 401 && this.current?.access_token === token) {
        this.setSession(null)
        onExpired()
      }
      return response
    },
  },
})
