import { inject, onActivated, provide, shallowRef } from 'vue'

export const chartMotion = Object.freeze({ line: 1400, bar: 680, radial: 780, count: 620, heatmap: 240, update: 400, gap: 100 })
export const chartMotionStyle = Object.fromEntries(Object.entries(chartMotion).map(([kind, duration]) =>
  [`--chart-motion-${kind}`, `${duration}ms`]))

const preference = typeof window === 'undefined' ? null : window.matchMedia('(prefers-reduced-motion: reduce)')
export const reducedMotion = shallowRef(preference?.matches ?? false)
preference?.addEventListener('change', event => { reducedMotion.value = event.matches })

export function chartMotionOptions(kind, phase, delay = 0) {
  const entering = phase === 'entrance'
  const animation = phase !== 'static' && !reducedMotion.value
  return {
    animation,
    animationDelay: animation && entering ? delay : 0,
    animationDuration: entering ? chartMotion[kind] : chartMotion.update,
    animationEasing: entering && kind === 'line' ? 'cubicInOut' : 'cubicOut',
    animationDurationUpdate: chartMotion.update,
    animationEasingUpdate: 'cubicOut',
  }
}

const scopeKey = Symbol('chart motion')
export const skeletonHandoff = Symbol('skeleton handoff')
const defaultScope = { phase: shallowRef('entrance'), revision: shallowRef(0), claim: () => 0 }

export function provideChartMotion(hasResult) {
  const phase = shallowRef('entrance')
  const revision = shallowRef(0)
  let seen = false
  let enterAfterLoad = false
  let slot = 0
  function commit() {
    phase.value = seen && !enterAfterLoad ? 'update' : 'entrance'
    seen = true
    enterAfterLoad = false
    slot = 0
    revision.value++
  }
  onActivated(() => {
    enterAfterLoad = !hasResult()
    phase.value = 'entrance'
    revision.value++
  })
  provide(scopeKey, { phase, revision, claim: () => Math.min(slot++, 2) * chartMotion.gap })
  return { commit, phase, revision }
}

export function useChartMotion() {
  return inject(scopeKey, defaultScope)
}

export function countUpValue(target, progress) {
  return target * (1 - 0.08 * (1 - progress) ** 3)
}

export function resizeChartIfNeeded(chart, container) {
  if (!container?.clientWidth || !container.clientHeight) return false
  if (chart.getWidth() === container.clientWidth && chart.getHeight() === container.clientHeight) return false
  chart.resize()
  return true
}
