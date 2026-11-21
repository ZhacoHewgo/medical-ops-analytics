import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { RouterView } from 'vue-router'
import { createAppRouter } from './router/index.js'
import { useSession } from './store/user.js'
import './style.css'

const pinia = createPinia()
const router = createAppRouter(useSession(pinia))

createApp({
  render: () => h(RouterView, null, {
    default: ({ Component, route }) => Component ? h(Component, { key: route.path }) : null,
  }),
})
  .use(pinia)
  .use(router)
  .mount('#app')
