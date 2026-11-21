<script setup>
import ThemeBoard from '../components/ThemeBoard.vue'
import { requestWithSession } from '../api/request.js'

defineOptions({ name: 'ResourceView' })

const labels = {
  discharges: ['出院人次', '人次'],
  available_bed_days: ['实际开放床日', '床日'],
  occupied_bed_days: ['实际占用床日', '床日'],
  bed_usage_rate: ['床位使用率', '%'],
}

function loadData(session, query, options, onExpired) {
  return requestWithSession(session, `/api/v1/resource/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard title="资源效率看板" period="按出院日期汇总" source="ads_efficiency_theme" :labels="labels" :load-data="loadData" />
</template>
