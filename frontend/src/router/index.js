import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Outpatient from '../views/Outpatient.vue'
import Inpatient from '../views/Inpatient.vue'
import Revenue from '../views/Revenue.vue'
import Disease from '../views/Disease.vue'
import Resource from '../views/Resource.vue'
import Login from '../LoginPage.vue'
import ReportCenter from '../views/ReportCenter.vue'
import UserManage from '../views/admin/UserManage.vue'
import RoleManage from '../views/admin/RoleManage.vue'
import DataSourceManage from '../views/admin/DataSourceManage.vue'
import TaskManage from '../views/admin/TaskManage.vue'

export function createAppRouter(session, history = createWebHistory()) {
  const router = createRouter({
    history,
    routes: [
      { path: '/', name: 'demo', component: Dashboard, meta: { title: '管理驾驶舱' } },
      { path: '/login', name: 'login', component: Login },
      { path: '/reports', name: 'reports', component: ReportCenter, meta: { protected: true } },
      { path: '/admin/users', name: 'admin-users', component: UserManage, meta: { protected: true, admin: true } },
      { path: '/admin/roles', name: 'admin-roles', component: RoleManage, meta: { protected: true, admin: true } },
      { path: '/ops/sources', name: 'ops-sources', component: DataSourceManage, meta: { protected: true, ops: true } },
      { path: '/ops/tasks', name: 'ops-tasks', component: TaskManage, meta: { protected: true, ops: true } },
      { path: '/outpatient', name: 'outpatient', component: Outpatient, meta: { title: '门诊', protected: true } },
      { path: '/inpatient', name: 'inpatient', component: Inpatient, meta: { title: '住院', protected: true } },
      { path: '/revenue', name: 'revenue', component: Revenue, meta: { title: '收入', protected: true } },
      { path: '/disease', name: 'disease', component: Disease, meta: { title: '病种', protected: true } },
      { path: '/resource', name: 'resource', component: Resource, meta: { title: '资源效率', protected: true } },
    ],
  })

  router.beforeEach(to => {
    if (to.meta.protected && !session.current) return { name: 'login', query: { redirect: to.fullPath } }
    if (to.meta.admin && session.current?.user.role !== 'admin') return { name: 'outpatient', query: { denied: 'admin' } }
    if (to.meta.ops && session.current?.user.role === 'department') return { name: 'outpatient', query: { denied: 'ops' } }
    if (to.name === 'login' && session.current) return { name: 'outpatient' }
  })
  return router
}
