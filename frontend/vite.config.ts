import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

/**
 * 后端端口。允许用环境变量覆盖，是为了让 e2e 能起**自己的**后端：
 * 默认 8000 经常被本地正在跑的服务占着，那时 e2e 起后端会失败（Errno 10048），
 * 但健康探测照样 200（打到了那个服务上），于是测出来的数字全是假的。
 * 踩过一次，别改回去。
 */
const BE_PORT = process.env.BE_PORT || '8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: `http://localhost:${BE_PORT}`,
        changeOrigin: true,
      },
    },
  },
})
