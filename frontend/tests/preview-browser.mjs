// Runs against the isolated server started by the Node 19 pytest fixture.
/* global document, innerWidth */
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
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
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, locale: 'zh-CN' })
  const errors = []
  const legacyBrowserRequests = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/dashboard/')) legacyBrowserRequests.push(request.url())
  })

  await page.goto(base + '/?source=preview', { waitUntil: 'domcontentloaded' })
  await page.waitForURL(url => url.pathname === '/login' && url.searchParams.get('redirect') === '/overview')
  await page.getByRole('heading', { name: '登录', exact: true }).waitFor()
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  assert.deepEqual(legacyBrowserRequests, [])

  async function getPreview(status, filters = {}) {
    const response = await page.request.get(base + '/api/dashboard/outpatient-preview', { params: filters })
    assert.equal(response.status(), status)
    return (await response.json()).data
  }

  const full = await getPreview(200)
  assert.equal(full.outpatient_visits, 4)
  assert.ok(full.meta.build_run_id)
  assert.equal((await getPreview(200, { dept_code: 'CARD' })).outpatient_visits, 3)
  assert.equal((await getPreview(200, { dept_code: 'RESP', start_date: '2026-01-01', end_date: '2026-01-01' })).outpatient_visits, 0)
  await getPreview(422, { start_date: '2026-01-01', end_date: '2026-01-05' })

  assert.ok((await page.request.post(base + '/__rehearsal/source/unavailable')).ok())
  await getPreview(503)
  assert.ok((await page.request.post(base + '/__rehearsal/source/available')).ok())
  assert.equal((await getPreview(200)).outpatient_visits, 4)

  const csv = await page.request.get(base + '/api/dashboard/overview')
  assert.ok(csv.ok())
  assert.equal((await csv.json()).data.outpatient_visits, 4)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ legacyRedirect: true, all: 4, department: 3, empty: 0, outOfCoverage: 422, unavailable: 503,
    recovery: 4, mobileWidth: 390, csv: 4, pageErrors: errors.length }))
} finally {
  await browser.close()
}
