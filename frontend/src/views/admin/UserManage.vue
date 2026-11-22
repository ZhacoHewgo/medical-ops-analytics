<script setup>
import WorkspaceHeading from '../../components/WorkspaceHeading.vue'
import { computed, nextTick, onMounted, reactive, ref, shallowRef } from 'vue'
import { roleLabels, useManagementRequest } from '../../api/admin.mjs'
import { outpatientDepartmentName } from '../../api/departmentLabels.mjs'

const request = useManagementRequest()
const emptyForm = { username: '', password: '', role: 'analyst', department: '', enabled: true }
const form = reactive({ ...emptyForm })
const users = shallowRef([])
const pages = shallowRef([0])
const next = ref(null)
const selected = shallowRef(null)
const editorHeading = ref(null)
const loading = ref(false)
const saving = ref(false)
const confirming = ref(false)
const loadError = ref('')
const formError = ref('')
const notice = ref('')
const heading = computed(() => selected.value ? '调整账号' : '新增账号')
const targetLabel = computed(() => selected.value?.username || form.username.trim())
const scopeName = code => code ? outpatientDepartmentName(code) : '全院'

function reset() {
  selected.value = null
  Object.assign(form, emptyForm)
  confirming.value = false
  formError.value = ''
}

async function load(cursors = pages.value) {
  if (loading.value || saving.value) return
  loading.value = true
  loadError.value = ''
  users.value = []
  next.value = null
  pages.value = cursors
  try {
    const data = await request(`users?after=${cursors.at(-1)}`)
    if (!Array.isArray(data.items) || (data.next_after !== null && !Number.isSafeInteger(data.next_after))) {
      throw new Error('账号列表响应无效。')
    }
    users.value = data.items
    next.value = data.next_after
  } catch (error) {
    loadError.value = error.message
  } finally {
    loading.value = false
  }
}

function refresh(cursors) {
  reset()
  notice.value = ''
  return load(cursors)
}

async function edit(user) {
  reset()
  notice.value = ''
  selected.value = user
  Object.assign(form, user, { department: user.department || '' })
  await nextTick()
  editorHeading.value?.focus({ preventScroll: true })
  editorHeading.value?.scrollIntoView({ block: 'nearest' })
}

function prepare() {
  if (loading.value || saving.value || confirming.value) return
  notice.value = ''
  formError.value = ''
  const department = form.department.trim()
  if (form.role === 'department' && !department) {
    formError.value = '请填写科室代码。'
    return
  }
  const length = new TextEncoder().encode(form.password).length
  if (!selected.value && (length < 12 || length > 72)) {
    formError.value = '密码需要 12–72 个 UTF-8 字节。'
    return
  }
  confirming.value = true
}

async function save() {
  if (loading.value || saving.value || !confirming.value) return
  saving.value = true
  formError.value = ''
  const editing = selected.value
  const scope = { role: form.role, department: form.role === 'department' ? form.department.trim() : null }
  let saved = false
  try {
    if (editing) {
      await request(`users/${editing.user_id}`, {
        method: 'PATCH', data: { ...scope, enabled: form.enabled, expected_revision: editing.revision },
      })
    } else {
      await request('users', {
        method: 'POST', data: { ...scope, username: form.username.trim(), password: form.password },
      })
    }
    saved = true
  } catch (error) {
    formError.value = error.message
  } finally {
    form.password = ''
    saving.value = confirming.value = false
  }
  if (saved) {
    notice.value = editing ? '账号设置已保存。' : '账号已创建。'
    reset()
    await load()
  }
}

onMounted(() => load())
</script>

<template>
  <main class="workspace section-page">
    <WorkspaceHeading>
      <h1>用户管理</h1>
      <template #tools>
        <RouterLink class="button secondary section-link" :to="{ name: 'admin-roles' }">查看角色权限</RouterLink>
      </template>
    </WorkspaceHeading>
    <div class="admin-layout account-layout">
      <section class="panel filter-panel account-list" aria-labelledby="account-list">
        <div class="section-heading account-list-heading">
          <h2 id="account-list">平台账号 <small v-if="!loading && !loadError">本页 {{ users.length }} 个</small></h2>
          <button class="button secondary" type="button" :disabled="loading || saving" @click="refresh(pages)">刷新列表</button>
        </div>
        <p v-if="loading" role="status" aria-busy="true">正在读取账号…</p>
        <p v-else-if="loadError" class="report-error" role="alert">{{ loadError }}</p>
        <p v-else-if="users.length === 0">本页暂无账号。</p>
        <div v-else class="table-scroll account-table" tabindex="0" aria-label="账号列表，可滚动查看">
          <table>
            <thead><tr><th scope="col">用户名</th><th scope="col">角色</th><th scope="col">科室</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
            <tbody>
              <tr v-for="user in users" :key="user.user_id" :class="{ 'account-selected': selected?.user_id === user.user_id }">
                <th scope="row">{{ user.username }}</th><td>{{ roleLabels[user.role] }}</td><td>{{ scopeName(user.department) }}</td>
                <td><span class="status-chip" :class="{ 'status-chip-attention': !user.enabled }">{{ user.enabled ? '启用' : '停用' }}</span></td>
                <td><button v-if="user.role !== 'admin'" class="button secondary" type="button"
                  :disabled="saving || confirming" @click="edit(user)">调整</button><span v-else class="account-protected">受保护</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="pages.length > 1 || next !== null" class="report-pagination">
          <button class="button secondary" type="button" :disabled="pages.length < 2 || loading || saving" @click="refresh(pages.slice(0, -1))">上一页</button>
          <button class="button secondary" type="button" :disabled="next === null || loading || saving" @click="refresh([...pages, next])">下一页</button>
        </div>
      </section>
      <section class="panel filter-panel account-editor" aria-labelledby="account-form">
        <div class="section-heading"><h2 id="account-form" ref="editorHeading" tabindex="-1">{{ heading }}</h2></div>
        <p v-if="notice" class="account-notice" role="status">{{ notice }}</p>
        <form class="admin-form" @submit.prevent="prepare">
          <fieldset :disabled="loading || saving || confirming">
            <div v-if="selected" class="account-target"><span>用户名</span><strong>{{ selected.username }}</strong></div>
            <template v-else>
              <div class="field"><label for="new-user">用户名</label><input id="new-user" v-model="form.username" autocomplete="off" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}" required></div>
              <div class="field"><label for="new-password">初始密码</label><input id="new-password" v-model="form.password" type="password" autocomplete="new-password" placeholder="12–72 个 UTF-8 字节" required></div>
            </template>
            <div class="field"><label for="account-role">角色</label>
              <select id="account-role" v-model="form.role"><option value="analyst">{{ roleLabels.analyst }}</option><option value="department">{{ roleLabels.department }}</option></select>
            </div>
            <div v-if="form.role === 'department'" class="field">
              <label for="account-dept">科室代码</label><input id="account-dept" v-model="form.department" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}" required>
            </div>
            <div v-if="selected" class="field"><label for="account-enabled">状态</label>
              <select id="account-enabled" v-model="form.enabled"><option :value="true">启用</option><option :value="false">停用</option></select>
            </div>
          </fieldset>
          <p v-if="formError" class="report-error" role="alert">{{ formError }}</p>
          <div v-if="confirming" class="admin-form account-confirm" :class="{ 'account-disable': !form.enabled }">
            <p role="status">确认{{ selected ? '更新' : '创建' }} {{ targetLabel }} 为{{ roleLabels[form.role] }}，范围 {{ form.role === 'department' ? scopeName(form.department) : '全院' }}，状态{{ form.enabled ? '启用' : '停用' }}。</p>
            <div class="filter-actions">
              <button class="button primary" type="button" :disabled="saving" @click="save">确认{{ selected ? '更新' : '创建' }}</button>
              <button class="button secondary" type="button" :disabled="saving" @click="confirming = false">取消确认</button>
            </div>
          </div>
          <div v-else class="filter-actions">
            <button class="button primary" type="submit" :disabled="loading">继续</button>
            <button v-if="selected" class="button secondary" type="button" @click="reset">取消调整</button>
          </div>
        </form>
      </section>
    </div>
  </main>
</template>

<style scoped>
.account-layout { grid-template-columns: minmax(0, 1fr) minmax(260px, 350px); }
.account-list-heading { flex-direction: row; justify-content: space-between; }
.account-list-heading h2 small { margin-left: 6px; color: var(--muted); font-size: .75rem; font-weight: 500; }
.account-table { margin-top: 0; max-height: 520px; }
.account-table :is(th, td) { white-space: nowrap; }
.account-table tbody tr.account-selected { background: var(--surface-tint); }
.account-table .account-protected { color: var(--muted); font-size: .78rem; }
.account-editor { position: sticky; top: 14px; }
.account-editor h2 { scroll-margin: 18px; }
.account-editor .admin-form { gap: 12px; }
.account-target, .account-confirm, .account-notice { padding: 10px 12px; border-radius: 9px; background: var(--surface-tint); overflow-wrap: anywhere; }
.account-target { display: grid; gap: 2px; }
.account-target span { color: var(--muted); font-size: .75rem; }
.account-confirm p, .account-notice { font-size: .82rem; }
.account-notice { margin-bottom: 12px; }
.account-disable { background: color-mix(in srgb, var(--warning-orange) 25%, white); }
@media (max-width: 1000px) {
  .account-layout { grid-template-columns: minmax(0, 1fr); }
  .account-editor { position: static; }
}
</style>
