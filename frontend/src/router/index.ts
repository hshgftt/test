import { createRouter, createWebHistory } from 'vue-router'
import CompareView from '@/views/CompareView.vue'
import StatsView from '@/views/StatsView.vue'
import UploadView from '@/views/UploadView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/compare' },
    {
      path: '/compare',
      name: 'compare',
      component: CompareView,
      meta: { title: '对比分析' },
    },
    {
      path: '/stats',
      name: 'stats',
      component: StatsView,
      meta: { title: '指标统计' },
    },
    {
      path: '/upload',
      name: 'upload',
      component: UploadView,
      meta: { title: '上传数据' },
    },
  ],
})

export default router
