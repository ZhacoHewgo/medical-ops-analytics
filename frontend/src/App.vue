<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, shallowRef } from 'vue'
import DashboardChart from './components/DashboardChart.vue'
import { createDashboardLoader } from './api/dashboard.mjs'

const state = shallowRef({ status: 'idle', data: null, error: '', requestedFilters: {} })
const meta = shallowRef(null)
const draft = reactive({ start_date: '', end_date: '', dept_code: '' })
const preview = new URLSearchParams(window.location.search).get('source') === 'preview'
const departmentLabel = code => code === '__UNKNOWN__' ? '未标注科室（__UNKNOWN__）' : code
const formatCount = count => count.toLocaleString('zh-CN')
const loader = createDashboardLoader({
  source: preview ? 'preview' : 'csv',
  onChange: value => {
    state.value = value
    if (value.status === 'success') meta.value = value.data.meta
  },
})
const data = computed(() => state.value.data)
const busy = computed(() => state.value.status === 'loading')
const scopeLabel = filters => `${filters.start_date || '默认最早日'} 至 ${filters.end_date || '默认最晚日'} · ${filters.dept_code ? departmentLabel(filters.dept_code) : '全院科室'}`
const qualityItems = [
  ['raw_rows', '原始行数', '全源输入记录'],
  ['accepted_visits', '保留就诊', '有效且唯一的就诊记录'],
  ['invalid_id_rows', '无效就诊标识', '空标识记录已隔离'],
  ['invalid_date_rows', '无效日期', '格式或日历日期不合法'],
  ['conflicting_id_rows', '冲突记录', '同一标识字段冲突，整组隔离'],
  ['duplicate_rows_removed', '移除重复', '规范化后完全一致的重复行'],
  ['unknown_department_visits', '未标注科室', '保留就诊的子集，仍计入门诊量'],
]

async function loadDefault() {
  const result = await loader.load({})
  if (result) Object.assign(draft, { ...result.filters, dept_code: result.filters.dept_code || '' })
}

async function retry() {
  const result = await loader.retry()
  if (result) Object.assign(draft, { ...result.filters, dept_code: result.filters.dept_code || '' })
}

onMounted(loadDefault)
onBeforeUnmount(loader.dispose)
</script>

<template>
  <a class="skip-link" href="#main">跳到门诊数据</a>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand"><span class="brand-mark" aria-hidden="true">+</span><span>MOPS <span class="brand-divider">/</span> 医疗运营工作台</span></div>
      <span class="demo-badge"><span aria-hidden="true" class="status-dot"></span>{{ preview ? '节点 8 宽表 · 本地只读预览' : '课程样例数据 · 本地演示' }}</span>
    </div>
  </header>

  <main id="main" class="workspace" tabindex="-1">
    <section class="page-heading" aria-labelledby="page-title">
      <div><p class="eyebrow">OUTPATIENT OVERVIEW</p><h1 id="page-title">门诊驾驶舱</h1><p class="lead">看清门诊规模、每日变化与科室分布。</p><a :href="preview ? '/' : '/?source=preview'">{{ preview ? '返回 CSV 看板' : '打开宽表预览' }}</a></div>
      <div v-if="meta" class="available-range"><span>可用数据日期</span><strong>{{ meta.available_start_date }} — {{ meta.available_end_date }}</strong></div>
    </section>

    <section class="panel filter-panel" aria-labelledby="filter-title">
      <div class="section-heading"><h2 id="filter-title">筛选范围</h2><p id="filter-help">编辑后点击应用；日期含首尾，留空使用数据边界。</p></div>
      <form class="filter-form" @submit.prevent="loader.load({ ...draft })" aria-describedby="filter-help">
        <div class="field"><label for="start-date">开始日期</label><input id="start-date" v-model="draft.start_date" type="date" /></div>
        <div class="field"><label for="end-date">结束日期</label><input id="end-date" v-model="draft.end_date" type="date" /></div>
        <div class="field department-field"><label for="department">科室代码</label><select id="department" v-model="draft.dept_code" :disabled="!meta"><option value="">全院科室</option><option v-for="code in meta?.department_codes || []" :key="code" :value="code">{{ departmentLabel(code) }}</option></select></div>
        <div class="filter-actions"><button type="submit" class="button primary" :disabled="!meta">应用筛选</button><button type="button" class="button secondary" @click="loadDefault">重置</button></div>
      </form>
    </section>

    <div class="status-announcer" role="status" aria-live="polite" aria-atomic="true">
      <span v-if="busy">正在读取：{{ scopeLabel(state.requestedFilters) }}。旧结果已清空。</span>
      <span v-else-if="data">当前生效：{{ scopeLabel(data.filters) }}<span v-if="data.outpatient_visits === 0"> · 当前范围暂无有效门诊记录</span></span>
      <span v-else-if="state.status === 'error'">当前无生效结果；请求范围：{{ scopeLabel(state.requestedFilters) }}</span>
      <span v-else>准备读取门诊数据…</span>
    </div>

    <section v-if="busy || state.status === 'idle'" class="panel state-panel" aria-busy="true" aria-labelledby="loading-title">
      <span class="loading-mark" aria-hidden="true">···</span><h2 id="loading-title">正在读取门诊数据</h2><p>从本地服务获取真实聚合响应，请稍候。</p>
    </section>
    <section v-else-if="state.status === 'error'" class="panel state-panel error-panel" aria-labelledby="error-title" role="alert">
      <span class="state-symbol" aria-hidden="true">!</span><h2 id="error-title">门诊数据暂不可用</h2><p>{{ state.error }}</p><p class="muted">当前不显示门诊数字。请确认本地数据加工与后端服务已就绪。</p><button type="button" class="button primary" @click="retry">重试本次请求</button>
    </section>

    <template v-else-if="data">
      <section class="overview-row" aria-label="当前范围门诊量">
        <article class="panel kpi-card"><div class="kpi-label"><h2>门诊量</h2><span class="scope-pill">{{ data.filters.dept_code ? '单科室' : '全院' }}</span></div><p class="kpi-value">{{ formatCount(data.outpatient_visits) }}<span>人次</span></p><p>按保留的唯一就诊记录计数，不代表唯一患者数。</p></article>
        <article class="panel scope-card"><p class="eyebrow">APPLIED SCOPE</p><h2>当前生效范围</h2><dl><div><dt>日期</dt><dd>{{ data.filters.start_date }} 至 {{ data.filters.end_date }}</dd></div><div><dt>科室</dt><dd>{{ data.filters.dept_code ? departmentLabel(data.filters.dept_code) : '全院科室' }}</dd></div></dl><p class="muted">仅展示有有效记录的日期；缺失日期不补零，科室仅按代码区分。</p></article>
      </section>

      <section v-if="data.outpatient_visits === 0" class="panel state-panel empty-panel" aria-labelledby="empty-title"><span class="state-symbol" aria-hidden="true">—</span><h2 id="empty-title">当前范围暂无有效门诊记录</h2><p>服务已成功返回空结果，门诊量为 0。可调整日期或选择全院科室。</p><button class="button secondary" type="button" @click="loadDefault">查看全部数据</button></section>

      <div class="charts-grid">
        <section class="panel chart-panel" aria-labelledby="trend-title"><div class="section-heading"><div><h2 id="trend-title">门诊日趋势</h2><p>按有效记录日展示 · 单位：人次</p></div><span class="chart-tag">日粒度</span></div><DashboardChart v-if="data.daily_trend.length" kind="trend" :rows="data.daily_trend" /><p v-else class="chart-empty">当前筛选无日趋势记录</p></section>
        <section class="panel chart-panel" aria-labelledby="ranking-title"><div class="section-heading"><div><h2 id="ranking-title">科室门诊量排行</h2><p>门诊量降序，同值按代码升序 · 单位：人次</p></div></div><DashboardChart v-if="data.department_ranking.length" kind="ranking" :rows="data.department_ranking" /><p v-else class="chart-empty">当前筛选无科室排行记录</p></section>
      </div>

      <section v-if="preview" class="panel quality-panel" aria-labelledby="build-title">
        <h2 id="build-title">宽表来源与构建</h2>
        <dl class="quality-grid">
          <div class="quality-item"><dt>来源表</dt><dd>{{ data.meta.source }}</dd></div>
          <div class="quality-item"><dt>批次</dt><dd>{{ data.meta.batch_date }}</dd></div>
          <div class="quality-item"><dt>构建号</dt><dd class="hash">{{ data.meta.build_run_id }}</dd></div>
        </dl>
      </section>
      <section v-else class="panel quality-panel" aria-labelledby="quality-title"><div class="section-heading"><div><h2 id="quality-title">数据质量摘要</h2><p>全源统计，不随上方日期与科室筛选变化。</p></div><span class="scope-pill">全源</span></div><dl class="quality-grid"><div v-for="[key, label, hint] in qualityItems" :key="key" class="quality-item"><dt>{{ label }}</dt><dd>{{ formatCount(data.meta.quality[key]) }}</dd><p>{{ hint }}</p></div></dl><p class="quality-note">原始行数 = 无效标识 + 无效日期 + 冲突记录 + 移除重复 + 保留就诊；未标注科室属于保留就诊的子集。</p>
        <details class="source-details"><summary>数据来源与生成信息</summary><dl><div><dt>来源文件</dt><dd>{{ data.meta.source_file }}</dd></div><div><dt>生成时间</dt><dd>{{ data.meta.generated_at }}</dd></div><div><dt>源文件 SHA256</dt><dd class="hash">{{ data.meta.source_sha256 }}</dd></div></dl></details>
      </section>
    </template>

    <footer class="page-footer"><p>仅供课程样例本地演示，不代表已连接真实医院或完成课程正式验收。</p><p>样例模式不代表已独立验证原始数据的生成方式。</p></footer>
  </main>
</template>
