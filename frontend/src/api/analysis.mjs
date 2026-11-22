import { onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSession } from './session.mjs'
import { waitForBackgroundRequest } from './request.js'

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const decimal = /^-?\d+(?:\.\d+)?$/

export const isMetricValue = value => typeof value === 'number' && Number.isFinite(value)
  || typeof value === 'string' && decimal.test(value) && Number.isFinite(Number(value))

export function formatMetric(value, unit) {
  if (!isMetricValue(value)) return '—'
  const display = (unit.startsWith('元') ? money : number).format(value)
  return unit === '%' ? display + '%' : [display, unit].filter(Boolean).join(' ')
}

export function describeAnswer({ label, unit, points, trend, nullReason, unavailable }) {
  const available = points.filter(point => point.value !== null)
  if (!available.length) {
    if (nullReason === 'ZERO_BASE') return '上期门诊人次为 0，无法计算环比。'
    if (nullReason === 'PRIOR_UNAVAILABLE') return '暂无可比上期数据，无法计算环比。'
    return unavailable ? '当前无法计算该指标。' : `所查期间暂无${label}数据。`
  }
  if (!trend.length) return `所查期间${label}为 ${formatMetric(available[0].value, unit)}。`

  const selected = available.length > 3 ? [available[0], available.at(-1)] : available
  const values = selected.map(point => {
    const [year, month] = point.start_date.split('-')
    return `${year} 年 ${Number(month)} 月为 ${formatMetric(point.value, unit)}`
  })
  return `${label}在 ${values.join('，')}。`
}

export function useAnalysisRequest() {
  const session = useSession()
  const router = useRouter()
  const busy = ref(false)
  const error = ref('')
  const status = ref(0)
  const controller = new AbortController()
  onBeforeUnmount(() => controller.abort())

  async function run(path, { body, read = data => data, invalid = '请核对查询条件。', waitForPreload = true } = {}) {
    if (busy.value) return
    busy.value = true
    error.value = ''
    status.value = 0
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
    try {
      if (body !== undefined && waitForPreload) await waitForBackgroundRequest()
      signal.throwIfAborted()
      const response = await session.request(path, {
        ...(body === undefined ? {} : {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }),
        signal,
      }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
      const payload = await response.json().catch(() => null)
      if (controller.signal.aborted) return
      status.value = response.status
      if (!response.ok) {
        error.value = {
          401: '请重新登录。', 403: '无权查询该数据。', 422: invalid,
          503: typeof payload?.msg === 'string' && payload.msg.includes('超出时限')
            ? '查询超时，请缩小范围。' : '该范围数据暂不可用。',
        }[response.status] ?? '查询失败，请重试。'
        return
      }
      if (payload?.code !== 0 || !payload.data) throw new Error('Invalid response')
      return read(payload.data)
    } catch (cause) {
      if (!controller.signal.aborted) error.value = cause?.name === 'TimeoutError' ? '查询超时，请重试。' : '查询失败，请重试。'
    } finally {
      busy.value = false
    }
  }

  return { busy, error, status, run }
}
