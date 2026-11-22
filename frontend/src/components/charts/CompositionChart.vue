<script setup>
import { onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { HeatmapChart, PieChart } from 'echarts/charts'
import { AriaComponent, GridComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartMotionOptions } from '../../styles/chartMotion.mjs'
import { chartTheme } from '../../styles/theme.js'
import { useAnimatedChart } from './useAnimatedChart.mjs'

use([HeatmapChart, PieChart, AriaComponent, GridComponent, TooltipComponent, VisualMapComponent, CanvasRenderer])

const props = defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  drillable: Boolean,
  theme: { type: Object, default: () => ({}) },
  kind: { type: String, required: true },
})
const emit = defineEmits(['select-row'])
const container = ref(null)
const failed = ref(false)
let fade

function heatmap() {
  const dates = [...new Set(props.data.map(row => row.date))]
  const departments = [...new Set(props.data.map(row => row.dept_code))]
  return {
    grid: { top: 12, left: 30, right: 16, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: dates, axisLabel: { color: chartTheme.text, hideOverlap: true } },
    yAxis: { type: 'category', data: departments, axisLabel: { color: chartTheme.text } },
    visualMap: { min: 0, max: Math.max(1, ...props.data.map(row => row.visits)),
      calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
      inRange: { color: [chartTheme.background, chartTheme.support, chartTheme.ranking, chartTheme.primary] } },
    series: [{ type: 'heatmap', data: props.data.map(row => [row.date, row.dept_code, row.visits]) }],
  }
}

function draw(chart, phase, delay) {
  const pie = props.kind === 'pie'
  const motion = chartMotionOptions(pie ? 'radial' : 'heatmap', phase, delay)
  const option = !pie ? heatmap() : {
    series: [{ ...motion, type: 'pie', radius: props.drillable ? ['56%', '88%'] : ['38%', '68%'], animationType: 'expansion',
      label: { show: !props.drillable, color: chartTheme.text, overflow: 'truncate' },
      itemStyle: { borderColor: chartTheme.background, borderWidth: 2 },
      data: props.data.map(row => ({ name: row.dept_label ?? row.dept_code, value: row.visits,
        itemStyle: row.dept_color ? { color: row.dept_color } : undefined })) }],
  }
  chart.setOption({
    ...motion, animation: pie && motion.animation, animationDelay: 0, aria: { enabled: true },
    color: props.theme.palette || chartTheme.palette,
    tooltip: { trigger: 'item', confine: true, renderMode: 'richText',
      formatter: props.drillable && props.kind === 'pie' ? '{b}\n{c} 人次 · {d}%' : undefined },
    ...option,
  }, { notMerge: phase !== 'update' })
  fade?.cancel()
  if (!pie && motion.animation && props.data.length) {
    fade = container.value.animate([{ opacity: .84 }, { opacity: 1 }], {
      duration: motion.animationDuration, delay: motion.animationDelay, easing: 'ease-out', fill: 'backwards',
    })
  }
}

const update = useAnimatedChart(container, draw, {
  failed,
  onInit: chart => chart.on('click', 'series.pie', ({ dataIndex }) => {
    const row = props.data[dataIndex]
    if (props.drillable && row) emit('select-row', row)
  }),
})
watch(() => [props.data, props.theme], update, { flush: 'post' })
onDeactivated(() => fade?.cancel())
onBeforeUnmount(() => fade?.cancel())
</script>

<template>
  <div class="chart-scroll" :aria-busy="loading">
    <p v-if="failed" role="alert">图表暂不可用，请查看报表数据。</p>
    <p v-else-if="!data.length && !loading">暂无数据</p>
    <div ref="container" class="chart"
      :style="{ height: drillable && kind === 'pie' ? '236px' : '290px' }"></div>
  </div>
</template>
