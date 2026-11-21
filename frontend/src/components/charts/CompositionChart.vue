<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { HeatmapChart, PieChart } from 'echarts/charts'
import { AriaComponent, GridComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartTheme } from '../../styles/theme.js'

use([HeatmapChart, PieChart, AriaComponent, GridComponent, TooltipComponent, VisualMapComponent, CanvasRenderer])

const props = defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
  kind: { type: String, required: true },
})
const container = ref(null)
const failed = ref(false)
let chart
let observer

function heatmap() {
  const dates = [...new Set(props.data.map(row => row.date))]
  const departments = [...new Set(props.data.map(row => row.dept_code))]
  return {
    grid: { top: 12, left: 30, right: 16, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'category', data: departments },
    visualMap: { min: 0, max: Math.max(1, ...props.data.map(row => row.visits)),
      calculable: true, orient: 'horizontal', left: 'center', bottom: 0 },
    series: [{ type: 'heatmap', data: props.data.map(row => [row.date, row.dept_code, row.visits]) }],
  }
}

function draw() {
  if (!chart) return
  try {
    const option = props.kind === 'heatmap' ? heatmap() : {
      series: [{ type: 'pie', radius: ['35%', '65%'],
        data: props.data.map(row => ({ name: row.dept_code, value: row.visits })) }],
    }
    chart.setOption({
      animation: false, aria: { enabled: true },
      color: props.theme.palette || chartTheme.palette,
      tooltip: { trigger: 'item', confine: true, renderMode: 'richText' },
      ...option,
    }, { notMerge: true })
    failed.value = false
  } catch {
    failed.value = true
  }
}

onMounted(() => {
  try {
    chart = init(container.value)
    observer = new ResizeObserver(() => chart.resize())
    observer.observe(container.value)
    draw()
  } catch {
    failed.value = true
  }
})
watch(() => [props.data, props.theme], draw)
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

<template>
  <div class="chart-scroll" :aria-busy="loading">
    <p v-if="failed" role="alert">图表暂不可用，请查看报表数据。</p>
    <p v-else-if="!data.length && !loading">暂无数据</p>
    <div ref="container" class="chart" style="height: 290px"></div>
  </div>
</template>
