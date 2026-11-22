<script setup>
import { ref, watch } from 'vue'
import WorkspaceIcon from './WorkspaceIcon.vue'

const props = defineProps({ disabled: Boolean, min: String, max: String })
const start = defineModel('start', { type: String, required: true })
const end = defineModel('end', { type: String, required: true })
const details = ref(null)
const trigger = ref(null)

function close() {
  details.value.open = false
  trigger.value.focus()
}

watch(() => props.disabled, disabled => {
  if (disabled) details.value.open = false
})
</script>

<template>
  <details ref="details" class="overview-date-range" @keydown.esc.stop="close">
    <summary ref="trigger" class="overview-filter-group" :aria-disabled="disabled"
      @click="disabled && $event.preventDefault()">
      <WorkspaceIcon name="calendar" />
      <span class="report-visually-hidden">日期范围</span>
      <span>{{ start || '开始日期' }} — {{ end || '结束日期' }}</span>
    </summary>
    <div class="overview-date-panel" role="group" aria-label="选择日期范围">
      <label>开始日期<input v-model="start" type="date" :min="min" :max="max" :disabled="disabled"></label>
      <label>结束日期<input v-model="end" type="date" :min="min" :max="max" :disabled="disabled"></label>
      <button type="button" class="button secondary" @click="close">确定</button>
    </div>
  </details>
</template>
