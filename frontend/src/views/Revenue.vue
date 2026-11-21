<script setup>
import ThemeBoard from '../components/ThemeBoard.vue'
import { requestWithSession } from '../api/request.js'

defineOptions({ name: 'RevenueView' })

const labels = {
  charge_amount: ['已记录收费额', '元'],
  charge_count: ['收费明细数', '条'],
  net_revenue: ['净医疗收入', '元'],
}

function loadData(session, query, options, onExpired) {
  return requestWithSession(session, `/api/v1/revenue/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard title="收入看板" period="按收费日期汇总" source="ads_income_theme" :labels="labels" :load-data="loadData" />
</template>
