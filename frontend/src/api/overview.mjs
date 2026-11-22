const dayLength = 86400000
const dateNumber = date => Date.parse(`${date}T00:00:00Z`)
const dateString = time => new Date(time).toISOString().slice(0, 10)
const fields = ['outpatient', 'inpatient', 'amount']

export function overviewDays({ filters, activity, daily_charges: charges, ranges }) {
  const visits = { OUTPATIENT: new Map(), INPATIENT: new Map() }
  for (const { date, kind, count } of activity) {
    const group = visits[kind]
    if (group) group.set(date, (group.get(date) ?? 0) + count)
  }
  const amounts = new Map((charges ?? []).map(({ date, amount }) => {
    const cents = Math.round(Number(amount) * 100)
    return [date, amount != null && Number.isSafeInteger(cents) ? cents : null]
  }))
  const inRange = (key, date) => !ranges || ranges.get(key)?.some(range => range.start <= date && date <= range.end)
  const rows = []
  for (let time = dateNumber(filters.start_date), last = dateNumber(filters.end_date); time <= last; time += dayLength) {
    const date = dateString(time)
    const amount = amounts.has(date) ? amounts.get(date) : charges && inRange('charge', date) ? 0 : null
    rows.push({ date,
      outpatient: inRange('outpatient', date) ? visits.OUTPATIENT.get(date) ?? 0 : null,
      inpatient: inRange('inpatient', date) ? visits.INPATIENT.get(date) ?? 0 : null,
      amount, chargeRecorded: amounts.has(date) && amount !== null })
  }
  return rows
}

function total(rows, partial = false) {
  return Object.fromEntries(fields.map(key => {
    const values = rows.map(row => row[key]).filter(value => value !== null)
    return [key, values.length && (partial || values.length === rows.length)
      ? values.reduce((sum, value) => sum + value, 0) : null]
  }))
}

function periodBuckets(rows, grain) {
  if (grain === 'day') return rows.map(row => [row])
  const buckets = new Map()
  for (const row of rows) {
    const monday = dateNumber(row.date) - (new Date(dateNumber(row.date)).getUTCDay() + 6) % 7 * dayLength
    const key = grain === 'month' ? row.date.slice(0, 7) : dateString(monday)
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(row)
  }
  return [...buckets.values()]
}

export function overviewPeriods(rows, grain, partial = false) {
  if (grain === 'day') return rows.map(row => ({ ...row, end: row.date }))
  return periodBuckets(rows, grain).map(bucket => ({
    date: bucket[0].date, end: bucket.at(-1).date, ...total(bucket, partial),
  }))
}

export function overviewBedPeriods(rows, grain) {
  return periodBuckets(rows, grain).map(bucket => {
    const occupied = bucket.reduce((sum, row) => sum + row.occupied, 0)
    const available = bucket.reduce((sum, row) => sum + row.available, 0)
    return { date: grain === 'month' ? bucket[0].date.slice(0, 7) : bucket[0].date,
      visits: available ? Number((occupied / available * 100).toFixed(2)) : null }
  })
}

export function overviewFocus(rows) {
  if (!rows.length) return []
  const recent = rows.slice(-7)
  const before = rows.length >= 14 ? total(rows.slice(-14, -7)) : null
  const current = total(recent)
  return fields.map(key => ({
    key, value: current[key], days: recent.length,
    change: current[key] !== null && before?.[key] > 0 ? current[key] / before[key] - 1 : null,
  }))
}

export function sparkline(values) {
  if (values.length < 2 || values.some(value => value == null)) return ''
  const min = Math.min(...values)
  const spread = Math.max(...values) - min
  return values.map((value, i) => `${i * 100 / (values.length - 1)},${spread ? 27 - (value - min) * 23 / spread : 15}`).join(' ')
}
