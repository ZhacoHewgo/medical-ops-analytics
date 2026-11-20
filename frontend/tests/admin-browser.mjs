// Executed by the opt-in browser test in test_account_admin.py.
/* global document, innerWidth */
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
    if (path.startsWith('/api/auth/users') && request.method() !== 'GET') writes.push(request.method())
  })
  async function signIn(tab, role) {
    await tab.getByLabel('用户名').fill(names[role])
    await tab.getByLabel('密码', { exact: true }).fill(password)
    await tab.getByRole('button', { name: '登录', exact: true }).click()
  }
  async function checked(action, method, path, status) {
    const wait = page.waitForResponse(response =>
      response.request().method() === method && path.test(new URL(response.url()).pathname))
    await action()
    const response = await wait
    assert.equal(response.status(), status)
    return response
  }
  const listPath = /^\/api\/auth\/users$/
  const userPath = /^\/api\/auth\/users\/\d+$/
  const target = () => page.getByRole('row', { name: new RegExp(names.department) })
  const button = label => page.getByRole('button', { name: label, exact: true })
  const prepare = () => button('继续').click()

  await page.goto(base + '/admin/users')
  await page.waitForURL('**/login')
  await signIn(page, 'admin')
  await page.waitForURL('**/outpatient')
  await checked(() => page.getByRole('link', { name: '用户管理' }).click(), 'GET', listPath, 200)
  await page.getByRole('row', { name: new RegExp(names.admin) }).getByText('受保护').waitFor()

  await page.getByLabel('用户名').fill(names.department)
  await page.getByLabel('初始密码').fill(password)
  await page.getByLabel('角色', { exact: true }).selectOption('department')
  await page.getByLabel('科室代码').fill('SIM_DEPT_01')
  await prepare()
  await button('取消确认').click()
  assert.deepEqual(writes, [])
  await prepare()
  await checked(() => button('确认创建').click(), 'POST', listPath, 201)
  await page.getByText('账号已创建。', { exact: true }).waitFor()
  await target().getByText('SIM_DEPT_01', { exact: true }).waitFor()
  assert.equal(await page.getByLabel('初始密码').inputValue(), '')

  const dept = await context.newPage()
  dept.on('pageerror', error => errors.push(error.message))
  await dept.goto(base + '/login')
  await signIn(dept, 'department')
  await dept.waitForURL('**/outpatient')
  assert.equal(await dept.getByRole('link', { name: '用户管理' }).count(), 0)
  await dept.goto(base + '/admin/roles')
  await dept.waitForURL('**/outpatient?denied=admin')
  await dept.getByText('只有管理员可以打开用户管理。').waitFor()

  await target().getByRole('button', { name: '调整' }).click()
  await page.getByLabel('科室代码').fill('SIM_DEPT_02')
  await prepare()
  await checked(() => button('确认更新').click(), 'PATCH', userPath, 200)
  await target().getByText('SIM_DEPT_02', { exact: true }).waitFor()
  await dept.reload()
  await dept.waitForURL('**/login?expired=1')

  await target().getByRole('button', { name: '调整' }).click()
  await page.getByLabel('状态', { exact: true }).selectOption({ label: '停用' })
  await prepare()
  await checked(() => button('确认更新').click(), 'PATCH', userPath, 200)
  await target().getByText('停用', { exact: true }).waitFor()
  await signIn(dept, 'department')
  await dept.getByRole('alert').getByText('账号已停用', { exact: false }).waitFor()

  await target().getByRole('button', { name: '调整' }).click()
  await page.getByLabel('状态', { exact: true }).selectOption({ label: '启用' })
  await prepare()
  await checked(() => button('确认更新').click(), 'PATCH', userPath, 200)
  await target().getByText('启用', { exact: true }).waitFor()
  await signIn(dept, 'department')
  await dept.waitForURL('**/outpatient')
  assert.deepEqual(writes, ['POST', 'PATCH', 'PATCH', 'PATCH'])

  await context.setOffline(true)
  await button('刷新列表').click()
  await page.getByRole('alert').getByText('无法连接账号服务', { exact: false }).waitFor()
  await context.setOffline(false)
  await checked(() => button('刷新列表').click(), 'GET', listPath, 200)
  await target().waitFor()
  for (const width of [360, 1920]) {
    await page.setViewportSize({ width, height: 900 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  }

  await checked(() => page.getByRole('link', { name: '查看角色权限' }).click(), 'GET', /^\/api\/auth\/roles$/, 200)
  await page.getByRole('heading', { name: '角色权限', exact: true }).waitFor()
  await page.getByRole('row', { name: /系统管理员/ }).getByText('查看和管理平台账号', { exact: false }).waitFor()
  assert.equal(await page.locator('tbody tr').count(), 3)

  const analyst = await context.newPage()
  analyst.on('pageerror', error => errors.push(error.message))
  await analyst.goto(base + '/login')
  await signIn(analyst, 'analyst')
  await analyst.waitForURL('**/outpatient')
  assert.equal(await analyst.getByRole('link', { name: '用户管理' }).count(), 0)
  await analyst.goto(base + '/admin/users')
  await analyst.waitForURL('**/outpatient?denied=admin')
  assert.deepEqual(errors, [])
  console.log('ADMIN_BROWSER_OK create=1 update=3 roles=3 denied_routes=2 revoked=1 offline=1')
} finally {
  await browser.close()
}
