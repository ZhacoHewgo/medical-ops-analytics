<script setup>
import { useRouter } from 'vue-router'
import App from '../App.vue'
import { requestWithSession } from '../api/request.js'
import { useSession } from '../store/user.js'

defineOptions({ name: 'OutpatientView' })

const router = useRouter()
const session = useSession()

function loadData(path, options) {
  return requestWithSession(session, path, options, () => {
    void router.replace({ name: 'login', query: { expired: '1' } })
  })
}
</script>

<template>
  <App :load-response="loadData" />
</template>
