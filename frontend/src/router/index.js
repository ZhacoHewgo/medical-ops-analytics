import { createRouter, createWebHistory } from 'vue-router'
import { multidimensionalPath } from '../api/report.mjs'
import Dashboard from '../views/Dashboard.vue'
import Outpatient from '../views/Outpatient.vue'
import Inpatient from '../views/Inpatient.vue'
import Revenue from '../views/Revenue.vue'
import Disease from '../views/Disease.vue'
import Resource from '../views/Resource.vue'
import Login from '../LoginPage.vue'
import ReportCenter from '../views/ReportCenter.vue'
import MetricQuery from '../views/MetricQuery.vue'
import AskData from '../views/AskData.vue'
import ReportGenerator from '../views/ReportGenerator.vue'
import MultidimensionalReport from '../views/MultidimensionalReport.vue'
import UserManage from '../views/admin/UserManage.vue'
import RoleManage from '../views/admin/RoleManage.vue'
import DataSourceManage from '../views/admin/DataSourceManage.vue'
import QualityManage from '../views/admin/QualityManage.vue'
import TaskManage from '../views/admin/TaskManage.vue'

export function createAppRouter(session, history = createWebHistory(), invalidateViews = () => {}) {
  const router = createRouter({
    history,
    routes: [
      { path: '/', redirect: { name: 'overview', query: {} } },
      { path: '/demo', redirect: { name: 'overview', query: {} } },
      { path: '/overview', name: 'overview', component: Dashboard,
        meta: { protected: true, retain: true, preload: '/api/v1/overview/summary' } },
      { path: '/reports/multidimensional', name: 'multidimensional-report', component: MultidimensionalReport,
        meta: { protected: true, retain: true, analysisTool: true, preload: multidimensionalPath() } },
      { path: '/login', name: 'login', component: Login },
      { path: '/outpatient', name: 'outpatient', component: Outpatient,
        meta: { title: '门诊', protected: true, retain: true, preload: '/api/v1/outpatient/summary' } },
      { path: '/inpatient', name: 'inpatient', component: Inpatient,
        meta: { title: '住院', protected: true, retain: true, preload: '/api/v1/inpatient/summary' } },
      { path: '/revenue', name: 'revenue', component: Revenue,
        meta: { title: '收入', protected: true, retain: true, preload: '/api/v1/revenue/summary' } },
      { path: '/disease', name: 'disease', component: Disease,
        meta: { title: '病种', protected: true, retain: true, preload: '/api/v1/disease/summary' } },
      { path: '/resource', name: 'resource', component: Resource,
        meta: { title: '资源效率', protected: true, retain: true, preload: '/api/v1/resource/summary' } },
      { path: '/reports', name: 'reports', component: ReportCenter,
        meta: { protected: true, retain: true, preload: '/api/v1/reports/outpatient?group_by=day_department' } },
      { path: '/metrics', name: 'metrics', component: MetricQuery, meta: { protected: true, retain: true, analysisTool: true } },
      { path: '/ask', name: 'ask', component: AskData, meta: { protected: true, retain: true, analysisTool: true } },
      { path: '/reports/generate', name: 'report-generate', component: ReportGenerator,
        meta: { protected: true, retain: true, analysisTool: true } },
      { path: '/admin/users', name: 'admin-users', component: UserManage,
        meta: { protected: true, admin: true, preload: '/api/auth/users?after=0' } },
      { path: '/admin/roles', name: 'admin-roles', component: RoleManage, meta: { protected: true, admin: true } },
      { path: '/ops/sources', name: 'ops-sources', component: DataSourceManage,
        meta: { protected: true, ops: true, preload: '/api/ops/sources' } },
      { path: '/ops/quality', name: 'ops-quality', component: QualityManage,
        meta: { protected: true, ops: true, preload: '/api/ops/quality' } },
      { path: '/ops/tasks', name: 'ops-tasks', component: TaskManage, meta: { protected: true, ops: true } },
    ],
  })

  let navigationVersion = 0
  router.beforeEach(async to => {
    const version = ++navigationVersion
    if (to.meta.protected && !session.current) return { name: 'login', query: { redirect: to.fullPath } }
    if (to.meta.admin && session.current?.user.role !== 'admin') return { name: 'outpatient', query: { denied: 'admin' } }
    if (to.meta.ops && session.current?.user.role === 'department') return { name: 'outpatient', query: { denied: 'ops' } }
    if (to.name === 'login' && session.current) return { name: 'outpatient' }
    if (to.meta.retain || to.meta.preload) {
      const current = session.current
      const response = await session.request('/api/auth/session', {
        signal: AbortSignal.timeout(3500),
      }, () => {}).catch(() => null)
      if (version !== navigationVersion) return false
      if (!session.current) return { name: 'login', query: { redirect: to.fullPath, expired: '1' } }
      if (session.current !== current) return false
      if (response?.status !== 204) invalidateViews()
    }
  })
  return router
}
