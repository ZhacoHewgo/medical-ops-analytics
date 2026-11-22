const overviewColumns = ['record_id', 'date', 'dept_code', 'outpatient_visits', 'discharges', 'outpatient_charge', 'inpatient_charge']
const detailTypes = ['outpatient', 'inpatient', 'charge']
const layouts = [
  ['overview', overviewColumns],
  ['outpatient', ['visit_id', 'visit_date', 'dept_code']],
  ['inpatient', ['visit_id', 'discharge_date', 'dept_code']],
  ['charge', ['charge_id', 'fee_date', 'dept_code', 'visit_type', 'amount']],
]
const kinds = { 门诊: 'OUTPATIENT', 住院: 'INPATIENT', 其他: 'OTHER' }
const care = ['OUTPATIENT', 'INPATIENT', 'OTHER']

const demoDepartments = ['HN', 'HX', 'XH', 'SJ', 'GK', 'PW', 'FC', 'EK', 'JZ', 'ICU']

export function overviewCsvTemplate(cutoff) {
  if (!validDate(cutoff)) return ''
  const first = new Date(cutoff + 'T00:00:00Z')
  first.setUTCMonth(first.getUTCMonth() + 1, 1)
  if (first.getUTCFullYear() > 9999) return ''
  const last = new Date(first)
  last.setUTCMonth(first.getUTCMonth() + 1, 0)
  const days = last.getUTCDate()
  const month = first.toISOString().slice(0, 7)
  const rows = [overviewColumns.join(',')]
  for (let day = 1; day <= days; day++) {
    const date = `${month}-${String(day).padStart(2, '0')}`
    const parts = Math.floor(100 / days) + Number(day <= 100 % days)
    const dailyVisits = 91 + (day * 13 % 29)
    const dailyDischarges = 13 + (day * 7 % 9)
    for (let part = 0; part < parts; part++) {
      const code = demoDepartments[(rows.length - 1) % demoDepartments.length]
      const visits = Math.floor(dailyVisits / parts) + Number(part < dailyVisits % parts)
      const discharges = Math.floor(dailyDischarges / parts) + Number(part < dailyDischarges % parts)
      const outpatientCharge = visits * (700 + ((day + part) * 37 % 300))
      const inpatientCharge = discharges * (22500 + ((day + part) * 733 % 4800))
      rows.push([`DEMO-${date}-${code}`, date, code, visits, discharges, outpatientCharge, inpatientCharge].join(','))
    }
  }
  return `data:text/csv;charset=utf-8,${encodeURIComponent(`\ufeff${rows.join('\r\n')}\r\n`)}`
}

function* csvRows(text) {
  const field = /(?:"((?:[^"]|"")*)"|([^",\r\n]*))(,|\r\n|\r|\n|$)/y
  let row = []
  while (field.lastIndex < text.length) {
    const match = field.exec(text)
    if (!match) throw new Error('CSV 格式错误，请检查引号和分隔符。')
    row.push((match[1]?.replaceAll('""', '"') ?? match[2]).trim())
    if (match[3] !== ',') {
      if (row.some(Boolean)) yield row
      row = []
    }
  }
  if (row.length) yield [...row, '']
}

function validDate(value) {
  if (!/^[1-9]\d{3}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

function readCents(amount) {
  if (!/^-?\d+(?:\.\d{1,2})?$/.test(amount)) return NaN
  const cents = Math.round(Number(amount) * 100)
  return Number.isSafeInteger(cents) && Math.abs(cents) <= 10_000_000_000 ? cents : NaN
}

function readMetrics(type, values) {
  if (type === 'overview') {
    const [visits, discharges, outpatientCharge, inpatientCharge] = values
    if (![visits, discharges].every(value => /^\d+$/.test(value) && Number(value) <= 1_000_000)) return null
    const charges = [readCents(outpatientCharge), readCents(inpatientCharge)]
    if (charges.some(Number.isNaN)) return null
    return [
      ['outpatient', 'OUTPATIENT', Number(visits), 0],
      ['inpatient', 'INPATIENT', Number(discharges), 0],
      ['charge', 'OUTPATIENT', 1, charges[0]],
      ['charge', 'INPATIENT', 1, charges[1]],
    ]
  }
  if (type !== 'charge') return [[type, type.toUpperCase(), 1, 0]]
  const [visitType, amount] = values
  const kind = kinds[visitType] ?? visitType.toUpperCase()
  const cents = readCents(amount)
  return care.includes(kind) && !Number.isNaN(cents) ? [[type, kind, 1, cents]] : null
}

export function importOverviewCsv(files, previous) {
  const seen = new Map(previous?.seen)
  const groups = new Map((previous?.groups ?? []).map(group => [groupKey(group), { ...group }]))
  const occupiedScopes = new Set([...groups.values()].map(({ date, code }) => `${date}:${code}`))
  const summaryScopes = new Set(previous?.summaryScopes)
  const ranges = new Map([...(previous?.ranges ?? [])].map(([type, entries]) => [type, [...entries]]))
  const departments = new Set(previous?.departments)
  let start = previous?.start ?? '', end = previous?.end ?? '', skipped = 0, rowCount = 0
  for (const [index, text] of files.entries()) {
    const rows = csvRows(text.replace(/^\ufeff/, ''))
    const header = rows.next().value?.map(column => column.toLowerCase()) ?? []
    const matches = layouts.filter(([, columns]) => columns.every(column => header.includes(column)))
    if (matches.length !== 1 || new Set(header).size !== header.length) {
      throw new Error(`第 ${index + 1} 个文件表头无法识别，请使用对应 CSV 模板。`)
    }
    const [type, columns] = matches[0]
    const positions = columns.map(column => header.indexOf(column))
    const range = { start: '', end: '' }
    let line = 1
    for (const row of rows) {
      line += 1
      rowCount += 1
      if (rowCount > 100000) throw new Error('一次最多导入 100,000 条记录。')
      if (row.length !== header.length) throw new Error(`第 ${index + 1} 个文件第 ${line} 行记录无效。`)
      const [id, date, rawCode, ...values] = positions.map(position => row[position])
      const code = rawCode || '__UNKNOWN__'
      const metrics = readMetrics(type, values)
      if (!id || !validDate(date) || !/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(code) || !metrics) {
        throw new Error(`第 ${index + 1} 个文件第 ${line} 行记录无效。`)
      }
      if (!range.start || date < range.start) range.start = date
      if (date > range.end) range.end = date
      const key = `${type}:${id}`
      const signature = type === 'overview' ? JSON.stringify([date, code, metrics])
        : `${date}:${code}:${metrics[0][1]}:${metrics[0][3]}`
      if (seen.has(key)) {
        if (seen.get(key) !== signature) throw new Error(`第 ${index + 1} 个文件第 ${line} 行重复编号的数据冲突。`)
        skipped += 1
        continue
      }
      const scope = `${date}:${code}`
      if (summaryScopes.has(scope) || (type === 'overview' && occupiedScopes.has(scope))) {
        throw new Error(`第 ${index + 1} 个文件第 ${line} 行与已导入的同日同科室数据重叠。`)
      }
      occupiedScopes.add(scope)
      if (type === 'overview') summaryScopes.add(scope)
      seen.set(key, signature)
      if (seen.size > 100000) throw new Error('最多累计导入 100,000 条记录。')
      if (!start || date < start) start = date
      if (date > end) end = date
      departments.add(code)
      for (const [type, kind, count, cents] of metrics) {
        const keyForGroup = groupKey({ date, code, type, kind })
        let group = groups.get(keyForGroup)
        if (!group) {
          group = { date, code, type, kind, count: 0, cents: 0 }
          groups.set(keyForGroup, group)
        }
        group.count += count
        group.cents += cents
      }
    }
    if (range.start) {
      for (const kind of type === 'overview' ? detailTypes : [type]) {
        const entries = ranges.get(kind) ?? []
        if (!entries.some(entry => entry.start === range.start && entry.end === range.end)) entries.push(range)
        ranges.set(kind, entries)
      }
    }
  }
  if (!rowCount) throw new Error('CSV 中没有可导入的记录。')
  if (Date.parse(end) - Date.parse(start) > 3660 * 86400000) throw new Error('CSV 日期范围最多 10 年。')
  return { seen, groups: [...groups.values()], ranges, departments, summaryScopes, start, end, added: seen.size, skipped }
}

function groupKey({ date, code, type, kind }) {
  return `${date}:${code}:${type}:${kind}`
}

export function filterImportedOverview(dataset, selected = {}) {
  const start = selected.start_date || dataset.start
  const end = selected.end_date || dataset.end
  const dept = selected.dept_code || null
  if (!validDate(start) || !validDate(end) || start > end || start < dataset.start || end > dataset.end) {
    throw new Error(`请选择 ${dataset.start} 至 ${dataset.end} 内的日期。`)
  }
  if (dept && !dataset.departments.has(dept)) throw new Error('请选择 CSV 中的科室。')
  const activity = []
  const days = new Map()
  const charges = new Map()
  const departments = new Map()
  const hasCoverage = type => dataset.ranges.get(type)?.some(range => range.start <= end && range.end >= start)
  const charging = hasCoverage('charge')
  const outpatient = hasCoverage('outpatient')
  const inpatient = hasCoverage('inpatient')
  let total = 0
  for (const { date, code, type, kind, count, cents } of dataset.groups) {
    if (date < start || date > end || (dept && dept !== code)) continue
    if (!departments.has(code)) departments.set(code, { code, label: code === '__UNKNOWN__' ? '未标注科室' : code,
      outpatient: 0, inpatient: 0, cents: 0 })
    const row = departments.get(code)
    if (type === 'charge') {
      row.cents += cents
      total += cents
      days.set(date, (days.get(date) ?? 0) + cents)
      charges.set(kind, (charges.get(kind) ?? 0) + cents)
    } else {
      row[type] += count
      activity.push({ date, kind, count })
    }
  }
  return {
    filters: { start_date: start, end_date: end, dept_code: dept },
    activity,
    ranges: dataset.ranges,
    charge_total: charging ? total / 100 : null,
    daily_charges: charging ? [...days].map(([date, cents]) => ({ date, amount: cents / 100 })) : null,
    charges: charging ? care.filter(kind => kind !== 'OTHER' || charges.has(kind))
      .map(name => ({ name, value: (charges.get(name) ?? 0) / 100 })) : [],
    departments: [...departments.values()].map(({ cents, ...row }) => ({
      ...row, amount: charging ? cents / 100 : null,
      outpatient: outpatient ? row.outpatient : null,
      inpatient: inpatient ? row.inpatient : null,
    })),
  }
}

export function appendedScope(publication, imported, selected = {}) {
  const { available_start_date: first, available_end_date: last } = publication
  if (imported.start <= last) throw new Error(`追加数据的日期须晚于课程数据截止日 ${last}。`)
  const start = selected.start_date || first
  const end = selected.end_date || imported.end
  if (Date.parse(imported.end) - Date.parse(first) > 3660 * 86400000) throw new Error('合并日期范围最多 10 年。')
  if (!validDate(start) || !validDate(end) || start < first || end > imported.end || start > end) {
    throw new Error(`请选择 ${first} 至 ${imported.end} 内的日期。`)
  }
  const filters = { start_date: start, end_date: end, dept_code: selected.dept_code || null }
  return { filters, course: start <= last ? { ...filters, end_date: end < last ? end : last } : null }
}

function moneySum(first, second) {
  if (first === null || second === null || (first === undefined && second === undefined)) return null
  return (Math.round(Number(first ?? 0) * 100) + Math.round(Number(second ?? 0) * 100)) / 100
}

export function appendOverview(course, imported, filters) {
  const { start_date: start, end_date: end, dept_code: dept } = filters
  const extra = end >= imported.start && (!dept || imported.departments.has(dept))
    ? filterImportedOverview(imported, { ...filters, start_date: start < imported.start ? imported.start : start }) : null
  const ranges = new Map(imported.ranges)
  if (course) {
    const range = { start: course.filters.start_date, end: course.filters.end_date }
    for (const kind of detailTypes) ranges.set(kind, [range, ...(ranges.get(kind) ?? [])])
  }
  const departments = new Map(course?.departments.map(row => [row.code, row]))
  for (const row of extra?.departments ?? []) {
    const base = departments.get(row.code)
    departments.set(row.code, {
      code: row.code, label: base?.label ?? row.label,
      outpatient: base ? base.outpatient + (row.outpatient ?? 0) : row.outpatient,
      inpatient: base ? base.inpatient + (row.inpatient ?? 0) : row.inpatient,
      amount: moneySum(base?.amount, row.amount ?? undefined),
    })
  }
  const charges = new Map(course?.charges.map(row => [row.name, row]))
  for (const row of extra?.charges ?? []) {
    const base = charges.get(row.name)
    charges.set(row.name, { ...row, value: moneySum(base?.value, row.value) })
  }
  const dailyCharges = [...(course?.daily_charges ?? []), ...(extra?.daily_charges ?? [])]
  return {
    filters, ranges, departments: [...departments.values()], charges: [...charges.values()],
    activity: [...(course?.activity ?? []), ...(extra?.activity ?? [])],
    daily_charges: course || extra?.daily_charges ? dailyCharges : null,
    charge_total: moneySum(course?.charge_total, extra?.charge_total ?? undefined),
  }
}
