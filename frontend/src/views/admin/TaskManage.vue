<script setup>
import { onMounted, ref, shallowRef } from 'vue'
import { useManagementRequest } from '../../api/admin.mjs'
import ScheduleEditor from './ScheduleEditor.vue'

const request = useManagementRequest('ops')
const names = { extract_ods: 'ODS 采集', clean_dwd: 'DWD 清洗', quality_check: '质量检查', build_dwd: '宽表构建' }
const labels = { START: '已启动，尚无结果', SUCCESS: '成功', FAILED: '失败', BLOCKED: '受阻', PARTIAL: '部分写入' }
const items = shallowRef([])
const allowed = ref(false)
const loading = ref(false)
const confirmed = ref(false)
const running = ref(false)
const error = ref('')
const feedback = ref('')
const format = date => new Date(date).toLocaleString('zh-CN')

async function load() {
  if (loading.value || running.value) return
  loading.value = true
  items.value = []
  allowed.value = false
  error.value = ''
  try {
    const data = await request('tasks')
    if (!Array.isArray(data.items)) throw new Error('任务列表响应无效。')
    items.value = data.items
    allowed.value = data.can_run === true
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function run() {
  if (!confirmed.value || running.value || !allowed.value) return
  running.value = true
  error.value = ''
  feedback.value = ''
  let result
  try {
    result = await request('tasks/extract/run', { method: 'POST', timeout: 105000 })
  } catch (cause) {
    error.value = cause.message
  } finally {
    running.value = confirmed.value = false
  }
  if (result) {
    await load()
    feedback.value = {
      SUCCESS: '本次 ODS 采集成功。',
      FAILED: '本次 ODS 采集失败，可查看下方任务状态。',
      INDETERMINATE: '本次采集结果待确认，请刷新查看任务状态。',
    }[result.outcome]
  }
}

onMounted(load)
</script>

<template>
  <header class="topbar">
    <nav class="topbar-inner" aria-label="任务导航">
      <RouterLink :to="{ name: 'outpatient' }">返回门诊驾驶舱</RouterLink>
      <RouterLink :to="{ name: 'ops-sources' }">数据源管理</RouterLink>
    </nav>
  </header>
  <main class="workspace">
    <section class="page-heading"><div><h1>任务管理</h1><p class="lead">查看最近运行的阶段、记录数和耗时。</p></div></section>
    <section class="panel filter-panel">
      <div class="section-heading ops-heading">
        <h2>任务状态</h2>
        <div class="filter-actions">
          <button class="button secondary" type="button" :disabled="loading || running" @click="load">刷新任务</button>
          <button v-if="allowed && !confirmed" class="button primary" type="button" @click="confirmed = true">手动采集</button>
        </div>
      </div>
      <div v-if="confirmed" class="filter-actions ops-feedback">
        <p role="status">{{ running ? '正在执行 ODS 采集…' : '确认使用当前登记批次执行一次 ODS 采集？' }}</p>
        <button class="button primary" type="button" :disabled="running || loading" @click="run">确认采集</button>
        <button class="button secondary" type="button" :disabled="running" @click="confirmed = false">取消</button>
      </div>
      <p v-if="feedback" class="ops-feedback" role="status">{{ feedback }}</p>
      <p v-if="error" class="report-error" role="alert">{{ error }}</p>
      <p v-if="loading" role="status" aria-busy="true">正在读取任务…</p>
      <div v-else-if="items.length" class="table-scroll" tabindex="0" aria-label="任务列表，可滚动查看">
        <table>
          <thead><tr><th scope="col">任务</th><th scope="col">最近状态</th><th scope="col">时间</th><th scope="col">记录数</th><th scope="col">耗时</th><th scope="col">结果码</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.name">
              <th scope="row">{{ names[item.name] }}</th>
              <td>{{ item.latest ? labels[item.latest.status] : '暂无近期记录' }}</td>
              <td>{{ item.latest ? format(item.latest.at) : '—' }}</td>
              <td>{{ item.latest?.rows ?? '—' }}</td>
              <td>{{ item.latest ? item.latest.cost_ms + ' ms' : '—' }}</td>
              <td>{{ item.latest?.code ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!error">暂无任务记录。</p>
    </section>
    <ScheduleEditor />
  </main>
</template>
