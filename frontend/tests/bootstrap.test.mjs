import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'

test('bootstrap registers Pinia and router before mounting the unchanged dashboard at /', async () => {
  const source = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
  const routes = await readFile(new URL('../src/router/index.js', import.meta.url), 'utf8')
  const dashboard = { name: 'Dashboard', render: () => null }
  const login = { name: 'Login', render: () => null }
  const session = { current: null }
  let app
  let root
  let mountedAt

  const createAppRouter = runInNewContext(
    routes.replace(/^import .+$/gm, '').replace('export function', 'function') + '\ncreateAppRouter',
    {
      Dashboard: dashboard, Outpatient: dashboard, Inpatient: dashboard, Revenue: dashboard, Disease: dashboard, Resource: dashboard, Login: login,
      ReportCenter: { name: 'ReportCenter', render: () => null },
      UserManage: { name: 'UserManage', render: () => null },
      RoleManage: { name: 'RoleManage', render: () => null },
      DataSourceManage: { name: 'DataSourceManage', render: () => null },
      TaskManage: { name: 'TaskManage', render: () => null },
      createRouter, createWebHistory: createMemoryHistory,
    }, { filename: 'src/router/index.js' },
  )
  runInNewContext(source.replace(/^import .+$/gm, ''), {
    createAppRouter,
    useSession: () => session,
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
    createPinia, h, createRouter, createWebHistory: createMemoryHistory, RouterView,
  }, { filename: 'src/main.js' })

  assert.equal(mountedAt, '#app')
  assert.equal(root.render().type, RouterView)
  const router = app.config.globalProperties.$router
  assert.deepEqual(router.getRoutes().map(route => route.path).sort(), ['/', '/admin/roles', '/admin/users', '/disease', '/inpatient', '/login', '/ops/sources', '/ops/tasks', '/outpatient', '/reports', '/resource', '/revenue'])
  await router.push('/')
  assert.equal(router.currentRoute.value.matched[0].components.default, dashboard)
  for (const theme of ['inpatient', 'revenue', 'disease', 'resource']) {
    await router.push(`/${theme}`)
    assert.equal(router.currentRoute.value.name, 'login')
    assert.equal(router.currentRoute.value.query.redirect, `/${theme}`)
  }
  await router.push('/reports')
  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(router.currentRoute.value.query.redirect, '/reports')
  await router.push('/admin/users')
  assert.equal(router.currentRoute.value.name, 'login')
  session.current = { user: { role: 'department' } }
  await router.push('/ops/sources')
  assert.equal(router.currentRoute.value.query.denied, 'ops')
  session.current = { user: { role: 'analyst' } }
  await router.push('/ops/tasks')
  assert.equal(router.currentRoute.value.name, 'ops-tasks')
  await router.push('/admin/roles')
  assert.equal(router.currentRoute.value.name, 'outpatient')
  assert.equal(router.currentRoute.value.query.denied, 'admin')
  session.current = { user: { role: 'admin' } }
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
})
