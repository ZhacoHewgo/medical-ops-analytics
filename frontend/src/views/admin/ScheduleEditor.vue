<script setup>
import { onMounted, reactive, ref, shallowRef } from 'vue'
import { useManagementRequest } from '../../api/admin.mjs'

const request = useManagementRequest('ops')
const state = shallowRef(null)
const draft = reactive({ enabled: true, time: '02:00' })
const error = ref('')
const notice = ref('')
const pending = ref(false)
const confirming = ref(false)
const format = (hour, minute) => [hour, minute].map(value => String(value).padStart(2, '0')).join(':')

function accept(data) {
  state.value = data
  Object.assign(draft, { enabled: data.enabled, time: format(data.hour, data.minute) })
}

async function load() {
  if (pending.value) return
  pending.value = true
  confirming.value = false
  error.value = notice.value = ''
  state.value = null
  try {
    accept(await request('tasks/schedule'))
  } catch (cause) {
    error.value = cause.message
  } finally {
    pending.value = false
  }
}

function prepare() {
  if (pending.value || !state.value?.can_edit) return
  error.value = notice.value = ''
  confirming.value = true
}

async function save() {
  if (pending.value || !state.value?.can_edit || !confirming.value) return
  pending.value = true
  const [hour, minute] = draft.time.split(':').map(Number)
  try {
    const result = await request('tasks/schedule', {
      method: 'PUT', data: { enabled: draft.enabled, hour, minute, expected_version: state.value.version },
    })
    accept(result)
    notice.value = result.changed ? '每日采集配置已保存。' : '配置与当前设置一致。'
  } catch (cause) {
    error.value = cause.message
  } finally {
    pending.value = confirming.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="panel filter-panel report-result" aria-labelledby="schedule-title">
    <div class="section-heading ops-heading">
      <h2 id="schedule-title">每日采集配置</h2>
      <button class="button secondary" type="button" :disabled="pending" @click="load">刷新采集配置</button>
    </div>
    <p v-if="error" class="report-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="ops-feedback" role="status">{{ notice }}</p>
    <p v-if="pending && !state" role="status">正在读取每日采集配置…</p>
    <template v-if="state">
      <p>当前配置 {{ state.enabled ? '启用' : '停用' }} · 每日 {{ format(state.hour, state.minute) }}（北京时间）</p>
      <p class="muted">前台托管调度进程读取此配置后生效。</p>
      <form v-if="state.can_edit" class="filter-form schedule-form" @submit.prevent="prepare">
        <div class="field">
          <label for="schedule-enabled">每日采集</label>
          <select id="schedule-enabled" v-model="draft.enabled" :disabled="pending || confirming">
            <option :value="true">启用</option><option :value="false">停用</option>
          </select>
        </div>
        <div class="field">
          <label for="schedule-time">每日执行时间</label>
          <input id="schedule-time" v-model="draft.time" type="time" step="60" required :disabled="pending || confirming">
        </div>
        <button v-if="!confirming" class="button primary" type="submit" :disabled="pending">继续更新配置</button>
      </form>
      <div v-if="confirming" class="filter-actions ops-feedback">
        <p role="status">{{ draft.enabled ? '确认启用每日采集，执行时间为北京时间 ' + draft.time + '？' : '确认停止后续的每日采集？' }}</p>
        <button class="button primary" type="button" :disabled="pending" @click="save">确认更新配置</button>
        <button class="button secondary" type="button" :disabled="pending" @click="confirming = false">取消更新</button>
      </div>
    </template>
  </section>
</template>
