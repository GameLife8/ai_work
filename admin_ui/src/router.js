import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './store/auth'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', component: () => import('./views/Dashboard.vue') },
      { path: 'connections', component: () => import('./views/Connections.vue') },
      { path: 'models', component: () => import('./views/Models.vue') },
      { path: 'skills', component: () => import('./views/Skills.vue') },
      { path: 'http-skills', component: () => import('./views/HttpSkills.vue'), meta: { adminOnly: true } },
      { path: 'runbooks', component: () => import('./views/Runbooks.vue'), meta: { adminOnly: true } },
      { path: 'runbook-runs', component: () => import('./views/RunbookRuns.vue'), meta: { adminOnly: true } },
      { path: 'prompts', component: () => import('./views/Prompts.vue'), meta: { adminOnly: true } },
      { path: 'pending', component: () => import('./views/PendingActions.vue') },
      { path: 'async-tasks', component: () => import('./views/AsyncTasks.vue') },
      { path: 'maintenance', component: () => import('./views/Maintenance.vue'), meta: { adminOnly: true } },
      { path: 'users', component: () => import('./views/Users.vue'), meta: { adminOnly: true } },
      { path: 'audit', component: () => import('./views/Audit.vue'), meta: { adminOnly: true } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.path !== '/login' && !auth.isLogged) return '/login'
  if (to.meta?.adminOnly && !auth.isAdmin) return '/dashboard'
  return true
})

export default router
