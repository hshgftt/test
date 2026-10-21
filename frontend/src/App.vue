<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <el-menu
        :default-active="route.path"
        mode="horizontal"
        router
        class="nav-menu"
        :ellipsis="false"
      >
        <el-menu-item index="/compare">📈 对比分析</el-menu-item>
        <el-menu-item index="/stats">📊 指标统计</el-menu-item>
        <el-menu-item index="/upload">📤 上传数据</el-menu-item>
      </el-menu>
    </header>

    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f0f2f5;
}
.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 24px;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  position: sticky;
  top: 0;
  z-index: 10;
}
.nav-menu { flex-shrink: 0; border-bottom: none; }
.app-main {
  flex: 1;
  padding: 16px;
  min-height: 0;
}
</style>
