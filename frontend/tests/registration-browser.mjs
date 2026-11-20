// Exercises real registration against the isolated MySQL fixture.
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const [base] = process.argv.slice(2)
assert.equal(new URL(base).hostname, '127.0.0.1')
const username = process.env.MOPS_BROWSER_ADMIN
const analystName = process.env.MOPS_BROWSER_ANALYST
const password = process.env.MOPS_BROWSER_PASSWORD
const dataset = process.env.MOPS_REGISTRY_DATASET
const batch = process.env.MOPS_REGISTRY_BATCH
assert.ok(username && analystName && password && dataset && batch)
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM, headless: true, args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = []
  const writes = []
  const url = '/api/ops/sources/registration'
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() === 'PUT' && new URL(request.url()).pathname === url) writes.push(request.method())
  })
  const button = (tab, label) => tab.getByRole('button', { name: label, exact: true })
  async function signIn(tab, name) {
    await tab.goto(base + '/login')
    await tab.getByLabel('用户名').fill(name)
    await tab.getByLabel('密码', { exact: true }).fill(password)
    await button(tab, '登录').click()
    await tab.waitForURL('**/outpatient')
    await tab.getByRole('link', { name: '数据源与任务' }).click()
  }
  async function submit(status) {
    const result = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === url)
    await button(page, '确认登记').click()
    const response = await result
    assert.equal(response.status(), status)
    return response.json()
  }
  async function prepare(target) {
    await page.getByLabel('数据集代码').fill(target)
    await page.getByLabel('登记批次日期').fill(batch)
    await button(page, '继续登记').click()
  }

  await signIn(page, username)
  const editor = page.getByRole('region', { name: '数据源批次登记' })
  const sourceTable = page.getByRole('table')
  await editor.getByLabel('数据集代码').waitFor()
  await prepare(dataset)
  await button(page, '取消登记').click()
  assert.deepEqual(writes, [])

  await prepare(dataset + '_missing')
  await submit(422)
  await editor.getByRole('alert').getByText('请核对数据集代码、批次日期和六类登记文件。').waitFor()
  assert.equal(await sourceTable.getByText(dataset, { exact: true }).count(), 0)

  await page.route('**/api/ops/sources/registration', route => {
    const body = route.request().postDataJSON()
    body.expected_version = 'f'.repeat(64)
    return route.continue({ postData: JSON.stringify(body) })
  }, { times: 1 })
  await prepare(dataset)
  await submit(409)
  await editor.getByRole('alert').getByText('操作发生冲突，请刷新状态后再试。').waitFor()
  await button(page, '刷新登记版本').click()
  await prepare(dataset)
  assert.deepEqual((await submit(200)).data.dataset_id, dataset)
  await editor.getByText('六类数据源登记已更新。').waitFor()
  await sourceTable.getByText(dataset, { exact: true }).last().waitFor()
  assert.equal(await sourceTable.getByText(dataset, { exact: true }).count(), 6)
  assert.equal(await editor.getByText(/mock_his|\.csv/).count(), 0)

  const analyst = await browser.newPage()
  await signIn(analyst, analystName)
  await analyst.getByRole('table').getByText(dataset, { exact: true }).last().waitFor()
  assert.equal(await analyst.getByRole('region', { name: '数据源批次登记' }).count(), 0)
  assert.equal(await analyst.getByRole('button', { name: '继续登记' }).count(), 0)
  assert.equal(writes.length, 3)
  assert.deepEqual(errors, [])
  console.log('REGISTRATION_BROWSER_OK sources=6 canceled=1 invalid=422 conflict=409 analyst_read_only=1')
} finally {
  await browser.close()
}
