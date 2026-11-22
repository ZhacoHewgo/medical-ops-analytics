import { buildQuery, validateDashboard } from './dashboard.mjs'
import { outpatientDepartmentName } from './departmentLabels.mjs'

export const multidimensionalEndpoint = '/api/v1/reports/multidimensional'
export const defaultMultidimensional = {
  group_by: 'date_department', grain: 'month', metrics: ['billing_income', 'charge_amount', 'ledger_income'],
}

export function multidimensionalPath(filters = defaultMultidimensional) {
  const params = new URLSearchParams(buildQuery(filters))
  params.set('group_by', filters.group_by)
  params.set('grain', filters.grain)
  for (const metric of filters.metrics) params.append('metrics', metric)
  return `${multidimensionalEndpoint}?${params}`
}

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

export function reportDateChoices(start, end) {
  if (!start || !end) throw new ReportError()
  buildQuery({ start_date: start, end_date: end })
  const days = []
  const months = []
  const from = Date.parse(start)
  const through = Date.parse(end)
  for (let time = from; time <= through; time += 86400000) {
    const day = new Date(time).toISOString().slice(0, 10)
    days.push(day)
    const month = day.slice(0, 7)
    if (months.at(-1)?.month !== month) months.push({ month, start_date: day, end_date: day })
    else months.at(-1).end_date = day
  }
  return { days, months }
}

export function reportNarrative({ overview, trend, ranking, anomaly }) {
  const count = value => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  const change = value => `${value < 0 ? '减少' : '增加'} ${count(Math.abs(value))} 人次`
  const visits = overview.outpatient_visits
  let summary = `本期门诊就诊 ${count(visits)} 人次`
  if (Number.isSafeInteger(overview.previous_visits)) {
    const difference = visits - overview.previous_visits
    summary += difference ? `，较上期${change(difference)}` : '，与上期持平'
    if (difference && overview.change_pct !== null && Number.isFinite(Number(overview.change_pct))) {
      summary += `（${count(Math.abs(Number(overview.change_pct)))}%）`
    }
    summary += `。上期门诊就诊 ${count(overview.previous_visits)} 人次`
  }
  const operations = overview.admissions !== undefined
  const amount = value => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const supplemental = [
    ['admissions', '入院', '人次'], ['discharges', '出院', '人次'], ['avg_stay_days', '平均住院日', '天'],
    ['charge_amount', '收费合计', '元'], ['outpatient_charge', '门诊收费', '元'], ['inpatient_charge', '住院收费', '元'],
    ['drug_share', '药品费占比', '%'], ['consumable_share', '材料费占比', '%'],
  ].filter(([key]) => overview[key] != null).map(([key, label, unit]) => {
    const value = unit === '元' ? amount(overview[key]) : count(Number(overview[key]))
    return `${label} ${value}${unit === '%' ? '%' : ' ' + unit}`
  })
  if (supplemental.length) summary += `。${supplemental.join('，')}`
  const peak = trend.reduce((highest, row) => !highest || row.visits > highest.visits ? row : highest, null)
  let trendText = '本期无门诊记录。'
  if (peak?.visits) {
    const mean = visits / trend.length
    const minimum = Math.min(...trend.map(row => row.visits))
    trendText = `日均门诊 ${count(mean)} 人次。单日最高为 ${peak.date} 的 ${count(peak.visits)} 人次。`
    if (minimum < peak.visits) {
      const aboveMean = trend.filter(row => row.visits > mean).length
      trendText += `日门诊量在 ${count(minimum)} 至 ${count(peak.visits)} 人次之间，有 ${count(aboveMean)} 天高于本期日均。`
    }
  }
  for (const [key, label, unit] of [['discharges', '出院人次', '人次'], ['charge_amount', '收费金额', '元']]) {
    const highest = trend.reduce((best, row) => row[key] != null && (!best || Number(row[key]) > Number(best[key])) ? row : best, null)
    if (highest && Number(highest[key]) > 0) {
      const value = unit === '元' ? amount(highest[key]) : count(highest[key])
      trendText += `单日${label}最高为 ${highest.date} 的 ${value} ${unit}。`
    }
  }
  const departments = ranking.filter(row => row.visits > 0)
  const topDepartments = departments.slice(0, 3)
  let distribution = `本期 ${count(departments.length)} 个科室有门诊记录。${topDepartments
    .map(row => `${outpatientDepartmentName(row.dept_code)} ${count(row.visits)} 人次`).join('，')}。`
  if (visits > 0 && departments.length > 3) {
    const total = topDepartments.reduce((sum, row) => sum + row.visits, 0)
    distribution += `门诊量前三的科室合计 ${count(total)} 人次，占本期门诊量的 ${count(total * 100 / visits)}%。`
  } else if (visits > 0 && departments.length > 1) {
    const first = topDepartments[0]
    distribution += `${outpatientDepartmentName(first.dept_code)}占本期门诊量的 ${count(first.visits * 100 / visits)}%。`
  }
  const largest = anomaly.top_changes[0]
  return [
    { id: 'overview', title: '总体概览', text: summary + '。' },
    { id: 'trend', title: operations ? '运营趋势' : '门诊趋势', text: trendText },
    ...(ranking.length ? [{ id: 'ranking', title: operations ? '科室对比' : '科室分布', text: departments.length ? distribution : '本期科室运营数据。' }] : []),
    { id: 'anomaly', title: '科室波动', text: largest
      ? `与上期相比，${outpatientDepartmentName(largest.dept_code)}的门诊量变动最大，${change(largest.difference)}。`
        + `该科室上期为 ${count(largest.previous)} 人次，本期为 ${count(largest.current)} 人次。`
      : anomaly.status === 'AVAILABLE' ? '科室门诊量与上期一致。' : '暂无可比上期。' },
  ]
}

export function reportPeriod(type, value) {
  const match = /^(\d{4})-(W?)(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const index = Number(match[3])
  if (year < 1000 || index < 1) return null
  if (type === 'monthly' && !match[2] && index <= 12) return monthRange(year, index)
  if (type !== 'weekly' || !match[2] || index > 53) return null
  const monday = new Date(0)
  monday.setUTCFullYear(year, 0, 4)
  monday.setUTCDate(4 - (monday.getUTCDay() + 6) % 7 + 7 * (index - 1))
  const start = monday.toISOString().slice(0, 10)
  monday.setUTCDate(monday.getUTCDate() + 3)
  if (monday.getUTCFullYear() !== year) return null
  monday.setUTCDate(monday.getUTCDate() + 3)
  if (monday.getUTCFullYear() > 9999) return null
  return { start_date: start, end_date: monday.toISOString().slice(0, 10) }
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
