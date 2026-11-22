import { inject, onActivated, onBeforeUnmount, onDeactivated, onMounted, watch } from 'vue'
import { init } from 'echarts/core'
import { reducedMotion, resizeChartIfNeeded, skeletonHandoff, useChartMotion } from '../../styles/chartMotion.mjs'

export function useAnimatedChart(container, render, { failed, onInit, onResize, frozen = () => false } = {}) {
  const scope = useChartMotion()
  const handoffDelay = inject(skeletonHandoff, () => 0)
  const delay = scope.claim()
  let chart
  let observer
  let drawn = false
  let replay = false
  let pendingPhase

  function draw(phase) {
    const node = container.value
    if (!node?.clientWidth || !node.clientHeight) {
      pendingPhase = phase
      return
    }
    pendingPhase = undefined
    try {
      if (!chart) {
        chart = init(node)
        onInit?.(chart)
      }
      resizeChartIfNeeded(chart, node)
      const mode = reducedMotion.value || frozen() ? 'static' : phase
      if (drawn && mode !== 'update') chart.clear()
      render(chart, mode, delay + (mode === 'entrance' ? handoffDelay() : 0), !drawn)
      drawn = true
      failed.value = false
    } catch {
      failed.value = true
    }
  }

  function enter(resuming = false) {
    if (!container.value?.clientWidth || (drawn && !replay)) return
    const phase = replay || resuming ? 'entrance' : scope.phase.value
    replay = false
    draw(phase)
  }

  onMounted(() => {
    observer = new ResizeObserver(() => {
      if (!drawn || replay) enter()
      else if (pendingPhase) draw(pendingPhase)
      else if (chart && resizeChartIfNeeded(chart, container.value)) onResize?.(chart)
    })
    observer.observe(container.value)
    enter()
  })
  onDeactivated(() => { replay = true })
  onActivated(() => enter(true))
  watch([reducedMotion, frozen], ([reduced, still]) => {
    if (reduced || still) draw('static')
  })
  onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })

  return () => draw('update')
}
