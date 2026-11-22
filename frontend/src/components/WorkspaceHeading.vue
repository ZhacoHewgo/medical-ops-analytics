<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useSession } from '../store/user.js'
import WorkspaceIcon from './WorkspaceIcon.vue'

const router = useRouter()
const session = useSession()
const account = computed(() => session.current?.user)

function logout() {
  session.setSession(null)
  void router.replace({ name: 'login' })
}
</script>

<template>
  <section class="page-heading workspace-heading" :class="{ 'workspace-heading-with-tools': $slots.tools }">
    <div class="workspace-heading-title"><slot /></div>
    <div v-if="$slots.tools" class="workspace-heading-tools"><slot name="tools" /></div>
    <div v-if="account" class="account-actions">
      <span class="workspace-account" :title="account.username">
        <WorkspaceIcon name="user" /><span>{{ account.username }}</span>
      </span>
      <button type="button" class="button secondary" @click="logout">退出登录</button>
    </div>
    <RouterLink v-else class="button secondary workspace-login" :to="{ name: 'outpatient' }">打开认证看板</RouterLink>
  </section>
</template>
