<script setup>
import App from '../App.vue'
import { request } from '../api/request.js'

defineOptions({ name: 'DashboardView' })

const preview = new URLSearchParams(window.location.search).get('source') === 'preview'
const endpoint = preview ? '/api/dashboard/outpatient-preview' : '/api/dashboard/overview'

function loadOverview(path, options) {
  return request(path, options)
}
</script>

<template>
  <App :endpoint="endpoint" :preview="preview" :load-response="loadOverview" />
</template>
