<script setup>
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useManagementRequest } from '../../api/admin.mjs'
import { useSession } from '../../api/session.mjs'

const request = useManagementRequest('ops')
const session = useSession()
const canCheck = computed(() => session.current?.user.role === 'admin')
const names = {
  his_outpatient: '门诊', his_inpatient: '住院', his_charge: '收费',
  emr_record: '电子病历', lis_report: '检验', finance_excel: '财务',
}
const items = shallowRef([])
const loading = ref(false)
const selected = ref('')
const checking = ref(false)
const error = ref('')
const feedback = ref('')
const format = date => new Date(date).toLocaleString('zh-CN')

async function load() {
  if (loading.value || checking.value) return
  loading.value = true
  items.value = []
  error.value = ''
  try {
    const data = await request('sources')
    if (!Array.isArray(data)) throw new Error('数据源列表响应无效。')
    items.value = data
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function check() {
  if (!selected.value || checking.value) return
  checking.value = true
  feedback.value = ''
  error.value = ''
  let result
  try {
    result = await request(`sources/${selected.value}/check`, { method: 'POST' })
  } catch (cause) {
    error.value = cause.message
  } finally {
    checking.value = false
    selected.value = ''
  }
  if (result) {
    await load()
    feedback.value = result.outcome === 'SUCCESS' ? '登记文件检查通过。' : '登记文件检查失败，请核对文件和列格式。'
  }
}

onMounted(load)
</script>

<template>
  <header class="topbar">
    <nav class="topbar-inner" aria-label="数据源导航">
      <RouterLink :to="{ name: 'outpatient' }">返回门诊驾驶舱</RouterLink>
      <RouterLink :to="{ name: 'ops-tasks' }">任务管理</RouterLink>
    </nav>
  </header>
  <main class="workspace">
    <section class="page-heading"><div><h1>数据源管理</h1><p class="lead">查看已登记的文件批次和最近检查结果。</p></div></section>
    <section class="panel filter-panel">
      <div class="section-heading">
        <h2>登记状态</h2>
        <button class="button secondary" type="button" :disabled="loading || checking" @click="load">刷新数据源</button>
      </div>
      <div v-if="selected" class="filter-actions ops-feedback">
        <p role="status">确认检查{{ names[selected] }}登记文件？</p>
        <button class="button primary" type="button" :disabled="checking" @click="check">{{ checking ? '检查中…' : '确认检查' }}</button>
        <button class="button secondary" type="button" :disabled="checking" @click="selected = ''">取消</button>
      </div>
      <p v-if="feedback" class="ops-feedback" role="status">{{ feedback }}</p>
      <p v-if="error" class="report-error" role="alert">{{ error }}</p>
      <p v-if="loading" role="status" aria-busy="true">正在读取数据源…</p>
      <div v-else-if="items.length" class="table-scroll" tabindex="0" aria-label="数据源列表，可滚动查看">
        <table>
          <thead><tr><th scope="col">数据源</th><th scope="col">登记</th><th scope="col">数据集</th><th scope="col">批次</th><th scope="col">最近检查</th><th v-if="canCheck" scope="col">操作</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.name">
              <th scope="row">{{ names[item.name] }}</th>
              <td>{{ item.registered ? '已登记' : '未登记' }}</td>
              <td>{{ item.dataset_id || '—' }}</td><td>{{ item.batch_date || '—' }}</td>
              <td v-if="item.last_check">{{ item.last_check.outcome === 'SUCCESS' ? '通过' : '失败' }} · {{ format(item.last_check.at) }}</td><td v-else>暂无检查记录</td>
              <td v-if="canCheck"><button v-if="item.registered" class="button secondary" type="button" :disabled="checking || Boolean(selected)" @click="selected = item.name">检查文件</button><span v-else>—</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!error">暂无数据源。</p>
    </section>
  </main>
</template>
