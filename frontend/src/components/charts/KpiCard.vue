<script setup>
import MotionNumber from './MotionNumber.vue'

defineProps({
  data: { type: Object, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
})
</script>

<template>
  <article class="panel kpi-card" :aria-busy="loading" :style="{ color: theme.text }">
    <div class="kpi-label"><h2>{{ data.label }}</h2><span v-if="data.scope" class="scope-pill">{{ data.scope }}</span></div>
    <p class="kpi-value" :style="{ color: theme.primary }">
      <MotionNumber v-if="!loading && data.value != null" :value="data.value" />
      <template v-else>{{ loading ? '读取中' : '暂无数据' }}</template><span v-if="!loading && data.value != null">{{ data.unit }}</span>
    </p>
    <p v-if="Number.isFinite(data.change)">变化 {{ data.change > 0 ? '↑' : data.change < 0 ? '↓' : '—' }} {{ data.change }}%</p>
    <p v-if="data.description">{{ data.description }}</p>
  </article>
</template>
