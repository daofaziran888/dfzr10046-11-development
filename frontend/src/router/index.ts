import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'
import AuditCenterView from '@/views/audit/AuditCenterView.vue'
import HandleOrderView from '@/views/audit/HandleOrderView.vue'
import ReviewOrderView from '@/views/audit/ReviewOrderView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/audit', name: 'audit-center', component: AuditCenterView,
      alias: ['/audit-dispatch', '/audit_dispatch', '/dispatch', '/todos', '/todo', '/work-todo'] },
    { path: '/audit/handle/:id', name: 'audit-handle', component: HandleOrderView, props: true },
    {
      path: '/audit/handle',
      redirect: (to) => {
        const id = to.query.id ?? to.query.dispatch_id ?? to.query.dispatchId ?? ''
        return id ? `/audit/handle/${id}` : '/audit'
      },
    },
    { path: '/audit/review/:id', name: 'audit-review', component: ReviewOrderView, props: true },
    {
      path: '/audit/review',
      redirect: (to) => {
        const id = to.query.id ?? to.query.dispatch_id ?? to.query.dispatchId ?? ''
        return id ? `/audit/review/${id}` : '/audit'
      },
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
