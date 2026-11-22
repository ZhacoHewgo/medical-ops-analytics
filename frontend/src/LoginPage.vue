<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSession } from './api/session.mjs'
import illustration from './assets/login-illustration.webp'
import logo from './assets/login-logo.svg'

const session = useSession()
const route = useRoute()
const router = useRouter()
const demoLogin = import.meta.env.DEV && import.meta.env.VITE_COURSE_DEMO_LOGIN === '1'
  ? { username: 'admin', hint: '课程验收默认密码', password: '123456' }
  : null
const accountKey = 'mops.remembered-username'
let rememberedAccount = ''
try {
  rememberedAccount = (localStorage.getItem(accountKey) || '').slice(0, 64)
} catch {
  rememberedAccount = ''
}
const username = ref(rememberedAccount || demoLogin?.username || '')
const remember = ref(Boolean(rememberedAccount))
const password = ref('')
const showPassword = ref(false)
const busy = ref(false)
const error = ref('')

function saveAccount(account) {
  try {
    if (account) localStorage.setItem(accountKey, account)
    else localStorage.removeItem(accountKey)
  } catch {
    return
  }
}

async function submit() {
  if (busy.value) return
  const account = username.value.trim()
  busy.value = true
  error.value = ''
  try {
    error.value = await session.login(account, password.value)
    if (!error.value) {
      saveAccount(remember.value ? account : '')
      const redirect = route.query.redirect
      const target = typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//')
        && router.resolve(redirect).meta.protected ? redirect : { name: 'outpatient' }
      await router.replace(target)
    }
  } catch {
    error.value = '暂时无法连接登录服务，请稍后重试。'
  } finally {
    password.value = ''
    showPassword.value = false
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <div class="login-layout">
      <section class="login-hero" aria-labelledby="login-welcome">
        <div class="login-intro">
          <div class="login-brand">
            <img :src="logo" alt="" width="44" height="44" />
            <span>医疗运营分析平台</span>
          </div>
          <h1 id="login-welcome">欢迎使用<br />医疗运营分析平台</h1>
          <p>整合业务数据 · 洞察运营情况 · 支撑精细化管理与决策</p>
        </div>
        <img class="login-illustration" :src="illustration" alt="" width="1363" height="1154" />
      </section>
      <form class="login-card" aria-labelledby="login-title" :aria-busy="busy" @submit.prevent="submit">
        <h2 id="login-title">登录</h2>
        <p class="login-description">请输入您的账号密码</p>
        <p v-if="route.query.expired === '1'" class="login-status" role="status">登录已失效，请重新登录。</p>
        <div class="login-fields">
          <div class="login-input">
            <label class="login-label" for="username">用户名</label>
            <svg class="login-icon" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="7" r="4" /><path d="M4.5 21v-1a7.5 7.5 0 0 1 15 0v1" />
            </svg>
            <input id="username" v-model="username" autocomplete="username" placeholder="账号" maxlength="64" required :disabled="busy" />
          </div>
          <div class="login-input">
            <label class="login-label" for="password">密码</label>
            <svg class="login-icon" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2" />
            </svg>
            <input id="password" v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password"
              placeholder="密码" :aria-describedby="demoLogin ? 'demo-password' : undefined" required :disabled="busy" />
            <button class="login-visibility" type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              :disabled="busy" @click="showPassword = !showPassword">
              <svg class="login-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12Z" />
                <circle cx="12" cy="12" r="2.8" /><path v-if="!showPassword" d="m3.5 3.5 17 17" />
              </svg>
            </button>
          </div>
        </div>
        <label class="login-remember">
          <input v-model="remember" type="checkbox" :disabled="busy" @change="!remember && saveAccount('')" />
          记住账号
        </label>
        <p v-if="error" class="login-error" role="alert">{{ error }}</p>
        <button class="login-submit" type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
        <p v-if="demoLogin" id="demo-password" class="login-demo">
          <span>{{ demoLogin.hint }}：<strong>{{ demoLogin.password }}</strong></span>
        </p>
      </form>
    </div>
  </main>
</template>

<style scoped>
.login-page {
  position: relative;
  isolation: isolate;
  display: grid;
  place-items: center;
  min-height: 100svh;
  padding: clamp(20px, 5svh, 48px) clamp(20px, 8vw, 112px);
  overflow: hidden;
  color: #15203b;
  background:
    radial-gradient(ellipse at 82% 62%, #8fb3fc 0%, transparent 54%),
    radial-gradient(ellipse at 0% 88%, #e2eaff 0%, transparent 52%),
    linear-gradient(150deg, #c8d8fa, #b4c9f5 47%, #c8d9fb);
}
.login-page::before, .login-page::after {
  content: '';
  position: absolute;
  z-index: -1;
  pointer-events: none;
}
.login-page::before {
  inset: 0 0 auto auto;
  width: min(45vw, 690px);
  height: 55%;
  background: radial-gradient(circle, #fff9 28%, transparent 30%) 0 5px / clamp(22px, 2.62vw, 40px) clamp(23px, 2.48vw, 38px);
  mask-image: linear-gradient(black 82%, transparent);
}
.login-page::after {
  right: -18%;
  bottom: -32%;
  width: 75%;
  height: 58%;
  border-radius: 75% 0 0;
  transform: rotate(-17deg);
  background: #5690fa45;
  box-shadow: -54vw -25vh 0 #7196ee32;
}
.login-layout {
  width: min(100%, 1040px);
  min-height: min(590px, 65svh);
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr);
  gap: 18px;
  padding-right: clamp(20px, 2.2vw, 32px);
  overflow: hidden;
  border-radius: 30px;
  background:
    radial-gradient(ellipse at left bottom, #f2f4ff 12%, transparent 68%),
    linear-gradient(133deg, #f1f3ff 0%, #e4eafe 62%, #e9edff 100%);
  box-shadow: 0 24px 70px #2f5bba25;
}
.login-hero {
  min-width: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}
.login-intro { padding: clamp(24px, 2.4vw, 34px) 0 12px clamp(24px, 3.1vw, 44px); }
.login-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 760;
  font-size: clamp(17px, 1.48vw, 22px);
  line-height: 1.2;
}
.login-brand img {
  width: clamp(30px, 2.4vw, 36px);
  height: auto;
  flex: none;
}
.login-intro h1 {
  margin: 22px 0 12px;
  font-size: clamp(28px, 2.7vw, 40px);
  font-weight: 800;
  letter-spacing: -.045em;
  line-height: 1.22;
}
.login-intro p { color: #55617d; font-size: clamp(13px, 1.13vw, 16px); line-height: 1.55; }
.login-illustration {
  align-self: end;
  display: block;
  width: 100%;
  max-width: 633px;
  height: 100%;
  min-height: 0;
  object-fit: contain;
  object-position: left bottom;
  mask-image: linear-gradient(to bottom, transparent, black 1.5%), linear-gradient(to left, transparent, black 1.3%);
  mask-composite: intersect;
}
.login-card {
  align-self: center;
  justify-self: center;
  width: min(100%, 420px);
  min-width: 0;
  margin: 30px 0;
  padding: clamp(24px, 2.5vw, 36px);
  border-radius: 24px;
  background: #fff;
}
.login-card h2 {
  font-size: 32px;
  font-weight: 800;
  letter-spacing: -.055em;
  line-height: 1.2;
}
.login-description { margin-top: 6px; color: #56617c; font-size: 16px; }
.login-fields { display: grid; gap: 14px; margin-top: 26px; }
.login-input { position: relative; }
.login-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.login-input input {
  display: block;
  height: 48px;
  padding: 8px 48px 8px 44px;
  border: 1.5px solid #dce3f1;
  border-radius: 10px;
  color: #15203b;
  background: #fcfdff;
  font-size: 16px;
}
.login-input input::placeholder { color: #63718c; }
.login-input input:focus { border-color: #2f5bdb; outline: 2px solid #2f5bdb50; outline-offset: 0; }
.login-icon {
  width: 22px;
  height: auto;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.65;
  stroke-linecap: round;
  stroke-linejoin: round;
  color: #707d98;
}
.login-input > .login-icon { position: absolute; top: 50%; left: 14px; transform: translateY(-50%); pointer-events: none; }
.login-visibility {
  display: grid;
  place-items: center;
  position: absolute;
  inset: 2px 2px 2px auto;
  width: 44px;
  border: 0;
  border-radius: 8px;
  background: transparent;
}
.login-visibility:hover:not(:disabled) { background: #edf2ff; }
.login-remember {
  display: flex;
  align-items: center;
  gap: 10px;
  width: fit-content;
  min-height: 24px;
  margin-top: 18px;
  font-size: 14px;
  cursor: pointer;
}
.login-remember input {
  width: 18px;
  height: 18px;
  margin: 0;
  padding: 0;
  accent-color: #1554f8;
  cursor: pointer;
}
.login-status, .login-error { margin-top: 12px; font-size: .9rem; }
.login-status { color: #55617d; }
.login-error { color: #9a3028; }
.login-submit {
  width: 100%;
  min-height: 48px;
  margin-top: 26px;
  border: 0;
  border-radius: 10px;
  background: #1554f8;
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: .36em;
  padding-inline-start: .36em;
}
.login-submit:hover:not(:disabled) { background: #1646d2; }
.login-demo {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 24px;
  color: #5d6884;
  font-size: 14px;
  text-align: center;
}
.login-demo::before, .login-demo::after { content: ''; flex: 1; height: 1px; background: #dce3f1; }
.login-demo strong { color: #15203b; font-size: 1.12em; }

@media (min-width: 841px) and (max-height: 680px) {
  .login-card { padding-block: 22px; margin-block: 20px; }
  .login-fields, .login-submit, .login-demo { margin-top: 18px; }
}
@media (max-width: 840px) {
  .login-page { padding-inline: 16px; }
  .login-layout { max-width: 440px; min-height: auto; grid-template-columns: minmax(0, 1fr); gap: 18px; padding: 20px 14px 14px; border-radius: 24px; }
  .login-hero { display: block; }
  .login-intro { padding: 0 6px; }
  .login-intro h1 { margin-bottom: 0; }
  .login-intro p, .login-illustration { display: none; }
  .login-card { margin: 0; padding: clamp(18px, 5vw, 32px); }
}
</style>
