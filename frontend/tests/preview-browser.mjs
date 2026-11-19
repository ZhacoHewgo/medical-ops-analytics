// Runs against the isolated server started by the Node 19 pytest fixture.
/* global document, innerWidth */
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const [base, resultDir] = process.argv.slice(2)
await mkdir(resultDir, { recursive: true })
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM,
  headless: true,
  args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: 'zh-CN' })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))

  const counts = async () => {
    const kpi = page.locator('.kpi-value')
    await kpi.waitFor({ state: 'visible' })
    return Number((await kpi.innerText()).replace(/[^0-9]/g, ''))
  }
  const apply = async () => {
    const response = page.waitForResponse(r => r.url().includes('/api/dashboard/outpatient-preview'))
    await page.getByRole('button', { name: '应用筛选' }).click()
    assert.equal((await response).status(), 200)
  }

  const fullResponse = page.waitForResponse(r => r.url().endsWith('/api/dashboard/outpatient-preview'))
  await page.goto(base + '/?source=preview', { waitUntil: 'domcontentloaded' })
  const full = await (await fullResponse).json()
  assert.equal(full.data.outpatient_visits, 4)
  assert.equal(await counts(), 4)
  await page.getByText(full.data.meta.build_run_id, { exact: true }).waitFor()
  await page.getByText('dwd_visit_detail', { exact: true }).waitFor()
  await page.screenshot({ path: join(resultDir, 'outpatient-preview.png'), fullPage: true })

  await page.getByLabel('科室代码').selectOption('CARD')
  await apply()
  assert.equal(await counts(), 3)
  await page.getByLabel('科室代码').selectOption('RESP')
  await page.getByLabel('结束日期').fill('2026-01-01')
  await apply()
  assert.equal(await counts(), 0)
  await page.getByRole('heading', { name: '当前范围暂无有效门诊记录' }).waitFor()

  await page.getByLabel('结束日期').fill('2026-01-05')
  const rejected = page.waitForResponse(r => r.url().includes('/api/dashboard/outpatient-preview'))
  await page.getByRole('button', { name: '应用筛选' }).click()
  assert.equal((await rejected).status(), 422)
  await page.getByRole('alert').getByText(/HTTP 422/).waitFor()
  assert.equal(await page.locator('.kpi-value').count(), 0)

  // The fixture rejects this request in the API using the same unavailable-source error path.
  await page.request.post(base + '/__rehearsal/source/unavailable')
  await page.getByRole('button', { name: '重置' }).click()
  await page.getByRole('alert').getByText(/HTTP 503/).waitFor()
  assert.equal(await page.locator('.kpi-value').count(), 0)
  await page.request.post(base + '/__rehearsal/source/available')
  await page.getByRole('button', { name: '重试本次请求' }).click()
  assert.equal(await counts(), 4)

  await page.setViewportSize({ width: 390, height: 844 })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  await page.screenshot({ path: join(resultDir, 'outpatient-preview-mobile.png'), fullPage: true })

  const csvResponse = page.waitForResponse(r => r.url().endsWith('/api/dashboard/overview'))
  await page.getByRole('link', { name: '返回 CSV 看板' }).click()
  assert.equal((await csvResponse).status(), 200)
  assert.equal(await counts(), 4)
  await page.getByRole('heading', { name: '数据质量摘要' }).waitFor()
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ all: 4, department: 3, empty: 0, outOfCoverage: 422, unavailable: 503,
    recovery: 4, mobileWidth: 390, csv: 4, pageErrors: errors.length }))
} finally {
  await browser.close()
}
