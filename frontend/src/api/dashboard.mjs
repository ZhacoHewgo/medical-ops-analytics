class DashboardError extends Error {}

const isDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
  && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value
const isCount = value => Number.isSafeInteger(value) && value >= 0

export function buildQuery(filters = {}) {
  const { start_date = '', end_date = '', dept_code = '' } = filters
  if ((start_date && !isDate(start_date)) || (end_date && !isDate(end_date))) {
    throw new DashboardError('请填写有效日期，格式为 YYYY-MM-DD。')
  }
  if (start_date && end_date && start_date > end_date) {
    throw new DashboardError('开始日期不能晚于结束日期。')
  }
  const params = new URLSearchParams()
  if (start_date) params.set('start_date', start_date)
  if (end_date) params.set('end_date', end_date)
  if (dept_code) params.set('dept_code', dept_code)
  return params.toString() ? `?${params}` : ''
}

export function validateDashboard(data, source = 'csv', department = null) {
  const require = condition => {
    if (!condition) throw new DashboardError('服务响应不符合门诊数据合同，请检查后端。')
  }
  require(data && typeof data === 'object')
  const { filters, meta, outpatient_visits: visits, daily_trend: trend, department_ranking: ranking } = data
  require(filters && meta && Array.isArray(trend) && Array.isArray(ranking) && isCount(visits))
  require(isDate(filters.start_date) && isDate(filters.end_date) && filters.start_date <= filters.end_date)
  require(['csv', 'preview', 'ads'].includes(source))
  require(isDate(meta.available_start_date) && isDate(meta.available_end_date)
    && meta.available_start_date <= meta.available_end_date)
  require(Array.isArray(meta.department_codes) && meta.department_codes.every(code => typeof code === 'string' && code.length > 0)
    && new Set(meta.department_codes).size === meta.department_codes.length)
  require(filters.dept_code === null || meta.department_codes.includes(filters.dept_code))
  require(trend.every((row, index) => row && isDate(row.date) && isCount(row.visits) && row.visits > 0
    && row.date >= filters.start_date && row.date <= filters.end_date
    && (index === 0 || trend[index - 1].date < row.date)))
  require(ranking.every(row => row && meta.department_codes.includes(row.dept_code)
    && isCount(row.visits) && row.visits > 0
    && (filters.dept_code === null || row.dept_code === filters.dept_code)))
  require(new Set(ranking.map(row => row.dept_code)).size === ranking.length)
  require(trend.reduce((sum, row) => sum + row.visits, 0) === visits
    && ranking.reduce((sum, row) => sum + row.visits, 0) === visits)
  if (source !== 'csv') {
    if (department !== null) {
      require(filters.dept_code === department
        && meta.department_codes.length === 1 && meta.department_codes[0] === department)
    }
    const expectedSource = source === 'ads' ? 'ads_outpatient_theme' : 'dwd_visit_detail'
    require(filters.start_date >= meta.available_start_date && filters.end_date <= meta.available_end_date)
    require(meta.source === expectedSource && isDate(meta.batch_date)
      && typeof meta.build_run_id === 'string'
      && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(meta.build_run_id)
      && meta.source_file === undefined && meta.quality === undefined)
    return data
  }
  require(meta.schema_version === '1.0' && meta.simulated === true && meta.quality_scope === 'whole_source')
  require(meta.source_file === 'data/mock_his/his_outpatient.csv'
    && typeof meta.source_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(meta.source_sha256)
    && typeof meta.generated_at === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(meta.generated_at)
    && Number.isFinite(Date.parse(meta.generated_at)))
  const qualityKeys = ['raw_rows', 'invalid_id_rows', 'invalid_date_rows', 'conflicting_id_rows',
    'duplicate_rows_removed', 'accepted_visits', 'unknown_department_visits']
  const quality = meta.quality
  require(quality && qualityKeys.every(key => isCount(quality[key])))
  require(quality.raw_rows === quality.invalid_id_rows + quality.invalid_date_rows
    + quality.conflicting_id_rows + quality.duplicate_rows_removed + quality.accepted_visits
    && quality.unknown_department_visits <= quality.accepted_visits
    && visits <= quality.accepted_visits)
  return data
}

export function trendTransitionSeed(previous, next) {
  if (!previous) return undefined
  if (previous.filters.dept_code !== next.filters.dept_code || previous.meta.batch_date !== next.meta.batch_date
      || previous.meta.build_run_id !== next.meta.build_run_id) return []

  const dates = new Set(next.daily_trend.map(row => row.date))
  if (previous.daily_trend.filter(row => dates.has(row.date)).length < 2) return []
  return previous.daily_trend.map(row => dates.has(row.date) ? row : { date: row.date, visits: null })
}

export function createDashboardLoader({ fetchImpl = fetch, onChange, source = 'csv', department = null, endpoint }) {
  const url = endpoint ?? {
    ads: '/api/v1/outpatient/summary',
    preview: '/api/dashboard/outpatient-preview',
    csv: '/api/dashboard/overview',
  }[source]
  let sequence = 0
  let controller
  let lastFilters = {}

  async function load(filters = {}) {
    const requestId = ++sequence
    controller?.abort()
    controller = new AbortController()
    const currentController = controller
    const requestedFilters = { ...filters }
    if (department !== null) requestedFilters.dept_code = department
    lastFilters = requestedFilters
    const publish = state => {
      if (requestId === sequence) onChange({ ...state, requestedFilters })
    }
    publish({ status: 'loading', data: null, error: '' })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; currentController.abort() }, 15000)
    try {
      const query = buildQuery(requestedFilters)
      const response = await fetchImpl(`${url}${query}`, {
        signal: currentController.signal, headers: { Accept: 'application/json' }, cache: 'no-store',
      })
      if (!response.ok) {
        const unavailable = {
          ads: '门诊集市暂不可用，请核对数据发布状态。',
          preview: '宽表构建不可用或已过期，请核对本地预览配置和构建号。',
          csv: '聚合数据暂不可用，请检查后端加工结果。',
        }
        const reason = {
          401: '登录已失效，请重新登录。',
          403: '当前账号无权查看所选科室。',
          422: '筛选参数未通过后端校验，请检查日期与科室。',
          503: unavailable[source],
        }[response.status] || '门诊服务请求失败。'
        throw new DashboardError(`${reason}（HTTP ${response.status}）`)
      }
      const payload = await response.json()
      if (payload?.code !== 0) throw new DashboardError('门诊服务返回错误，请检查后端。')
      const data = validateDashboard(payload.data, source, department)
      const expected = {
        start_date: requestedFilters.start_date || data.meta.available_start_date,
        end_date: requestedFilters.end_date || data.meta.available_end_date,
        dept_code: requestedFilters.dept_code || null,
      }
      if (Object.keys(expected).some(key => expected[key] !== data.filters[key])) {
        throw new DashboardError('服务返回的生效筛选与请求不一致，请检查后端。')
      }
      if (requestId !== sequence) return null
      publish({ status: 'success', data, error: '' })
      return data
    } catch (error) {
      publish({ status: 'error', data: null, error: timedOut ? '请求超过 15 秒，请检查本地服务后重试。'
        : error instanceof DashboardError ? error.message : '无法读取门诊数据，请检查本地后端与网络后重试。' })
      return null
    } finally {
      clearTimeout(timer)
    }
  }

  return {
    load,
    retry: () => load(lastFilters),
    dispose: () => { sequence++; controller?.abort() },
  }
}
