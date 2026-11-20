import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import App from './App.vue'
import Login from './LoginPage.vue'
import { useSession } from './api/session.mjs'
import './style.css'

const pinia = createPinia()
const session = useSession(pinia)
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'demo', component: App },
    { path: '/login', name: 'login', component: Login },
    { path: '/outpatient', name: 'outpatient', component: App, props: { authenticated: true }, meta: { protected: true } },
  ],
})

router.beforeEach(to => {
  if (to.meta.protected && !session.current) return { name: 'login' }
  if (to.name === 'login' && session.current) return { name: 'outpatient' }
})

createApp({
  render: () => h(RouterView, null, {
    default: ({ Component, route }) => Component ? h(Component, { key: route.path }) : null,
  }),
})
  .use(pinia)
  .use(router)
  .mount('#app')
