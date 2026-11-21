<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { buildQuery } from '../api/dashboard.mjs'
import { useSession } from '../store/user.js'
import TrendChart from './charts/TrendChart.vue'

const props = defineProps({
  title: { type: String, required: true },
  period: { type: String, required: true },
  source: { type: String, required: true },
  labels: { type: Object, required: true },
  loadData: { type: Function, required: true },
})

const router = useRouter()
const session = useSession()
const department = session.current?.user.department ?? null
const filters = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const data = shallowRef(null)
const trail = ref([])
const busy = ref(false)
const error = ref('')
const chartRows = computed(() => data.value?.group_by === 'day'
  ? data.value.rows.map(row => ({ date: row.group, visits: row.value === null ? null : Number(row.value) }))
  : [])
const nullLabels = {
  INCOMPLETE_INPUT: '收费记录金额不完整',
  NET_REVENUE_RULE_UNCONFIRMED: '净收入口径待确认',
  OFFICIAL_DRG_GROUPING_MISSING: '缺正式 DRG 分组结果',
  DRG_GROUPING_AND_WEIGHTS_MISSING: '缺 DRG 分组和权重',
  BED_CAPACITY_MISSING: '缺开放床位台账',
  BED_OCCUPANCY_RULE_UNCONFIRMED: '占用床日口径待确认',
}
let pending

async function load(selection = filters, drill = null) {
  pending?.abort()
  const controller = pending = new AbortController()
  const selected = { ...selection, dept_code: department || selection.dept_code }
  busy.value = true
  error.value = ''
  data.value = null
  try {
    const query = buildQuery(selected)
    const params = drill ? `${query || '?'}${query ? '&' : ''}drill=${drill}` : query
    const response = await props.loadData(session, params, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    if (controller.signal.aborted) return
    if (!response.ok) {
      error.value = {
        401: '请重新登录。',
        403: '当前账号无权查看所选科室。',
        422: '请检查日期及科室范围。',
        503: '汇总数据暂不可用，请核对发布状态。',
      }[response.status] || '暂时无法读取汇总，请重试。'
      return
    }
    const payload = await response.json()
    if (controller.signal.aborted) return
    const result = payload?.data
    if (payload?.code !== 0 || result?.meta?.source !== props.source
        || result.filters?.dept_code !== (selected.dept_code || null)
        || (selected.start_date && result.filters.start_date !== selected.start_date)
        || (selected.end_date && result.filters.end_date !== selected.end_date)
        || result.group_by !== (drill || 'day') || !Array.isArray(result.rows)
        || !Object.hasOwn(props.labels, result.metric) || !result.metrics
        || Object.keys(props.labels).some(key => !Object.hasOwn(result.metrics, key))
        || Object.keys(result.metrics).some(key => !Object.hasOwn(props.labels, key))) {
      throw new Error('汇总响应与请求范围不一致。')
    }
    data.value = result
    Object.assign(filters, result.filters, { dept_code: result.filters.dept_code || '' })
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause?.name === 'Error' && cause.message === '汇总响应与请求范围不一致。'
        ? cause.message : '暂时无法读取汇总，请检查日期或重试。'
    }
  } finally {
    if (pending === controller) busy.value = false
  }
}

function apply() {
  trail.value = []
  return load()
}

function canDrill() {
  const result = data.value
  if (result.group_by === 'department') return !result.filters.dept_code
  return !result.filters.dept_code || result.filters.start_date !== result.filters.end_date
}

function handleDrill(group) {
  if (!canDrill()) return
  const current = data.value
  const scope = { ...current.filters }
  trail.value.push({ scope, drill: current.group_by === 'department' ? 'department' : null })
  if (current.group_by === 'department') return load({ ...scope, dept_code: group })
  return load({ ...scope, start_date: group, end_date: group }, scope.dept_code ? null : 'department')
}

function back() {
  const item = trail.value.pop()
  if (item) return load(item.scope, item.drill)
}

function display(item, unit) {
  if (item.value === null) return nullLabels[item.null_reason] || '暂无可用结果'
  return unit ? `${item.value} ${unit}` : item.value
}

onMounted(apply)
onBeforeUnmount(() => pending?.abort())
</script>

<template>
  <header class="topbar">
    <div class="topbar-inner">
      <span class="brand">医疗运营工作台</span>
      <nav class="account-actions" aria-label="主题看板">
        <RouterLink :to="{ name: 'outpatient' }">门诊</RouterLink>
        <RouterLink :to="{ name: 'inpatient' }">住院</RouterLink>
        <RouterLink :to="{ name: 'revenue' }">收入</RouterLink>
        <RouterLink :to="{ name: 'disease' }">病种</RouterLink>
        <RouterLink :to="{ name: 'resource' }">资源效率</RouterLink>
      </nav>
    </div>
  </header>
  <main class="workspace">
    <div class="page-heading"><div><h1>{{ title }}</h1><p class="lead">{{ period }}</p></div></div>
    <section class="panel filter-panel" aria-label="筛选范围">
      <form class="filter-form" @submit.prevent="apply">
        <div class="field"><label for="theme-start">开始日期</label><input id="theme-start" v-model="filters.start_date" type="date"></div>
        <div class="field"><label for="theme-end">结束日期</label><input id="theme-end" v-model="filters.end_date" type="date"></div>
        <div class="field"><label for="theme-dept">科室代码</label><input id="theme-dept" v-model.trim="filters.dept_code" placeholder="全院" maxlength="64" :readonly="Boolean(department)"></div>
        <div class="filter-actions">
          <button type="submit" class="button primary" :disabled="busy">查询</button>
          <button v-if="trail.length" type="button" class="button secondary" :disabled="busy" @click="back">返回上层</button>
        </div>
      </form>
    </section>
    <p v-if="busy" class="status-announcer" role="status">正在读取汇总…</p>
    <p v-if="error" class="status-announcer" role="alert">{{ error }}</p>
    <section v-if="data" class="panel scope-card" aria-label="汇总结果">
      <h2>{{ data.filters.dept_code || '全院' }} · {{ data.filters.start_date }} 至 {{ data.filters.end_date }}</h2>
      <dl>
        <div v-for="(item, key) in data.metrics" :key="key">
          <dt>{{ labels[key][0] }}</dt><dd>{{ display(item, labels[key][1]) }}</dd>
        </div>
      </dl>
      <TrendChart v-if="chartRows.some(row => row.visits !== null)" :data="chartRows"
                  :label="labels[data.metric][0]" :unit="labels[data.metric][1]" :drillable="canDrill()"
                  @select-row="row => handleDrill(row.date)" />
      <div v-if="data.rows.length" class="table-scroll">
        <table>
          <caption>{{ data.group_by === 'day' ? '按日明细' : '按科室明细' }}</caption>
          <thead><tr><th scope="col">{{ data.group_by === 'day' ? '日期' : '科室代码' }}</th><th scope="col">{{ labels[data.metric][0] }}</th></tr></thead>
          <tbody>
            <tr v-for="row in data.rows" :key="row.group">
              <th scope="row">
                <button v-if="canDrill()" type="button" class="button secondary" @click="handleDrill(row.group)">{{ row.group }}</button>
                <span v-else>{{ row.group }}</span>
              </th>
              <td>{{ display(row, labels[data.metric][1]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else>当前范围没有汇总记录。</p>
    </section>
  </main>
</template>
