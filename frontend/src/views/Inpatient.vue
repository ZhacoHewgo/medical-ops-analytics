<script setup>
import { onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { requestWithSession } from '../api/request.js'
import { useSession } from '../store/user.js'

defineOptions({ name: 'InpatientView' })

const router = useRouter()
const session = useSession()
const department = session.current?.user.department
const filters = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const data = shallowRef(null)
const busy = ref(false)
const error = ref('')
let pending

async function load() {
  pending?.abort()
  const controller = pending = new AbortController()
  busy.value = true
  error.value = ''
  data.value = null
  try {
    const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value))
    const response = await requestWithSession(session, `/api/v1/inpatient/summary?${query}`, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    if (controller.signal.aborted) return
    if (!response.ok) {
      error.value = response.status === 422 ? '请检查日期及科室范围。' : '住院汇总暂不可用。'
      return
    }
    const payload = await response.json()
    if (controller.signal.aborted) return
    if (payload.code !== 0 || !payload.data?.filters) throw new Error('Invalid inpatient summary')
    data.value = payload.data
    const actual = payload.data.filters
    Object.assign(filters, actual, { dept_code: actual.dept_code || '' })
  } catch {
    if (!controller.signal.aborted) error.value = '暂时无法读取住院汇总，请重试。'
  } finally {
    if (pending === controller) busy.value = false
  }
}

function handleDrill(day) {
  filters.start_date = filters.end_date = day
  return load()
}

function display(metric, unit) {
  if (metric.value !== null) return `${Number(metric.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${unit}`
  return metric.null_reason === 'ZERO_BASE' ? '无出院记录' : '输入不完整'
}

onMounted(load)
onBeforeUnmount(() => pending?.abort())
</script>

<template>
  <header class="topbar">
    <div class="topbar-inner">
      <span class="brand">医疗运营工作台</span>
      <RouterLink :to="{ name: 'outpatient' }">返回门诊看板</RouterLink>
    </div>
  </header>
  <main class="workspace">
    <div class="page-heading">
      <div>
        <h1>住院运营</h1>
        <p class="lead">课程样例 · 按出院日期汇总</p>
      </div>
    </div>
    <section class="panel filter-panel" aria-label="筛选住院数据">
      <form class="filter-form" @submit.prevent="load">
        <div class="field"><label for="inpatient-start">开始日期</label><input id="inpatient-start" v-model="filters.start_date" type="date"></div>
        <div class="field"><label for="inpatient-end">结束日期</label><input id="inpatient-end" v-model="filters.end_date" type="date"></div>
        <div class="field"><label for="inpatient-dept">科室代码</label><input id="inpatient-dept" v-model.trim="filters.dept_code" placeholder="全院" maxlength="64" :readonly="Boolean(department)"></div>
        <button type="submit" class="button primary" :disabled="busy">查询</button>
      </form>
    </section>
    <p v-if="busy" class="status-announcer" role="status">正在读取住院汇总…</p>
    <p v-if="error" class="status-announcer" role="alert">{{ error }}</p>
    <section v-if="data" class="panel scope-card" aria-label="住院汇总">
      <h2>{{ data.filters.dept_code || '全院' }} · {{ data.filters.start_date }} 至 {{ data.filters.end_date }}</h2>
      <dl>
        <div><dt>出院人次</dt><dd>{{ data.discharges.toLocaleString('zh-CN') }}</dd></div>
        <div><dt>平均住院日（样例源字段）</dt><dd>{{ display(data.avg_stay_days, '天') }}</dd></div>
        <div><dt>住院次均费用（样例总费用）</dt><dd>{{ display(data.avg_cost, '元/人次') }}</dd></div>
      </dl>
      <div v-if="data.daily_trend.length" class="table-scroll">
        <table>
          <caption>每日出院人次</caption>
          <thead><tr><th scope="col">出院日期</th><th scope="col">人次</th></tr></thead>
          <tbody><tr v-for="row in data.daily_trend" :key="row.date"><th scope="row"><button type="button" class="button secondary" @click="handleDrill(row.date)">{{ row.date }}</button></th><td>{{ row.discharges }}</td></tr></tbody>
        </table>
      </div>
    </section>
  </main>
</template>
