<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getTree } from '@/api'
import type { TreeResponse } from '@/types'

const emit = defineEmits<{ (e: 'change', ids: number[]): void }>()

interface TreeNode {
  key: string
  label: string
  type: 'platform' | 'brand' | 'vehicle'
  id?: number // only present on vehicle nodes
  children?: TreeNode[]
}

const rawTree = ref<TreeResponse>({})
const loading = ref(false)
const filterText = ref('')

const treeRef = ref<any>(null)

/** build el-tree data from { platform: { brand: [{id,name}] } } */
const treeData = computed<TreeNode[]>(() => {
  const out: TreeNode[] = []
  for (const [platform, brands] of Object.entries(rawTree.value)) {
    const brandNodes: TreeNode[] = []
    for (const [brand, vehicles] of Object.entries(brands)) {
      brandNodes.push({
        key: `b-${brand}`,
        label: brand,
        type: 'brand',
        children: vehicles.map((v) => ({
          key: `v-${v.id}`,
          label: v.name,
          type: 'vehicle',
          id: v.id,
        })),
      })
    }
    out.push({
      key: `p-${platform}`,
      label: platform,
      type: 'platform',
      children: brandNodes,
    })
  }
  return out
})

/** flat list of every vehicle id (used by 全选) */
const allVehicleKeys = computed(() => {
  const keys: string[] = []
  const walk = (nodes: TreeNode[]) => {
    for (const n of nodes) {
      if (n.type === 'vehicle') keys.push(n.key)
      else if (n.children) walk(n.children)
    }
  }
  walk(treeData.value)
  return keys
})

const checkedCount = ref(0)

function emitChecked() {
  const tree = treeRef.value
  if (!tree) return
  // leafOnly = true → only vehicle nodes
  const nodes: TreeNode[] = tree.getCheckedNodes(true, false)
  const ids = nodes.filter((n) => n.type === 'vehicle' && n.id != null).map((n) => n.id!)
  checkedCount.value = ids.length
  emit('change', ids)
}

function selectAll() {
  treeRef.value?.setCheckedKeys(allVehicleKeys.value, false)
  emitChecked()
}

function clearAll() {
  treeRef.value?.setCheckedKeys([], false)
  emitChecked()
}

/** select every vehicle of one platform */
function selectPlatform(platform: string) {
  const node = treeData.value.find((p) => p.label === platform)
  if (!node?.children) return
  const keys: string[] = []
  for (const b of node.children) {
    for (const v of b.children ?? []) keys.push(v.key)
  }
  treeRef.value?.setCheckedKeys(keys, false)
  emitChecked()
}

function filterNode(value: string, data: TreeNode) {
  if (!value) return true
  return data.label.toLowerCase().includes(value.toLowerCase())
}

watch(filterText, (val) => {
  treeRef.value?.filter(val)
})

onMounted(async () => {
  loading.value = true
  try {
    rawTree.value = await getTree()
  } finally {
    loading.value = false
  }
})

defineExpose({ selectAll, clearAll })
</script>

<template>
  <div class="tree-panel" v-loading="loading">
    <div class="tree-toolbar">
      <el-input
        v-model="filterText"
        placeholder="搜索平台 / 品牌 / 车型"
        clearable
        size="small"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <div class="btn-row">
        <el-button size="small" type="primary" @click="selectAll">全选</el-button>
        <el-button size="small" @click="clearAll">清空</el-button>
      </div>
      <div class="checked-info">
        已选 <b>{{ checkedCount }}</b> 台车
      </div>
    </div>

    <el-scrollbar class="tree-scroll">
      <el-tree
        ref="treeRef"
        :data="treeData"
        node-key="key"
        show-checkbox
        default-expand-all
        :filter-node-method="filterNode"
        @check="emitChecked"
        class="vehicle-tree"
      >
        <template #default="{ node, data }">
          <span class="node-label">
            <span v-if="data.type === 'platform'" class="tag platform">平台</span>
            <span v-else-if="data.type === 'brand'" class="tag brand">品牌</span>
            <span v-else class="tag vehicle">车型</span>
            <span>{{ node.label }}</span>
            <el-button
              v-if="data.type === 'platform'"
              link
              size="small"
              type="primary"
              class="pick-btn"
              @click.stop="selectPlatform(data.label)"
            >
              选本平台
            </el-button>
          </span>
        </template>
      </el-tree>
    </el-scrollbar>
  </div>
</template>

<style scoped>
.tree-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  overflow: hidden;
}
.tree-toolbar {
  padding: 10px;
  border-bottom: 1px solid #eee;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.btn-row {
  display: flex;
  gap: 6px;
}
.checked-info {
  font-size: 12px;
  color: #666;
}
.tree-scroll {
  flex: 1;
  min-height: 0;
  padding: 6px 4px;
}
.vehicle-tree :deep(.el-tree-node__content) {
  height: 30px;
}
.node-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
}
.tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  color: #fff;
  flex-shrink: 0;
}
.tag.platform { background: #667eea; }
.tag.brand { background: #f5576c; }
.tag.vehicle { background: #00c2a8; }
.pick-btn {
  margin-left: 4px;
  font-size: 11px;
}
</style>
