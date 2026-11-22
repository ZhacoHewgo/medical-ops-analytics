// Executed by the opt-in browser test in test_operations.py.
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const [base] = process.argv.slice(2)
assert.equal(new URL(base).hostname, '127.0.0.1')
const names = { admin: process.env.MOPS_BROWSER_ADMIN, analyst: process.env.MOPS_BROWSER_ANALYST,
  department: process.env.MOPS_BROWSER_DEPARTMENT }
const password = process.env.MOPS_BROWSER_PASSWORD
assert.ok(names.admin && names.analyst && names.department && password)
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM, headless: true, args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  const page = await context.newPage()
  const errors = []
  const writes = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/ops/') && request.method() === 'POST') writes.push(path)
  })
  async function signIn(tab, role) {
    await tab.goto(base + '/login')
    await tab.getByLabel('用户名').fill(names[role])
    await tab.getByLabel('密码', { exact: true }).fill(password)
    await tab.getByRole('button', { name: '登录', exact: true }).click()
    await tab.waitForURL('**/outpatient')
  }
  async function checked(action, method, path) {
    const wait = page.waitForResponse(response =>
      response.request().method() === method && new URL(response.url()).pathname === path)
    await action()
    const response = await wait
    assert.equal(response.status(), 200)
    return (await response.json()).data
  }
  const sources = '/api/ops/sources'
  const tasks = '/api/ops/tasks'

  await page.goto(base + '/ops/sources')
  await page.waitForURL(url => url.pathname === '/login')
  await signIn(page, 'admin')
  await page.getByRole('link', { name: '数据源与任务' }).click()
  const outpatient = page.getByRole('row', { name: /门诊/ })
  await outpatient.getByText('已登记', { exact: true }).waitFor()
  assert.equal(await page.getByText('已登记', { exact: true }).count(), 6)
  assert.equal(await page.getByRole('columnheader', { name: '源文件' }).count(), 1)
  for (const name of ['his_outpatient', 'his_inpatient', 'his_charge', 'emr_record', 'lis_report', 'finance_excel']) {
    assert.equal(await page.getByRole('cell', { name: name + '.csv', exact: true }).count(), 1)
  }
  await outpatient.getByRole('button', { name: '检查文件' }).click()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  assert.deepEqual(writes, [])
  await outpatient.getByRole('button', { name: '检查文件' }).click()
  assert.equal((await checked(() => page.getByRole('button', { name: '确认检查' }).click(), 'POST',
    sources + '/his_outpatient/check')).outcome, 'SUCCESS')
  await page.getByText('登记文件检查通过。').waitFor()
  assert.equal(await page.getByText(/mock_his\/|files\//).count(), 0)

  await checked(() => page.getByRole('link', { name: '任务管理' }).click(), 'GET', tasks)
  const extract = page.getByRole('row', { name: /ODS 采集/ })
  await extract.getByText('成功', { exact: true }).waitFor()
  await page.getByRole('button', { name: '手动采集' }).click()
  assert.equal((await checked(() => page.getByRole('button', { name: '确认采集' }).click(), 'POST',
    tasks + '/extract/run')).outcome, 'SUCCESS')
  await page.getByText('本次 ODS 采集成功。').waitFor()
  await extract.getByText('18', { exact: true }).waitFor()
  assert.equal(await page.getByText(/\/backend\/etl|SIM_PRIVATE/).count(), 0)

  await context.setOffline(true)
  await page.getByRole('button', { name: '刷新任务' }).click()
  await page.getByRole('alert').getByText('操作响应中断', { exact: false }).waitFor()
  await context.setOffline(false)
  await checked(() => page.getByRole('button', { name: '刷新任务' }).click(), 'GET', tasks)

  const analyst = await context.newPage()
  await signIn(analyst, 'analyst')
  await analyst.getByRole('link', { name: '数据源与任务' }).click()
  await analyst.getByRole('row', { name: /门诊/ }).getByText('已登记', { exact: true }).waitFor()
  assert.equal(await analyst.getByRole('button', { name: '检查文件' }).count(), 0)
  await analyst.getByRole('link', { name: '任务管理' }).click()
  await analyst.getByRole('row', { name: /ODS 采集/ }).getByText('成功', { exact: true }).waitFor()
  assert.equal(await analyst.getByRole('button', { name: '手动采集' }).count(), 0)

  const department = await context.newPage()
  await signIn(department, 'department')
  assert.equal(await department.getByRole('link', { name: '数据源与任务' }).count(), 0)
  await department.goto(base + '/ops/tasks')
  await department.waitForURL('**/outpatient?denied=ops')
  await department.getByText('当前账号无权查看数据源与任务。').waitFor()
  assert.deepEqual(writes, [sources + '/his_outpatient/check', tasks + '/extract/run'])
  assert.deepEqual(errors, [])
  console.log('OPERATIONS_BROWSER_OK checked=1 manual_ods=1 rows=18 analyst_read_only=1 department_denied=1')
} finally {
  await browser.close()
}
