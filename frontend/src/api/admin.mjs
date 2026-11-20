import { onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useSession } from './session.mjs'

export const roleLabels = { admin: '系统管理员', analyst: '运营分析师', department: '科室账号' }

const messages = {
  401: '登录已失效，请重新登录。',
  403: '当前账号没有管理权限。',
  404: '找不到该账号，请刷新列表。',
  422: '请检查账号、角色、科室和密码格式。',
  503: '账号服务或审计暂不可用。请刷新核对后再操作。',
}

const operationMessages = {
  401: messages[401],
  403: '当前账号或环境未获准执行这项操作。',
  404: '没有找到该数据源。',
  409: '操作发生冲突，请刷新状态后再试。',
  503: '服务或审计暂不可用，请刷新核对状态。',
}

export function useManagementRequest(area = 'auth') {
  const operations = area === 'ops'
  const session = useSession()
  const router = useRouter()
  const lifetime = new AbortController()
  onBeforeUnmount(() => lifetime.abort())

  return async function request(path, { method = 'GET', data, timeout = 15000 } = {}) {
    let response
    try {
      const headers = { Accept: 'application/json' }
      if (data) headers['Content-Type'] = 'application/json'
      response = await session.request(`/api/${area}/${path}`, {
        method, headers, body: data && JSON.stringify(data),
        signal: AbortSignal.any([lifetime.signal, AbortSignal.timeout(timeout)]),
      }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    } catch {
      throw new Error(operations ? '操作响应中断，请刷新核对状态。' : '无法连接账号服务，请检查网络。')
    }
    if (!operations && response.status === 409) {
      throw new Error(method === 'POST' ? '用户名已存在。' : '账号已变化或受保护，请刷新列表。')
    }
    if (!response.ok) throw new Error((operations ? operationMessages : messages)[response.status] || '服务请求失败，请刷新核对。')
    const payload = await response.json().catch(() => null)
    if (payload?.code !== 0 || !payload.data) throw new Error('管理服务返回无效响应。')
    return payload.data
  }
}
