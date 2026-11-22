import { createApp, h, KeepAlive, ref, watch } from 'vue'
import { createPinia } from 'pinia'
import { RouterView } from 'vue-router'
import WorkspaceFrame from './components/WorkspaceFrame.vue'
import { clearPreloadedRequests, preloadWithSession, waitForPageRequest } from './api/request.js'
import { createAppRouter } from './router/index.js'
import { useSession } from './store/user.js'
import './style.css'

const pinia = createPinia()
const session = useSession(pinia)
const viewEpoch = ref(0)
const invalidateViews = () => {
  clearPreloadedRequests()
  viewEpoch.value += 1
}
const router = createAppRouter(session, undefined, invalidateViews)
let preloadedAccount
const visitedRoutes = new Set()
watch(() => [
  session.current?.access_token,
  session.current?.user.role,
  session.current?.user.department,
], () => {
  preloadedAccount = null
  visitedRoutes.clear()
  invalidateViews()
}, { flush: 'sync' })

router.afterEach((to, _from, failure) => {
  const account = session.current
  if (failure || to.name === 'login' || !account?.access_token) return
  visitedRoutes.add(to.name)
  if (to.meta.analysisTool || preloadedAccount === account) return
  preloadedAccount = account
  setTimeout(async () => {
    const { role, department } = account.user
    for (const { name, meta } of router.getRoutes()) {
      if (!meta.preload || visitedRoutes.has(name)
          || (meta.admin && role !== 'admin') || (meta.ops && role === 'department')) continue
      await waitForPageRequest()
      if (session.current !== account) return
      if (router.currentRoute.value.meta.analysisTool) {
        preloadedAccount = null
        return
      }
      if (visitedRoutes.has(name)) continue
      const [endpoint, query] = meta.preload.split('?')
      const params = [department && endpoint.startsWith('/api/v1/') && `dept_code=${encodeURIComponent(department)}`, query]
        .filter(Boolean).join('&')
      visitedRoutes.add(name)
      await preloadWithSession(session, params ? `${endpoint}?${params}` : endpoint, meta.retain ? undefined : 30000)
    }
  }, 0)
})

createApp({
  render: () => h(RouterView, null, {
    default: ({ Component, route }) => {
      if (!Component || (route.meta.protected && !session.current)) return null
      const page = h(Component, { key: route.path })
      if (route.name === 'login') return page
      return h(WorkspaceFrame, null, {
        default: () => [
          h(KeepAlive, { key: viewEpoch.value, max: 11 }, { default: () => route.meta.retain ? page : null }),
          route.meta.retain ? null : page,
        ],
      })
    },
  }),
})
  .use(pinia)
  .use(router)
  .mount('#app')
