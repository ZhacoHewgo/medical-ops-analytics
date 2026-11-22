<script setup>
import { computed, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { chartMotionOptions } from '../styles/chartMotion.mjs'
import { chartTheme } from '../styles/theme.js'
import { useAnimatedChart } from './charts/useAnimatedChart.mjs'

use([BarChart, LineChart, AriaComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  kind: { type: String, required: true },
  rows: { type: Array, required: true },
  drillable: Boolean,
  snapshot: Boolean,
  exporting: Boolean,
  label: { type: String, default: '门诊量' },
  unit: { type: String, default: '人次' },
  category: { type: String, default: '科室' },
  period: { type: String, default: '日' },
  theme: { type: Object, default: () => ({}) },
  highlightDate: String,
  compactRanking: Boolean,
  previousRows: Array,
})
const emit = defineEmits(['select-row'])
const container = ref(null)
const chartError = ref(false)
const picture = ref('')
const isComparison = computed(() => props.kind === 'comparison')
const isRanking = computed(() => props.kind !== 'trend')
const height = computed(() => {
  if (isComparison.value) return 208
  if (isRanking.value) return props.compactRanking ? Math.max(145, props.rows.length * 20 + 28) : Math.max(300, props.rows.length * 23 + 42)
  return props.highlightDate ? 264 : 300
})
const description = computed(() => isComparison.value ? '科室门诊人次本期与上期对比'
  : isRanking.value ? `${props.label}${props.category}排行` : `${props.label}${props.period}趋势`)
const compactAmount = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 })
const rowLabel = row => row.dept_label ?? (row.dept_code === '__UNKNOWN__' ? '未标注科室（__UNKNOWN__）' : row.dept_code)

function takeSnapshot(chart) {
  if (props.snapshot && props.exporting) {
    picture.value = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' })
  }
}

function draw(chart, phase, delay, initial, rows = props.rows) {
  if (initial && phase === 'update' && !isRanking.value && props.previousRows?.length > 1) {
    draw(chart, 'static', 0, false, props.previousRows)
  }
  const ranking = isRanking.value
  const compactRank = ranking && props.compactRanking
  const { primary, ranking: rankColor, text, grid } = { ...chartTheme, ...props.theme }
  const labels = rows.map(row => ranking ? rowLabel(row) : row.date)
  const values = rows.map(row => row.visits)
  const comparison = isComparison.value
  const motion = chartMotionOptions(ranking ? 'bar' : 'line', phase, delay)
  const focusedTrend = !ranking && Boolean(props.highlightDate)
  const seriesValues = values.map((value, index) => {
    const highlighted = focusedTrend && labels[index] === props.highlightDate
    const color = compactRank && rows[index].dept_color
    return { value, name: ranking ? rows[index].dept_code : labels[index], ...(color && { itemStyle: { color } }), ...(highlighted && {
      symbolSize: 9, itemStyle: { color: 'white', borderColor: primary, borderWidth: 2 },
    }) }
  })
  const countAxis = {
    type: 'value', minInterval: props.unit === '元' ? 0.01 : 1, splitLine: { lineStyle: { color: grid } },
    show: !compactRank,
    axisLabel: { hideOverlap: true, formatter: props.unit === '元' ? value => compactAmount.format(value) : undefined },
  }
  const categoryAxis = {
    type: 'category', data: labels, inverse: ranking, boundaryGap: ranking,
    axisLine: { lineStyle: { color: chartTheme.support } }, axisTick: { show: false },
    axisLabel: { color: text, width: ranking ? 128 : 90, overflow: 'truncate', hideOverlap: true, showMaxLabel: ranking ? undefined : false,
      formatter: focusedTrend ? date => date.slice(5).replace('-', '/') : undefined },
  }
  chart.setOption({
    ...motion,
    animationDelay: 0,
    textStyle: { fontFamily: 'system-ui, sans-serif', color: text },
    aria: { enabled: true, label: { description: description.value } },
    tooltip: {
      show: !props.snapshot,
      trigger: 'axis',
      renderMode: 'richText',
      confine: true,
      valueFormatter: value => `${focusedTrend && typeof value === 'number' ? value.toLocaleString('zh-CN') : value} ${props.unit}`,
      ...(focusedTrend && {
        backgroundColor: text, borderColor: text, textStyle: { color: 'white', fontSize: 12 },
        padding: [8, 11], axisPointer: { lineStyle: { color: chartTheme.ranking, type: 'dashed' } },
      }),
    },
    legend: comparison ? { top: 2, left: 12, textStyle: { color: text } } : undefined,
    grid: compactRank ? { top: 4, right: 45, bottom: 4, left: 6, containLabel: true }
      : { top: comparison ? 34 : 16, right: 24, bottom: 24, left: 12, containLabel: true },
    xAxis: ranking ? countAxis : categoryAxis,
    yAxis: ranking ? categoryAxis : countAxis,
    series: comparison ? [
      { ...motion, name: '本期', type: 'bar', data: rows.map(row => row.current), barMaxWidth: 16, itemStyle: { color: primary } },
      { ...motion, name: '上期', type: 'bar', data: rows.map(row => row.previous), barMaxWidth: 16, itemStyle: { color: chartTheme.accent } },
    ] : [{
      id: 'main-series', name: props.label, type: ranking ? 'bar' : 'line', data: seriesValues,
      ...motion,
      barMaxWidth: compactRank ? 12 : 16, showSymbol: props.drillable || values.length === 1,
      symbol: focusedTrend ? 'circle' : undefined, symbolSize: focusedTrend ? 4 : 8,
      label: compactRank ? { show: true, position: 'right', color: text, fontSize: 11,
        formatter: ({ value }) => value.toLocaleString('zh-CN') } : undefined,
      cursor: props.drillable ? 'pointer' : 'default',
      itemStyle: { color: ranking ? rankColor : primary, borderRadius: ranking ? [0, 4, 4, 0] : 0 },
      lineStyle: { width: focusedTrend ? 2.4 : 2 }, areaStyle: ranking ? undefined : { color: primary, opacity: focusedTrend ? 0.06 : 0.09 },
    }],
  }, { notMerge: phase !== 'update' })
  takeSnapshot(chart)
}

const update = useAnimatedChart(container, draw, {
  failed: chartError,
  frozen: () => props.snapshot && props.exporting,
  onResize: takeSnapshot,
  onInit: chart => chart.on('click', 'series', ({ dataIndex }) => {
    const row = props.rows[dataIndex]
    if (props.drillable && row) emit('select-row', row)
  }),
})
watch(() => [props.rows, props.theme, props.highlightDate, props.label, props.unit], update, { flush: 'post' })
</script>

<template>
  <div :class="snapshot ? 'chart-snapshot' : 'chart-scroll'" :tabindex="snapshot ? undefined : 0"
    :role="snapshot ? 'img' : undefined"
    :aria-label="snapshot ? description : isRanking ? `${category}排行图，可滚动查看` : `${label}${period}趋势图`">
    <div ref="container" class="chart" :style="{ height: `${height}px` }" :aria-hidden="snapshot || undefined"></div>
    <img v-if="picture" :src="picture" :alt="description" hidden>
  </div>
  <p v-if="chartError" role="alert" class="chart-error">图表暂未绘制，请使用下方数据表查看真实结果。</p>
  <details v-if="!isComparison" class="chart-table">
    <summary>查看{{ isRanking ? category + '排行' : period + '趋势' }}数据表（{{ rows.length }} 项）</summary>
    <div class="table-scroll" tabindex="0" aria-label="图表数据表，可滚动查看">
      <table>
        <caption>{{ isRanking ? `${category}排行` : `${label}${period}趋势` }}</caption>
        <thead><tr><th scope="col">{{ isRanking ? category : period === '月' ? '月份' : '日期' }}</th><th scope="col">{{ label }}（{{ unit }}）</th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="isRanking ? row.dept_code : row.date">
            <th scope="row">
              <button v-if="drillable" class="button secondary" type="button" @click="emit('select-row', row)">{{ isRanking ? rowLabel(row) : row.date }}</button>
              <span v-else>{{ isRanking ? rowLabel(row) : row.date }}</span>
            </th>
            <td>{{ row.visits?.toLocaleString('zh-CN') ?? '暂缺' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
</template>
