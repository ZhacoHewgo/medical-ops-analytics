<script setup>
import { onMounted, ref, shallowRef } from 'vue'
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
  <header class="topbar">
    <nav class="topbar-inner" aria-label="角色管理导航"><RouterLink :to="{ name: 'admin-users' }">返回用户管理</RouterLink></nav>
  </header>
  <main class="workspace">
    <section class="page-heading">
      <div><h1>角色权限</h1><p class="lead">平台使用三种内置角色。账号的角色和科室在用户管理中调整。</p></div>
    </section>
    <section class="panel filter-panel" aria-label="角色列表">
      <p v-if="loading" role="status" aria-busy="true">正在读取角色…</p>
      <div v-else-if="error" role="alert">
        <p class="report-error">{{ error }}</p><button class="button secondary" type="button" @click="load">重试</button>
      </div>
      <p v-else-if="!roles.length">暂无角色。</p>
      <table v-else>
        <thead><tr><th scope="col">角色</th><th scope="col">权限</th></tr></thead>
        <tbody>
          <tr v-for="role in roles" :key="role.code">
            <th scope="row">{{ role.label }}</th>
            <td>{{ role.permissions.map(permission => descriptions[permission] || permission).join('、') }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </main>
</template>
