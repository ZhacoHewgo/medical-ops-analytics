import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import App from './App.vue'
import Login from './LoginPage.vue'
import ReportCenter from './views/ReportCenter.vue'
import UserManage from './views/admin/UserManage.vue'
import RoleManage from './views/admin/RoleManage.vue'
import { useSession } from './api/session.mjs'
import './style.css'

const pinia = createPinia()
const session = useSession(pinia)
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'demo', component: App },
    { path: '/login', name: 'login', component: Login },
    { path: '/reports', name: 'reports', component: ReportCenter, meta: { protected: true } },
    { path: '/admin/users', name: 'admin-users', component: UserManage, meta: { protected: true, admin: true } },
    { path: '/admin/roles', name: 'admin-roles', component: RoleManage, meta: { protected: true, admin: true } },
    { path: '/outpatient', name: 'outpatient', component: App, props: { authenticated: true }, meta: { protected: true } },
  ],
})

router.beforeEach(to => {
  if (to.meta.protected && !session.current) return { name: 'login' }
  if (to.meta.admin && session.current?.user.role !== 'admin') return { name: 'outpatient', query: { denied: 'admin' } }
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
