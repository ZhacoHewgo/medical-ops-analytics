<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useSession } from '../store/user.js'
import { chartMotionStyle } from '../styles/chartMotion.mjs'
import logo from '../assets/login-logo.svg'
import WorkspaceIcon from './WorkspaceIcon.vue'

const route = useRoute()
const session = useSession()
const account = computed(() => session.current?.user)
const analysisLinks = [
  ['overview', '管理驾驶舱', 'home'],
  ['outpatient', '门诊分析', 'people'],
  ['inpatient', '住院分析', 'bed'],
  ['revenue', '收入分析', 'money'],
  ['disease', '病种分析', 'pulse'],
  ['resource', '资源效率', 'resource'],
]
const reportLinks = [
  ['multidimensional-report', '多维报表', 'grid'],
  ['metrics', '指标查询', 'chart'],
  ['ask', '智能问数', 'question'],
  ['report-generate', '报告生成', 'file'],
  ['reports', '门诊报表', 'file'],
]

</script>

<template>
  <div class="workspace-frame" :style="chartMotionStyle">
    <a v-if="route.name === 'outpatient'" class="skip-link" href="#main">跳到门诊数据</a>
    <aside class="workspace-sidebar">
      <RouterLink class="workspace-brand" :to="{ name: 'overview' }">
        <img :src="logo" alt="" width="36" height="36" />
        <strong>医疗运营分析平台</strong>
      </RouterLink>
      <nav class="workspace-nav" aria-label="工作台导航">
        <div class="workspace-nav-group">
          <p>运营分析</p>
          <RouterLink v-for="[name, label, icon] in analysisLinks" :key="name" :to="{ name }">
            <WorkspaceIcon :name="icon" />{{ label }}
          </RouterLink>
        </div>
        <div class="workspace-nav-group">
          <p>查询与报告</p>
          <RouterLink v-for="[name, label, icon] in reportLinks" :key="name" :to="{ name }">
            <WorkspaceIcon :name="icon" />{{ label }}
          </RouterLink>
        </div>
        <div v-if="account && account.role !== 'department'" class="workspace-nav-group">
          <p>管理</p>
          <RouterLink v-if="account?.role === 'admin'" :to="{ name: 'admin-users' }"
            :class="{ 'workspace-nav-current': route.name === 'admin-roles' }"><WorkspaceIcon name="user" />用户管理</RouterLink>
          <RouterLink :to="{ name: 'ops-sources' }"
            :class="{ 'workspace-nav-current': route.name === 'ops-tasks' }"><WorkspaceIcon name="storage" />数据源与任务</RouterLink>
          <RouterLink :to="{ name: 'ops-quality' }"><WorkspaceIcon name="file" />数据质量</RouterLink>
        </div>
      </nav>
    </aside>
    <div class="workspace-content">
      <slot />
    </div>
  </div>
</template>
