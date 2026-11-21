import { buildQuery, validateDashboard } from './dashboard.mjs'

export const reportGroups = {
  day: { label: '按日', time: '日期', department: false },
  department: { label: '按科室', time: '', department: true },
  day_department: { label: '日期 × 科室', time: '日期', department: true },
  month_department: { label: '月份 × 科室', time: '月份', department: true },
}

const messages = {
  401: '登录已失效，请重新登录。',
  403: '当前账号没有查看或导出该科室的权限。',
  409: '数据发布批次已更新，请重新查询后导出。',
  422: '请检查日期和科室范围，或缩小查询范围。',
  503: '门诊集市暂不可用，请稍后重试。',
}

export class ReportError extends Error {
  constructor(status) {
    super(messages[status] || '报表服务返回无效结果，请稍后重试。')
  }
}

export async function readReport(request, filters, group, department, signal) {
  const params = new URLSearchParams(buildQuery(filters))
  params.set('group_by', group)
  const response = await request(`/api/v1/reports/outpatient?${params}`, {
    headers: { Accept: 'application/json' }, signal,
  })
  if (!response.ok) throw new ReportError(response.status)
  const payload = await response.json()
  if (payload?.code !== 0) throw new ReportError()
  const data = validateDashboard(payload.data, 'ads', department)
  const expected = {
    start_date: filters.start_date || data.meta.available_start_date,
    end_date: filters.end_date || data.meta.available_end_date,
    dept_code: department || filters.dept_code || null,
  }
  if (data.group_by !== group || Object.keys(expected).some(key => expected[key] !== data.filters[key])
      || !Array.isArray(data.rows) || data.rows.length > 10000) throw new ReportError()

  const spec = reportGroups[group]
  const dates = new Set(data.daily_trend.map(row => spec.time === '月份' ? row.date.slice(0, 7) : row.date))
  const departments = new Set(data.department_ranking.map(row => row.dept_code))
  const keys = new Set()
  let total = 0
  for (const row of data.rows) {
    if (!row || !Number.isSafeInteger(row.visits) || row.visits <= 0
        || (spec.time ? !dates.has(row.date) : row.date !== null)
        || (spec.department ? !departments.has(row.dept_code) : row.dept_code !== null)) throw new ReportError()
    const key = JSON.stringify([row.date, row.dept_code])
    if (keys.has(key)) throw new ReportError()
    keys.add(key)
    total += row.visits
  }
  if (total !== data.outpatient_visits) throw new ReportError()
  return data
}

function monthRange(year, month) {
  const date = new Date(0)
  date.setUTCFullYear(year, month, 0)
  const end = date.toISOString().slice(0, 10)
  return { start_date: end.slice(0, 7) + '-01', end_date: end }
}

export function comparisonRange(filters, method) {
  const [year, month] = filters.start_date.split('-').map(Number)
  const full = monthRange(year, month)
  if (full.start_date !== filters.start_date || full.end_date !== filters.end_date) return null
  const backYear = method === 'year' || month === 1 ? year - 1 : year
  const backMonth = method === 'year' ? month : month === 1 ? 12 : month - 1
  if (backYear < 1) return null
  return { ...monthRange(backYear, backMonth), dept_code: filters.dept_code }
}

export function drillSelection(filters, row) {
  const range = { ...filters }
  if (row.date?.length === 7) {
    const [year, month] = row.date.split('-').map(Number)
    const full = monthRange(year, month)
    range.start_date = filters.start_date > full.start_date ? filters.start_date : full.start_date
    range.end_date = filters.end_date < full.end_date ? filters.end_date : full.end_date
  } else if (row.date) {
    range.start_date = range.end_date = row.date
  }
  return { ...range, dept_code: row.dept_code || filters.dept_code }
}

export async function readReportExport(request, data, format, signal) {
  const type = format === 'pdf' ? 'application/pdf'
    : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  const response = await request('/api/v1/reports/export', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json', Accept: type },
    body: JSON.stringify({
      ...data.filters, group_by: data.group_by, format, expected_build_run_id: data.meta.build_run_id,
    }),
  })
  if (!response.ok) throw new ReportError(response.status)
  if (response.headers.get('content-type') !== type) throw new ReportError()
  const file = await response.blob()
  if (!file.size) throw new ReportError()
  return file
}
