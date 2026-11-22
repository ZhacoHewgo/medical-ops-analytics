<script setup>
import WorkspaceHeading from '../../components/WorkspaceHeading.vue'
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useManagementRequest } from '../../api/admin.mjs'

const request = useManagementRequest()
const roles = shallowRef([])
const loading = ref(false)
const error = ref('')
const descriptions = {
  read_hospital: '查询全院获准的聚合数据',
  read_department: '查询本科室获准的聚合数据',
  manage_accounts: '查看和管理平台账号',
  read_operations: '查看数据源与任务状态',
  run_operations: '检查登记文件和触发获准的采集',
}
const headings = {
  read_hospital: '全院数据',
  read_department: '本科室数据',
  manage_accounts: '账号管理',
  read_operations: '运维查看',
  run_operations: '采集操作',
}
const permissions = computed(() => {
  const published = new Set(roles.value.flatMap(role => role.permissions))
  const known = Object.keys(headings).filter(key => published.delete(key))
  return [...known, ...published]
})

async function load() {
  loading.value = true
  error.value = ''
  roles.value = []
  try {
    const data = await request('roles')
    if (!Array.isArray(data)) throw new Error('角色列表响应无效。')
    roles.value = data
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <main class="workspace section-page">
    <WorkspaceHeading>
      <h1>角色权限</h1>
      <template #tools>
        <RouterLink class="button secondary section-link" :to="{ name: 'admin-users' }">返回用户管理</RouterLink>
      </template>
    </WorkspaceHeading>
    <section class="panel filter-panel role-matrix" aria-labelledby="role-matrix-title">
      <div class="section-heading role-matrix-heading">
        <h2 id="role-matrix-title">权限矩阵</h2>
        <small v-if="roles.length">{{ roles.length }} 种角色 · {{ permissions.length }} 项权限</small>
      </div>
      <p v-if="loading" role="status" aria-busy="true">正在读取角色…</p>
      <div v-else-if="error" role="alert">
        <p class="report-error">{{ error }}</p><button class="button secondary" type="button" @click="load">重试</button>
      </div>
      <p v-else-if="!roles.length">暂无角色。</p>
      <div v-else class="table-scroll role-table" tabindex="0" aria-label="角色权限，可滚动查看">
        <table>
          <thead><tr><th scope="col">角色</th><th v-for="permission in permissions" :key="permission" scope="col"
            :title="descriptions[permission]">{{ headings[permission] || permission }}</th></tr></thead>
          <tbody>
            <tr v-for="role in roles" :key="role.code">
              <th scope="row">{{ role.label }}</th>
              <td v-for="permission in permissions" :key="permission">
                <template v-if="role.permissions.includes(permission)">
                  <span class="status-chip permission-granted" aria-hidden="true">允许</span>
                  <span class="report-visually-hidden">{{ descriptions[permission] || permission }}</span>
                </template>
                <template v-else><span class="permission-absent" aria-hidden="true">—</span><span class="report-visually-hidden">未授予</span></template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>

<style scoped>
.role-matrix-heading { flex-direction: row; justify-content: space-between; }
.role-matrix-heading small { color: var(--muted); font-size: .76rem; }
.role-table { position: relative; margin-top: 0; }
.role-table table { min-width: 590px; }
.role-table th:first-child { width: 20%; white-space: nowrap; }
.role-table tr > :first-child { position: sticky; left: 0; z-index: 1; background: white; }
.role-table thead tr > :first-child { z-index: 2; background: var(--surface-tint); }
.role-table :is(th, td):not(:first-child) { text-align: center; }
.role-table tbody tr { height: 48px; }
.permission-granted { color: var(--brand-blue); }
.permission-absent { color: var(--muted); }
</style>
