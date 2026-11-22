<script setup>
import { onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { chartMotion, countUpValue, reducedMotion, useChartMotion } from '../../styles/chartMotion.mjs'

const props = defineProps({
  value: { type: [Number, String], default: null },
  format: { type: Function, default: value => value?.toLocaleString('zh-CN') ?? '—' },
})
const element = ref(null)
const trace = ref(null)
const duration = ref(chartMotion.count)
const scope = useChartMotion()
let frame
let replay = false
let drawn = false

function stop() {
  cancelAnimationFrame(frame)
  trace.value = null
}

function play(phase) {
  stop()
  if (!element.value?.isConnected) return
  drawn = true
  const target = Number(props.value)
  if (reducedMotion.value || !target || !Number.isFinite(target)) return
  duration.value = phase === 'entrance' ? chartMotion.count : chartMotion.update
  const started = performance.now()
  frame = requestAnimationFrame(draw)

  function draw(now) {
    const progress = Math.max(0, (now - started) / duration.value)
    if (progress >= 1 || !element.value?.isConnected) {
      trace.value = null
      return
    }
    const count = countUpValue(target, progress)
    trace.value = props.format(Number.isInteger(target) ? Math.round(count) : count)
    frame = requestAnimationFrame(draw)
  }
}

watch([() => props.value, element], ([, node], [, previousNode]) => {
  play(node === previousNode ? 'update' : scope.phase.value)
}, { flush: 'post' })
watch(reducedMotion, reduced => { if (reduced) stop() })
onDeactivated(() => { replay = true; stop() })
onActivated(() => {
  if (replay || !drawn) play('entrance')
  replay = false
})
onBeforeUnmount(stop)
</script>

<template>
  <b ref="element" class="motion-number">{{ value == null ? '—' : format(value) }}<i v-if="trace !== null" class="motion-number-trace"
    :data-count="trace" :style="{ animationDuration: `${duration}ms` }" aria-hidden="true"></i></b>
</template>
