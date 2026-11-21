<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSession } from './api/session.mjs'

const session = useSession()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    error.value = await session.login(username.value.trim(), password.value)
    if (!error.value) await router.replace({ name: 'outpatient' })
  } catch {
    error.value = '暂时无法连接登录服务，请稍后重试。'
  } finally {
    password.value = ''
    busy.value = false
  }
}
</script>

<template>
  <main class="workspace">
    <form class="panel login-panel" @submit.prevent="submit">
      <h1>登录医疗运营工作台</h1>
      <p v-if="route.query.expired === '1'" role="status">登录已失效，请重新登录。</p>
      <div class="field">
        <label for="username">用户名</label>
        <input id="username" v-model="username" autocomplete="username" maxlength="64" required />
      </div>
      <div class="field">
        <label for="password">密码</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" required />
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <button class="button primary" type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
      <RouterLink to="/">返回课程样例演示</RouterLink>
    </form>
  </main>
</template>
