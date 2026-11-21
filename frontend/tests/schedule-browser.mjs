// Exercises the schedule settings with the isolated MySQL application.
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const { chromium } = await import(pathToFileURL(process.env.MOPS_PLAYWRIGHT_CORE).href)
const [base] = process.argv.slice(2)
assert.equal(new URL(base).hostname, '127.0.0.1')
const usernames = { admin: process.env.MOPS_BROWSER_ADMIN, analyst: process.env.MOPS_BROWSER_ANALYST }
const password = process.env.MOPS_BROWSER_PASSWORD
assert.ok(usernames.admin && usernames.analyst && password)
const browser = await chromium.launch({
  executablePath: process.env.MOPS_CHROMIUM, headless: true, args: ['--disable-gpu', '--no-proxy-server'],
})

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = []
  let writes = 0
  const path = '/api/ops/tasks/schedule'
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() === 'PUT' && new URL(request.url()).pathname === path) writes++
  })
  const section = tab => tab.getByRole('region', { name: '每日采集配置' })
  const button = label => section(page).getByRole('button', { name: label, exact: true })
  async function openSchedule(tab, role) {
    await tab.goto(base + '/login')
    await tab.getByLabel('用户名').fill(usernames[role])
    await tab.getByLabel('密码', { exact: true }).fill(password)
    await tab.getByRole('button', { name: '登录', exact: true }).click()
    await tab.waitForURL('**/outpatient')
    await tab.getByRole('link', { name: '数据源与任务', exact: true }).click()
    await tab.getByRole('link', { name: '任务管理', exact: true }).click()
    await section(tab).getByText('当前配置', { exact: false }).waitFor()
  }

  async function submit(status) {
    const result = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === path)
    await button('确认更新配置').click()
    const response = await result
    assert.equal(response.status(), status)
    return response.json()
  }

  await openSchedule(page, 'admin')
  const editor = section(page)
  const mode = editor.getByLabel('每日采集', { exact: true })
  const time = editor.getByLabel('每日执行时间', { exact: true })
  await mode.selectOption({ label: '停用' })
  await button('继续更新配置').click()
  await button('取消更新').click()
  assert.equal(writes, 0)
  await button('继续更新配置').click()
  assert.equal((await submit(200)).data.enabled, false)
  await editor.getByText('当前配置 停用', { exact: false }).waitFor()

  await page.route('**/api/ops/tasks/schedule', route => {
    const payload = route.request().postDataJSON()
    payload.expected_version = 0
    return route.continue({ postData: JSON.stringify(payload) })
  }, { times: 1 })
  await mode.selectOption({ label: '启用' })
  await time.fill('04:45')
  await button('继续更新配置').click()
  await submit(409)
  await editor.getByRole('alert').getByText('操作发生冲突，请刷新状态后再试。').waitFor()
  await button('刷新采集配置').click()
  await mode.selectOption({ label: '启用' })
  await time.fill('04:45')
  await button('继续更新配置').click()
  const configured = (await submit(200)).data
  assert.equal(configured.enabled, true)
  assert.equal(configured.hour, 4)
  assert.equal(configured.minute, 45)
  await editor.getByText('当前配置 启用 · 每日 04:45（北京时间）').waitFor()

  const analyst = await browser.newPage()
  await openSchedule(analyst, 'analyst')
  await section(analyst).getByText('当前配置 启用 · 每日 04:45（北京时间）').waitFor()
  assert.equal(await section(analyst).getByLabel('每日执行时间').count(), 0)
  assert.equal(writes, 3)
  assert.deepEqual(errors, [])
  console.log('SCHEDULE_BROWSER_OK pause=1 resume=1 conflict=409 canceled=1 analyst_read_only=1')
} finally {
  await browser.close()
}
