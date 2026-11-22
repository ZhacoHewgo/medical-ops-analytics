import test from 'node:test'
import assert from 'node:assert/strict'
import { chartMotionOptions, countUpValue, reducedMotion, resizeChartIfNeeded } from '../src/styles/chartMotion.mjs'

test('entrances can stagger while snapshots and reduced motion stay static', () => {
  assert.equal(chartMotionOptions('bar', 'entrance', 100).animationDelay, 100)
  assert.equal(chartMotionOptions('bar', 'entrance').animationEasing, 'cubicOut')
  assert.equal(chartMotionOptions('radial', 'entrance').animationDuration, 780)
  assert.equal(chartMotionOptions('bar', 'static', 100).animation, false)
  assert.equal(chartMotionOptions('bar', 'static', 100).animationDelay, 0)
  reducedMotion.value = true
  try {
    assert.equal(chartMotionOptions('radial', 'entrance', 100).animation, false)
    assert.equal(chartMotionOptions('line', 'update').animation, false)
  } finally {
    reducedMotion.value = false
  }
})

test('count-up approaches the exact value without overshooting', () => {
  assert.equal(countUpValue(100, 0), 92)
  assert.ok(countUpValue(100, 0.4) > 92)
  assert.equal(countUpValue(100, 1), 100)
  assert.ok(countUpValue(-18.5, 0.4) > -18.5)
  assert.equal(countUpValue(-18.5, 1), -18.5)
  assert.equal(countUpValue(0, 0.5), 0)
})

test('line entry and filter transitions have distinct timings and respect reduced motion', () => {
  assert.deepEqual(chartMotionOptions('line', 'entrance'), {
    animation: true, animationDelay: 0, animationDuration: 1400, animationEasing: 'cubicInOut',
    animationDurationUpdate: 400, animationEasingUpdate: 'cubicOut',
  })
  for (const kind of ['line', 'bar', 'radial', 'heatmap']) {
    const update = chartMotionOptions(kind, 'update', 100)
    assert.equal(update.animationDuration, 400)
    assert.equal(update.animationEasing, 'cubicOut')
    assert.equal(update.animationDelay, 0)
  }
})

test('resize observer leaves a chart at its existing size alone', () => {
  let resized = 0
  const chart = { getWidth: () => 400, getHeight: () => 300, resize: () => { resized += 1 } }
  assert.equal(resizeChartIfNeeded(chart, { clientWidth: 400, clientHeight: 300 }), false)
  assert.equal(resizeChartIfNeeded(chart, { clientWidth: 0, clientHeight: 300 }), false)
  assert.equal(resizeChartIfNeeded(chart, { clientWidth: 280, clientHeight: 300 }), true)
  assert.equal(resized, 1)
})
