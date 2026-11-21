// Executed by the opt-in browser check in test_outpatient_ads_api.py.
/* global document, innerWidth */
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const [base, output] = process.argv.slice(2)
assert.equal(new URL(base).hostname, '127.0.0.1')
const usernames = { analyst: process.env.MOPS_BROWSER_ANALYST, department: process.env.MOPS_BROWSER_DEPARTMENT }
const password = process.env.MOPS_BROWSER_PASSWORD
assert.ok(usernames.analyst && usernames.department && password)
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM, headless: true, args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  const page = await context.newPage()
  const errors = []
  const route = '/api/v1/reports/outpatient'
  const exportRoute = '/api/v1/reports/export'
  const report = page.getByTestId('report-table')
  const headings = report.locator('thead th')
  const query = () => page.getByRole('button', { name: '查询', exact: true }).click()
  const exportButton = page.getByRole('button', { name: '导出 Excel' })
  page.on('pageerror', error => errors.push(error.message))

  const read = async (action, path = route, status = 200) => {
    const wait = page.waitForResponse(response => new URL(response.url()).pathname === path)
    await action()
    const response = await wait
    assert.equal(response.status(), status)
    return response
  }
  const assertHeading = async (text) => page.getByRole('heading', { name: text, exact: true }).waitFor()

  async function login(role) {
    await page.getByLabel('用户名').fill(usernames[role])
    await page.getByLabel('密码', { exact: true }).fill(password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL('**/outpatient')
    await read(() => page.getByRole('link', { name: '门诊报表' }).click())
    await assertHeading('门诊报表')
  }

  await page.goto(base + '/reports')
  await page.waitForURL(url => url.pathname === '/login')
  await login('analyst')
  if (process.env.MOPS_BROWSER_CALENDAR === '1') {
    await assertHeading('日期 × 科室 · 5 人次')
    await page.getByLabel('开始日期').fill('2025-02-01')
    await page.getByLabel('结束日期').fill('2025-02-28')
    await read(query)
    await assertHeading('日期 × 科室 · 2 人次')
    await page.getByText('+100%（对比 2025-01 的 1 人次）', { exact: true }).waitFor()
    await page.getByLabel('变化率').selectOption('year')
    await page.getByText('0%（对比 2024-02 的 2 人次）', { exact: true }).waitFor()
    await page.getByLabel('科室', { exact: true }).selectOption('SIM_DEPT_02')
    await read(query)
    await page.getByText('暂无可比增长率。对比期门诊量为 0。').waitFor()
    console.log('REPORT_COMPARISON_OK mom=100 yoy=0 zero_denominator=null')
  } else {
    for (const [key, title, columns] of [
      ['day', '按日', ['日期', '门诊人次', '详情']],
      ['department', '按科室', ['科室', '门诊人次', '详情']],
      ['month_department', '月份 × 科室', ['月份', '科室', '门诊人次', '详情']],
      ['day_department', '日期 × 科室', ['日期', '科室', '门诊人次']],
    ]) {
      await page.getByLabel('汇总方式').selectOption(key)
      const { data } = await (await read(query)).json()
      assert.equal(data.group_by, key)
      assert.equal(data.outpatient_visits, 4)
      await assertHeading(title + ' · 4 人次')
      assert.deepEqual(await headings.allTextContents(), columns)
    }

    await page.getByLabel('汇总方式').selectOption('department')
    await read(query)
    await assertHeading('按科室 · 4 人次')
    const target = report.getByRole('row', { name: /SIM_DEPT_01/ })
    const { data: scoped } = await (await read(() => target.getByRole('button', { name: '下钻' }).click())).json()
    assert.equal(scoped.filters.dept_code, 'SIM_DEPT_01')
    await assertHeading('日期 × 科室 · 3 人次')
    await page.getByLabel('变化率').selectOption('year')
    await page.getByText('选择一个完整自然月后可比较。').waitFor()
    await read(() => page.getByRole('button', { name: '返回上一级' }).click())
    await assertHeading('按科室 · 4 人次')
    await page.setViewportSize({ width: 360, height: 800 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.setViewportSize({ width: 1920, height: 1080 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))

    await page.getByLabel('开始日期').fill('2025-01-04')
    await page.getByLabel('结束日期').fill('2025-01-04')
    await read(query)
    await page.getByText('当前范围暂无门诊记录').waitFor()
    assert.equal(await report.count(), 0)
    await read(() => page.getByRole('button', { name: '重置' }).click())
    await assertHeading('日期 × 科室 · 4 人次')

    await page.route('**/api/v1/reports/export', intercepted => {
      const body = intercepted.request().postDataJSON()
      body.expected_build_run_id = 'ffffffff-ffff-4fff-bfff-ffffffffffff'
      return intercepted.continue({ postData: JSON.stringify(body) })
    }, { times: 1 })
    await read(() => exportButton.click(), exportRoute, 409)
    await page.getByRole('alert').getByText('数据发布批次已更新，请重新查询后导出。').waitFor()
    await context.setOffline(true)
    await exportButton.click()
    await page.getByRole('alert').getByText('导出失败，请检查网络后重试。').waitFor()
    await query()
    await page.getByRole('alert').getByText('查询失败', { exact: false }).waitFor()
    assert.equal(await report.count(), 0)
    await context.setOffline(false)
    await read(() => page.getByRole('button', { name: '重试查询' }).click())
    await assertHeading('日期 × 科室 · 4 人次')

    await page.getByRole('link', { name: '返回门诊驾驶舱' }).click()
    await page.getByRole('button', { name: '退出登录' }).click()
    await page.waitForURL(url => url.pathname === '/login')
    await login('department')
    assert.equal(await page.getByLabel('科室', { exact: true }).isDisabled(), true)
    await assertHeading('日期 × 科室 · 3 人次')
    await page.route('**/api/v1/reports/outpatient**', intercepted => {
      const url = new URL(intercepted.request().url())
      url.searchParams.set('dept_code', 'SIM_DEPT_02')
      return intercepted.continue({ url: url.href })
    }, { times: 1 })
    await read(query, route, 403)
    await page.getByRole('alert').getByText('当前账号没有查看或导出该科室的权限。').waitFor()
    assert.equal(await report.count(), 0)
    await read(() => page.getByRole('button', { name: '重试查询' }).click())
    await assertHeading('日期 × 科室 · 3 人次')

    const wait = page.waitForEvent('download')
    const response = await read(() => exportButton.click(), exportRoute)
    const body = response.request().postDataJSON()
    assert.equal(body.dept_code, 'SIM_DEPT_01')
    assert.equal(body.group_by, 'day_department')
    assert.ok(body.expected_build_run_id)
    const download = await wait
    assert.equal(download.suggestedFilename(), '门诊人次.xlsx')
    await download.saveAs(join(output, 'report.xlsx'))

    const waitForPdf = page.waitForEvent('download')
    const pdfResponse = await read(() => page.getByRole('button', { name: '导出 PDF' }).click(), exportRoute)
    assert.deepEqual(pdfResponse.request().postDataJSON(), { ...body, format: 'pdf' })
    assert.equal(pdfResponse.headers()['content-type'], 'application/pdf')
    const pdf = await waitForPdf
    assert.equal(pdf.suggestedFilename(), '门诊人次.pdf')
    const pdfPath = join(output, 'report.pdf')
    await pdf.saveAs(pdfPath)
    assert.equal((await readFile(pdfPath)).subarray(0, 5).toString(), '%PDF-')
    console.log('REPORT_BROWSER_OK groups=4 hospital=4 department=3 empty=0 status=403,409 offline=1')
  }
  assert.deepEqual(errors, [])
} finally {
  await browser.close()
}
