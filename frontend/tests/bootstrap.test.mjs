import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'

test('bootstrap registers Pinia and router before mounting the unchanged dashboard at /', async () => {
  const source = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
  const dashboard = { name: 'Dashboard', render: () => null }
  const login = { name: 'Login', render: () => null }
  const session = { current: null }
  let app
  let root
  let mountedAt

  // Exercise the entry with real plugins; replace DOM mounting, SFC/CSS imports and browser history.
  runInNewContext(source.replace(/^import .+$/gm, ''), {
    App: dashboard,
    Login: login,
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
  assert.deepEqual(router.getRoutes().map(route => route.path).sort(), ['/', '/login', '/outpatient'])
  await router.push('/')
  assert.equal(router.currentRoute.value.matched[0].components.default, dashboard)
  await router.push('/outpatient')
  assert.equal(router.currentRoute.value.name, 'login')
  session.current = {}
  await router.push('/outpatient')
  assert.equal(router.currentRoute.value.name, 'outpatient')
  await router.push('/login')
  assert.equal(router.currentRoute.value.name, 'outpatient')
})
