<script setup>
import { computed, onDeactivated, ref, shallowRef } from 'vue'
import { importOverviewCsv, overviewCsvTemplate } from '../api/overviewImport.mjs'

const props = defineProps({ disabled: Boolean, previous: Object, cutoff: String, apply: { type: Function, required: true } })
const dialog = ref(null)
const picker = ref(null)
const files = shallowRef([])
const busy = ref(false)
const dragging = ref(false)
const error = ref('')
const templateHref = computed(() => overviewCsvTemplate(props.cutoff))

function open() {
  files.value = []
  error.value = ''
  dragging.value = false
  picker.value.value = ''
  dialog.value.showModal()
}

function select(list) {
  dragging.value = false
  if (busy.value) return
  const chosen = [...list]
  files.value = []
  error.value = ''
  if (chosen.some(file => !/\.csv$/i.test(file.name))) {
    error.value = '请选择 CSV 文件。'
  } else if (chosen.length > 12 || chosen.reduce((size, file) => size + file.size, 0) > 16 * 1024 * 1024) {
    error.value = '一次最多选择 12 个文件，合计不超过 16 MB。'
  } else {
    files.value = chosen
  }
}

async function submit() {
  if (!files.value.length || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const decoder = new TextDecoder('utf-8', { fatal: true })
    const texts = await Promise.all(files.value.map(async file => decoder.decode(await file.arrayBuffer())))
    const imported = importOverviewCsv(texts, props.previous)
    if (dialog.value?.open) {
      await props.apply(imported)
      dialog.value?.close()
    }
  } catch (cause) {
    error.value = cause.constructor === Error ? cause.message : '读取失败，请将文件另存为 UTF-8 CSV 后重试。'
  } finally {
    busy.value = false
  }
}

onDeactivated(() => dialog.value?.close())
</script>

<template>
  <button type="button" class="button secondary" :disabled="disabled || busy" @click="open">导入 CSV</button>
  <dialog ref="dialog" class="overview-csv-dialog" aria-labelledby="overview-csv-title"
    @cancel="busy && $event.preventDefault()" @close="files = []">
    <h2 id="overview-csv-title">导入 CSV 数据</h2>
    <p>追加 {{ cutoff }} 之后的运营汇总或业务明细</p>
    <input ref="picker" hidden type="file" accept=".csv,text/csv" multiple
      @change="select($event.target.files); $event.target.value = ''">
    <button type="button" class="csv-drop" :class="{ 'csv-dragging': dragging }" :disabled="busy"
      @click="picker.click()" @dragover.prevent="dragging = true" @dragleave="dragging = false"
      @drop.prevent="select($event.dataTransfer.files)">
      <strong>{{ files.length ? files.map(file => file.name).join('、') : '选择或拖入 CSV 文件' }}</strong>
    </button>
    <div v-if="templateHref" class="csv-template">
      <a :href="templateHref" download="testdata.csv">下载模拟数据</a>
    </div>
    <p v-if="error" class="query-error" role="alert">{{ error }}</p>
    <div class="csv-actions">
      <button type="button" class="button secondary" :disabled="busy" @click="dialog.close()">取消</button>
      <button type="button" class="button primary" :disabled="!files.length || busy" @click="submit">
        {{ busy ? '正在导入…' : '开始导入' }}
      </button>
    </div>
  </dialog>
</template>

<style scoped>
.overview-csv-dialog {
  width: min(28rem, calc(100vw - 32px)); padding: 24px; border: 1px solid var(--border);
  border-radius: 16px; box-shadow: 0 20px 72px #12213a33; color: var(--ink);
}
.overview-csv-dialog::backdrop { background: #13203966; }
.overview-csv-dialog h2 { margin: 0 0 6px; font-size: 1.25rem; }
.overview-csv-dialog p { margin: 0 0 16px; font-size: .875rem; }
.csv-drop {
  display: block; width: 100%; min-height: 100px; padding: 16px; border: 1px dashed var(--border);
  border-radius: 12px; background: var(--surface-tint); color: var(--ink); cursor: pointer; overflow-wrap: anywhere;
}
.csv-drop:is(:hover, :focus-visible, .csv-dragging) { border-color: currentColor; }
.csv-template, .csv-actions { margin-top: 14px; }
.csv-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.csv-template a { color: inherit; font-size: .84rem; text-underline-offset: 3px; }
.csv-actions { justify-content: flex-end; }
.overview-csv-dialog .query-error { margin: 14px 0 0; }
</style>
