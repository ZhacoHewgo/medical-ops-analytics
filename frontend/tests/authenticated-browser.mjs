// Run against a loopback FastAPI server with the test_outpatient_ads_api fixture.
/* global document, innerWidth */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const base = new URL(process.argv[2])
assert.equal(base.hostname, '127.0.0.1')
const credentials = { analyst: process.env.MOPS_BROWSER_ANALYST, department: process.env.MOPS_BROWSER_DEPARTMENT }
const password = process.env.MOPS_BROWSER_PASSWORD
assert.ok(credentials.analyst && credentials.department && password, 'Missing isolated fixture credentials')
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM,
  headless: true,
  args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, locale: 'zh-CN' })
  const errors = []
  const endpoint = '/api/v1/outpatient/summary'
  const requests = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => requests.push(new URL(request.url()).pathname))

  async function readSummary(action, status) {
    const result = page.waitForResponse(response => new URL(response.url()).pathname === endpoint)
    await action()
    const response = await result
    assert.equal(response.status(), status)
    if (status === 200) return response.json()
  }

  async function login(role, secret = password) {
    await page.getByLabel('用户名').fill(credentials[role])
    await page.getByLabel('密码', { exact: true }).fill(secret)
    await page.getByRole('button', { name: '登录', exact: true }).click()
  }

  async function checkCount(expected, payload) {
    assert.equal(payload.data.outpatient_visits, expected)
    const count = page.locator('.kpi-value')
    await count.waitFor()
    assert.equal(Number((await count.innerText()).replace(/[^0-9]/g, '')), expected)
  }

  async function checkWidth(width, height) {
    await page.setViewportSize({ width, height })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  }

  const apply = () => page.getByRole('button', { name: '应用筛选' }).click()
  await page.goto(new URL('/outpatient', base).href)
  await page.waitForURL(url => url.pathname === '/login')
  assert.equal(requests.includes(endpoint), false)
  await login('analyst', password + '-invalid')
  await page.getByRole('alert').getByText('用户名或密码错误', { exact: false }).waitFor()
  const full = await readSummary(() => login('analyst'), 200)
  await checkCount(4, full)
  assert.equal(await page.getByRole('heading', { name: '集市来源与构建' }).count(), 0)
  await page.locator('canvas').first().waitFor()
  await page.getByRole('heading', { name: '日均门诊（有记录日）' }).waitFor()
  const peak = full.data.daily_trend.reduce((highest, row) => row.visits > highest.visits ? row : highest)
  const peakDay = await readSummary(() => page.getByRole('button', { name: '查看峰值日' }).click(), 200)
  await checkCount(peak.visits, peakDay)
  assert.equal(peakDay.data.filters.start_date, peak.date)
  assert.equal(peakDay.data.filters.end_date, peak.date)
  await page.getByRole('heading', { name: '当日科室贡献' }).waitFor()
  await checkCount(4, await readSummary(() => page.getByRole('button', { name: '返回上一级' }).click(), 200))
  await checkWidth(1920, 1080)
  await checkWidth(360, 800)
  assert.equal(await page.evaluate(() => JSON.stringify(sessionStorage).includes('password')), false)
  await checkCount(4, await readSummary(() => page.reload(), 200))

  const openRange = () => page.locator('.overview-date-range summary').click()
  const closeRange = () => page.getByRole('button', { name: '确定', exact: true }).click()
  await openRange()
  await page.getByLabel('开始日期').fill('2025-01-04')
  await page.getByLabel('结束日期').fill('2025-01-04')
  await closeRange()
  await checkCount(0, await readSummary(apply, 200))
  await page.getByRole('heading', { name: '当前范围暂无有效门诊记录' }).waitFor()
  await openRange()
  await page.getByLabel('结束日期').fill('2025-01-05')
  await closeRange()
  await readSummary(apply, 422)
  await page.getByRole('alert').getByText('HTTP 422', { exact: false }).waitFor()
  assert.equal(await page.locator('.kpi-value').count(), 0)
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.waitForURL(url => url.pathname === '/login')

  await checkCount(3, await readSummary(() => login('department'), 200))
  const department = page.getByLabel('科室代码')
  assert.equal(await department.isDisabled(), true)
  assert.deepEqual(await department.locator('option').allTextContents(), ['SIM_DEPT_01'])

  // Send a contradictory filter to the real API to verify error handling as well as server-side scope.
  const widen = route => {
    const url = new URL(route.request().url())
    url.searchParams.set('dept_code', 'SIM_DEPT_02')
    return route.continue({ url: url.href })
  }
  await page.route('**/api/v1/outpatient/summary**', widen, { times: 1 })
  await readSummary(apply, 403)
  await page.getByRole('alert').getByText('HTTP 403', { exact: false }).waitFor()
  assert.equal(await page.locator('.kpi-value').count(), 0)
  await checkCount(3, await readSummary(() => page.getByRole('button', { name: '重试本次请求' }).click(), 200))

  assert.ok((await page.request.post(new URL('/__test/publication/off', base).href)).ok())
  await readSummary(apply, 503)
  await page.getByRole('alert').getByText('HTTP 503', { exact: false }).waitFor()
  assert.equal(await page.locator('.kpi-value').count(), 0)
  assert.ok((await page.request.post(new URL('/__test/publication/on', base).href)).ok())
  await checkCount(3, await readSummary(() => page.getByRole('button', { name: '重试本次请求' }).click(), 200))

  assert.ok((await page.request.post(new URL('/__test/account/revoke', base).href)).ok())
  await readSummary(apply, 401)
  await page.waitForURL('**/login?expired=1')
  await page.getByRole('status').getByText('登录已失效', { exact: false }).waitFor()
  assert.equal(await page.evaluate(() => sessionStorage.getItem('mops.auth.session')), null)
  assert.equal(await page.locator('.kpi-value').count(), 0)

  assert.equal(requests.includes('/api/dashboard/overview'), false)
  assert.equal(requests.includes('/api/dashboard/outpatient-preview'), false)
  assert.deepEqual(errors, [])
  console.log('DASHBOARD_BROWSER_OK analyst=4 department=3 empty=0 statuses=401,403,422,503 widths=360,1920')
} finally {
  await browser.close()
}
