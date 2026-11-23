import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import { createApp, h, KeepAlive, ref, watch } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { defaultMultidimensional, multidimensionalPath } from '../src/api/report.mjs'

test('bootstrap registers Pinia and router and uses the protected overview as the home page', async () => {
  const source = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
  const routes = await readFile(new URL('../src/router/index.js', import.meta.url), 'utf8')
  const dashboard = { name: 'Dashboard', render: () => null }
  const login = { name: 'Login', render: () => null }
  const session = { current: null, request: async () => ({ status: 204 }) }
  let app
  let root
  let mountedAt
  const preloads = []
  const afterPaint = []
  const firstPage = Promise.withResolvers()
  const firstPreload = Promise.withResolvers()
  const firstPreloadStarted = Promise.withResolvers()

  const createAppRouter = runInNewContext(
    routes.replace(/^import .+$/gm, '').replace('export function', 'function') + '\ncreateAppRouter',
    {
      Dashboard: dashboard, Outpatient: dashboard, Inpatient: dashboard, Revenue: dashboard,
      Disease: dashboard, Resource: dashboard, Login: login,
      ReportCenter: { name: 'ReportCenter', render: () => null },
      MetricQuery: { name: 'MetricQuery', render: () => null },
      AskData: { name: 'AskData', render: () => null },
      ReportGenerator: { name: 'ReportGenerator', render: () => null },
      MultidimensionalReport: { name: 'MultidimensionalReport', render: () => null },
      UserManage: { name: 'UserManage', render: () => null },
      RoleManage: { name: 'RoleManage', render: () => null },
      DataSourceManage: { name: 'DataSourceManage', render: () => null },
      QualityManage: { name: 'QualityManage', render: () => null },
      TaskManage: { name: 'TaskManage', render: () => null },
      createRouter, createWebHistory: createMemoryHistory, AbortSignal, multidimensionalPath,
    }, { filename: 'src/router/index.js' },
  )
  runInNewContext(source.replace(/^import .+$/gm, ''), {
    createAppRouter,
    useSession: () => session,
    clearPreloadedRequests: () => {},
    preloadWithSession: (_session, path) => {
      preloads.push(path)
      if (preloads.length === 1) {
        firstPreloadStarted.resolve()
        return firstPreload.promise
      }
    },
    waitForPageRequest: () => firstPage.promise,
    setTimeout: callback => afterPaint.push(callback),
    encodeURIComponent,
    createApp: component => {
      root = component
      app = createApp(component)
      app.mount = target => {
        assert.ok(app.config.globalProperties.$pinia)
        assert.ok(app.config.globalProperties.$router)
        mountedAt = target
      }
      return app
    },
    createPinia, h, KeepAlive, ref, watch, createRouter, createWebHistory: createMemoryHistory, RouterView,
  }, { filename: 'src/main.js' })

  assert.equal(mountedAt, '#app')
  assert.equal(root.render().type, RouterView)
  const router = app.config.globalProperties.$router
  assert.deepEqual(router.getRoutes().map(route => route.path).sort(), [
    '/', '/admin/roles', '/admin/users', '/ask', '/demo', '/disease', '/inpatient', '/login', '/metrics',
    '/ops/quality', '/ops/sources', '/ops/tasks', '/outpatient', '/overview', '/reports', '/reports/generate', '/reports/multidimensional',
    '/resource', '/revenue',
  ])
  assert.deepEqual(router.getRoutes().filter(route => route.meta.retain).map(route => route.path).sort(),
    ['/ask', '/disease', '/inpatient', '/metrics', '/outpatient', '/overview', '/reports', '/reports/generate',
      '/reports/multidimensional', '/resource', '/revenue'])
  for (const home of ['/', '/?source=preview', '/demo', '/demo?source=preview']) {
    await router.push(home)
    assert.equal(router.currentRoute.value.name, 'login')
    assert.equal(router.currentRoute.value.query.redirect, '/overview')
  }
  for (const theme of ['inpatient', 'revenue', 'disease', 'resource', 'metrics', 'ask', 'reports/generate', 'reports/multidimensional']) {
    await router.push(`/${theme}`)
    assert.equal(router.currentRoute.value.name, 'login')
    assert.equal(router.currentRoute.value.query.redirect, `/${theme}`)
  }
  await router.push('/reports')
  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(router.currentRoute.value.query.redirect, '/reports')
  await router.push('/admin/users')
  assert.equal(router.currentRoute.value.name, 'login')
  await router.push('/ops/quality')
  assert.equal(router.currentRoute.value.query.redirect, '/ops/quality')
  session.current = { user: { role: 'department' } }
  await router.push('/ops/sources')
  assert.equal(router.currentRoute.value.query.denied, 'ops')
  await router.push('/ops/quality')
  assert.equal(router.currentRoute.value.query.denied, 'ops')
  session.current = { user: { role: 'analyst' } }
  await router.push('/ops/tasks')
  assert.equal(router.currentRoute.value.name, 'ops-tasks')
  await router.push('/ops/quality')
  assert.equal(router.currentRoute.value.name, 'ops-quality')
  await router.push('/admin/roles')
  assert.equal(router.currentRoute.value.name, 'outpatient')
  assert.equal(router.currentRoute.value.query.denied, 'admin')
  session.current = { user: { role: 'admin' } }
  await router.push('/')
  assert.equal(router.currentRoute.value.name, 'overview')
  assert.equal(router.currentRoute.value.matched[0].components.default, dashboard)
  await router.push('/admin/roles')
  assert.equal(router.currentRoute.value.name, 'admin-roles')
  for (const theme of ['inpatient', 'revenue', 'disease', 'resource']) {
    await router.push(`/${theme}`)
    assert.equal(router.currentRoute.value.name, theme)
  }
  await router.push('/outpatient')
  assert.equal(router.currentRoute.value.name, 'outpatient')
  await router.push('/login')
  assert.equal(router.currentRoute.value.name, 'outpatient')
  session.request = async () => {
    session.current = null
    return { status: 401 }
  }
  await router.push('/revenue')
  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(router.currentRoute.value.query.redirect, '/revenue')
  assert.equal(router.currentRoute.value.query.expired, '1')

  session.current = { access_token: 'TEST_DEPT', user: { role: 'department', department: 'HN' } }
  session.request = async () => ({ status: 204 })
  await router.push('/ask')
  assert.equal(afterPaint.length, 0)
  await router.push('/overview')
  assert.equal(afterPaint.length, 1)
  const warmup = afterPaint.shift()()
  assert.equal(preloads.length, 0)
  firstPage.resolve()
  await firstPreloadStarted.promise
  const multidimensional = multidimensionalPath({ ...defaultMultidimensional, dept_code: 'HN' })
  assert.deepEqual(preloads, [multidimensional])
  await router.push('/metrics')
  assert.equal(afterPaint.length, 0)
  firstPreload.resolve()
  await warmup
  assert.equal(preloads.length, 1)
  await router.push('/inpatient')
  assert.equal(afterPaint.length, 1)
  await afterPaint.shift()()
  assert.deepEqual(preloads.sort(), [
    '/api/v1/disease/summary?dept_code=HN',
    '/api/v1/outpatient/summary?dept_code=HN',
    multidimensional,
    '/api/v1/reports/outpatient?dept_code=HN&group_by=day_department',
    '/api/v1/resource/summary?dept_code=HN',
    '/api/v1/revenue/summary?dept_code=HN',
  ])

  let invalidations = 0
  const rapidRouter = createAppRouter(session, createMemoryHistory(), () => { invalidations += 1 })
  let checkingReports
  const reportsStarted = new Promise(resolve => { checkingReports = resolve })
  let finishReports
  session.current = { user: { role: 'analyst' } }
  session.request = () => {
    if (finishReports) return Promise.resolve({ status: 204 })
    return new Promise(resolve => {
      finishReports = resolve
      checkingReports()
    })
  }
  const pendingReports = rapidRouter.push('/reports')
  await reportsStarted
  await rapidRouter.push('/revenue')
  finishReports({ status: 503 })
  await pendingReports
  assert.equal(rapidRouter.currentRoute.value.name, 'revenue')
  assert.equal(invalidations, 0)
})
