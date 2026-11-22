<script setup>
import { ref, watch } from 'vue'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { formatMetric } from '../api/analysis.mjs'
import { chartMotionOptions } from '../styles/chartMotion.mjs'
import { chartTheme } from '../styles/theme.js'
import { useAnimatedChart } from './charts/useAnimatedChart.mjs'

use([LineChart, AriaComponent, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  rows: { type: Array, required: true },
  grain: { type: String, required: true },
  colors: { type: Array, required: true },
})
const container = ref(null)
const failed = ref(false)

function draw(chart, phase, delay) {
  const motion = chartMotionOptions('line', phase, delay)
  chart.setOption({
    ...motion,
    animationDelay: 0,
    aria: { enabled: true, label: { description: '门诊、出院和收费趋势' } },
    color: props.colors,
    grid: { top: 30, left: 52, right: 50, bottom: 28 },
    tooltip: { trigger: 'axis', confine: true, renderMode: 'richText' },
    xAxis: {
      type: 'category', boundaryGap: false, data: props.rows.map(row => row.date === row.end ? row.date : `${row.date} — ${row.end}`),
      axisLine: { lineStyle: { color: chartTheme.support } }, axisTick: { show: false },
      axisLabel: { color: chartTheme.text, hideOverlap: true, formatter: label => props.grain === 'month' ? label.slice(0, 7) : label.slice(5, 10) },
    },
    yAxis: [
      { type: 'value', minInterval: 1, name: '人次', axisLabel: { color: chartTheme.text }, splitLine: { lineStyle: { color: chartTheme.grid } } },
      { type: 'value', name: '万元', axisLabel: { color: chartTheme.text, formatter: value => formatMetric(value / 10000, '') },
        splitLine: { show: false } },
    ],
    series: [
      ['outpatient', '门诊人次', '人次'], ['inpatient', '出院人次', '人次'], ['amount', '收费金额', '元'],
    ].map(([key, name, unit], index) => ({
      ...motion, id: key, type: 'line', name, yAxisIndex: index === 2 ? 1 : 0, smooth: .24, showSymbol: props.rows.length === 1,
      lineStyle: { width: key === 'inpatient' ? 3 : 2.5, type: key === 'inpatient' ? 'dashed' : 'solid' },
      areaStyle: key === 'outpatient' ? { color: props.colors[0] + '12' } : undefined,
      data: props.rows.map(row => ({ name: `${row.date}/${row.end}`,
        value: row[key] === null ? null : index === 2 ? row[key] / 100 : row[key] })),
      tooltip: { valueFormatter: value => formatMetric(value, unit) },
    })),
  }, { notMerge: phase !== 'update' })
}

const update = useAnimatedChart(container, draw, { failed })
watch(() => [props.rows, props.grain, props.colors], update, { flush: 'post' })
</script>

<template>
  <div ref="container" class="overview-trend-chart" role="img" aria-label="运营趋势图"></div>
  <p v-if="failed" class="chart-error" role="alert">趋势图暂不可用，可查看日度明细。</p>
</template>

<style scoped>
.overview-trend-chart { width: 100%; height: 248px; min-width: 0; }
</style>
