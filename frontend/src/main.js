import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import App from './App.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/', component: App }],
})

createApp({ render: () => h(RouterView) })
  .use(createPinia())
  .use(router)
  .mount('#app')
