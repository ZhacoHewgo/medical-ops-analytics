<script setup>
import ThemeBoard from '../components/ThemeBoard.vue'
import RevenueDashboard from '../components/RevenueDashboard.vue'
import { requestWithSession } from '../api/request.js'

defineOptions({ name: 'RevenueView' })

const labels = {
  charge_amount: ['收费金额', '元'],
  charge_count: ['收费明细数', '条'],
  net_revenue: ['净医疗收入', '元'],
}

function loadData(session, query, options, onExpired) {
  return requestWithSession(session, `/api/v1/revenue/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard class="revenue-page" title="收入分析" source="ads_income_theme" :labels="labels" :load-data="loadData" loading-layout="revenue" :loading-cards="3">
    <template #dashboard="{ summary, drillable, select }">
      <RevenueDashboard :summary="summary" :drillable="drillable" @select="select" />
    </template>
  </ThemeBoard>
</template>

<style scoped>
.revenue-page :deep(.workspace-heading) { margin-bottom: 12px; }
.revenue-page :deep(.filter-panel) { margin-bottom: 14px; padding: 0; border: 0; background: transparent; box-shadow: none; }
.revenue-page :deep(.range-filter) { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.revenue-page :deep(.range-filter .field) { display: flex; gap: 8px; align-items: center; min-width: 0; }
.revenue-page :deep(.range-filter .field input) { width: 95px; min-width: 0; }
.revenue-page :deep(.range-filter summary) { white-space: nowrap; }
.revenue-page :deep(.range-filter .filter-actions) { margin: 0; }
</style>
