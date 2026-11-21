<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { chartTheme } from '../styles/theme.js'

use([BarChart, LineChart, AriaComponent, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  kind: { type: String, required: true },
  rows: { type: Array, required: true },
  drillable: Boolean,
  label: { type: String, default: '门诊量' },
  unit: { type: String, default: '人次' },
  theme: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['select-row'])
const container = ref(null)
const chartError = ref(false)
const isRanking = computed(() => props.kind === 'ranking')
const height = computed(() => isRanking.value ? Math.max(280, props.rows.length * 34 + 48) : 280)
const departmentLabel = code => code === '__UNKNOWN__' ? '未标注科室（__UNKNOWN__）' : code
let chart
let observer

function draw() {
  if (!chart) return
  const ranking = isRanking.value
  const { primary, ranking: rankColor, text, grid } = { ...chartTheme, ...props.theme }
  const labels = props.rows.map(row => ranking ? departmentLabel(row.dept_code) : row.date)
  const values = props.rows.map(row => row.visits)
  const countAxis = { type: 'value', minInterval: props.unit === '元' ? 0.01 : 1, splitLine: { lineStyle: { color: grid } } }
  const categoryAxis = {
    type: 'category', data: labels, inverse: ranking, boundaryGap: ranking,
    axisLine: { lineStyle: { color: '#d4dfe6' } }, axisTick: { show: false },
    axisLabel: { color: text, width: ranking ? 128 : 90, overflow: 'truncate', hideOverlap: true },
  }
  try {
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: 'system-ui, sans-serif', color: text },
      aria: { enabled: true, label: { description: ranking ? `科室${props.label}排行，完整数值见下方数据表。` : `有效记录日的${props.label}趋势，完整数值见下方数据表。` } },
      tooltip: { trigger: 'axis', renderMode: 'richText', confine: true, valueFormatter: value => `${value} ${props.unit}` },
      grid: { top: 16, right: 24, bottom: 24, left: 12, containLabel: true },
      xAxis: ranking ? countAxis : categoryAxis,
      yAxis: ranking ? categoryAxis : countAxis,
      series: [{
        name: props.label, type: ranking ? 'bar' : 'line', data: values,
        barMaxWidth: 16, showSymbol: props.drillable || values.length === 1, symbolSize: 8,
        cursor: props.drillable ? 'pointer' : 'default',
        itemStyle: { color: ranking ? rankColor : primary, borderRadius: ranking ? [0, 4, 4, 0] : 0 },
        lineStyle: { width: 3 }, areaStyle: ranking ? undefined : { color: '#117c80', opacity: 0.07 },
      }],
    }, { notMerge: true })
    chartError.value = false
  } catch {
    chartError.value = true
  }
}

onMounted(() => {
  try {
    chart = init(container.value)
    if (props.drillable) {
      chart.on('click', 'series', ({ dataIndex }) => {
        const row = props.rows[dataIndex]
        if (row) emit('select-row', row)
      })
    }
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(container.value)
    draw()
  } catch {
    chartError.value = true
  }
})
watch(() => [props.rows, props.theme], draw)
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

<template>
  <div class="chart-scroll" tabindex="0" :aria-label="isRanking ? '科室排行图，可滚动查看' : `${label}日趋势图`">
    <div ref="container" class="chart" :style="{ height: `${height}px` }"></div>
  </div>
  <p v-if="chartError" role="alert" class="chart-error">图表暂未绘制，请使用下方数据表查看真实结果。</p>
  <details class="chart-table">
    <summary>查看{{ isRanking ? '科室排行' : '日趋势' }}数据表（{{ rows.length }} 项）</summary>
    <div class="table-scroll" tabindex="0" aria-label="图表数据表，可滚动查看">
      <table>
        <caption>{{ isRanking ? `科室${label}排行` : `有效记录日的${label}` }}</caption>
        <thead><tr><th scope="col">{{ isRanking ? '科室代码' : '日期' }}</th><th scope="col">{{ label }}（{{ unit }}）</th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="isRanking ? row.dept_code : row.date">
            <th scope="row">
              <button v-if="drillable" class="button secondary" type="button" @click="emit('select-row', row)">{{ isRanking ? departmentLabel(row.dept_code) : row.date }}</button>
              <span v-else>{{ isRanking ? departmentLabel(row.dept_code) : row.date }}</span>
            </th>
            <td>{{ row.visits?.toLocaleString('zh-CN') ?? '暂缺' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
</template>
