<script setup>
import ThemeBoard from '../components/ThemeBoard.vue'
import { requestWithSession } from '../api/request.js'

defineOptions({ name: 'DiseaseView' })

const labels = {
  source_diagnosis_cases: ['有源诊断编码的出院人次', '人次'],
  discharges: ['出院人次', '人次'],
  drg_grouped_cases: ['DRG 入组病例', '例'],
  cmi: ['病例组合指数（CMI）', ''],
}

function loadData(session, query, options, onExpired) {
  return requestWithSession(session, `/api/v1/disease/summary${query}`, options, onExpired)
}
</script>

<template>
  <ThemeBoard title="病种看板" period="按出院日期汇总" source="ads_drg_theme" :labels="labels" :load-data="loadData" />
</template>
