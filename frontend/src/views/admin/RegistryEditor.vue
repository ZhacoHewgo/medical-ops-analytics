<script setup>
import { onMounted, reactive, ref, shallowRef } from 'vue'
import { useManagementRequest } from '../../api/admin.mjs'

defineProps({ disabled: Boolean })
const emit = defineEmits(['saved'])
const request = useManagementRequest('ops')
const state = shallowRef(null)
const form = reactive({ dataset_id: '', batch_date: '' })
const error = ref('')
const feedback = ref('')
const pending = ref(false)
const confirming = ref(false)

async function load() {
  if (pending.value) return
  confirming.value = false
  pending.value = true
  state.value = null
  error.value = ''
  try {
    const data = await request('sources/registration')
    if (typeof data.version !== 'string' || !Array.isArray(data.batches)) throw new Error('登记状态响应无效。')
    state.value = data
  } catch (cause) {
    error.value = cause.message
  } finally {
    pending.value = false
  }
}

function prepare() {
  if (pending.value || !state.value?.enabled) return
  error.value = feedback.value = ''
  confirming.value = true
}

async function save() {
  if (pending.value || !state.value?.enabled || !confirming.value) return
  pending.value = true
  try {
    const result = await request('sources/registration', {
      method: 'PUT', timeout: 45000,
      data: { ...form, expected_version: state.value.version },
    })
    feedback.value = result.changed ? '六类数据源登记已更新。' : '登记批次与当前一致。'
    emit('saved')
  } catch (cause) {
    error.value = cause.message
  } finally {
    pending.value = confirming.value = false
  }
  if (feedback.value) await load()
}

onMounted(load)
</script>

<template>
  <section class="panel filter-panel report-result" aria-labelledby="registry-title">
    <div class="section-heading ops-heading">
      <h2 id="registry-title">数据源批次登记</h2>
      <button class="button secondary" type="button" :disabled="pending" @click="load">刷新登记版本</button>
    </div>
    <p v-if="error" class="report-error" role="alert">{{ error }}</p>
    <p v-if="feedback" class="ops-feedback" role="status">{{ feedback }}</p>
    <p v-if="!state && pending" role="status" aria-busy="true">正在读取登记版本…</p>
    <p v-else-if="state && !state.enabled">当前环境未启用批次登记变更。</p>
    <template v-else-if="state">
      <p>当前登记</p>
      <ul v-if="state.batches.length" class="registry-batches">
        <li v-for="batch in state.batches" :key="batch.dataset_id + ':' + batch.batch_date">
          {{ batch.dataset_id }} · {{ batch.batch_date }} · {{ batch.sources }} 类
        </li>
      </ul>
      <p v-else>暂无已登记批次。</p>
      <form class="filter-form registry-form" @submit.prevent="prepare">
        <div class="field">
          <label for="registry-dataset">数据集代码</label>
          <input id="registry-dataset" v-model.trim="form.dataset_id" maxlength="128"
            pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}" :disabled="confirming || pending || disabled" required>
        </div>
        <div class="field">
          <label for="registry-date">登记批次日期</label>
          <input id="registry-date" v-model="form.batch_date" type="date" :disabled="confirming || pending || disabled" required>
        </div>
        <button v-if="!confirming" class="button primary" type="submit" :disabled="pending || disabled">继续登记</button>
      </form>
      <div v-if="confirming" class="filter-actions ops-feedback">
        <p role="status">确认将六类数据源统一登记到 {{ form.dataset_id }} · {{ form.batch_date }}？</p>
        <button class="button primary" type="button" :disabled="pending || disabled" @click="save">{{ pending ? '正在检查并登记…' : '确认登记' }}</button>
        <button class="button secondary" type="button" :disabled="pending" @click="confirming = false">取消登记</button>
      </div>
    </template>
  </section>
</template>
