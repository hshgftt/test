<script setup lang="ts">
/**
 * 通用导航树。
 *
 * ── 为什么是「通用」──
 *
 * 层级数量、顺序、来源全部来自后端 `tree_profiles.levels`，前端**不做任何**
 * 「平台 → 品牌 → 车」的假设。要给树加一层「能源形式」，改配置即可：
 *
 *     { source: 'attr', key: 'energy_type' }
 *
 * 组件只认节点上的 `kind` 与 `children`，因此哪怕以后出现新的 kind，
 * 只要后端按同样的形状返回，这里照常渲染。
 *
 * 叶子是 `kind='segment'`（车 × 试验模式），勾选结果直接就是对比/取数接口
 * 需要的分析单元，不再经过「车 id 列表」这一层中间态。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getMeta, getTree } from '@/api'
import type { Meta, NavNode, NavTree, SegmentRef, TreeLevel } from '@/types'

const props = defineProps<{
  /** 初始选中的分析单元（按 vehicle_id:mode_id 匹配），用于树重建后恢复 */
  selected?: SegmentRef[]
}>()

const emit = defineEmits<{ (e: 'change', segs: SegmentRef[]): void }>()

const treeRef = ref<any>(null)
const loading = ref(false)
const filterText = ref('')

const meta = ref<Meta | null>(null)
const nav = ref<NavTree | null>(null)
const profileCode = ref<string>('')

/** 每个节点的原始对象，勾选后取 segment 用（tree 的 data 就是 NavNode） */
const nodes = computed<NavNode[]>(() => nav.value?.nodes ?? [])

/** 层级配置：用来给每层生成标签文字（平台 / 品牌 / 车型 …） */
const levels = computed<TreeLevel[]>(() => nav.value?.levels ?? [])

function levelTitle(node: NavNode): string {
  if (node.kind === 'segment') return '单元'
  if (node.kind === 'test_mode') {
    const lv = levels.value.find((l) => l.source === 'test_mode')
    return lv?.label || '模式'
  }
  if (node.kind === 'vehicle') return '车型'
  const lv = levels.value[node.level]
  if (lv?.label) return lv.label
  if (node.code) {
    const def = meta.value?.attrs.find((a) => a.code === node.code)
    if (def) return def.name
  }
  return '字段'
}

const KIND_COLOR: Record<string, string> = {
  attr: '#667eea',
  vehicle: '#00c2a8',
  test_mode: '#f59e0b',
  segment: '#909399',
}

function tagColor(node: NavNode): string {
  return KIND_COLOR[node.kind] ?? '#909399'
}

/** 叶子节点的展示文字：segment 直接写模式（未分模式也有值） */
function nodeLabel(node: NavNode): string {
  if (node.kind === 'segment') return node.mode || node.vehicle_name || node.label
  return node.label
}

/** el-tree 的 node-key 用后端生成的 key，全局唯一且跨层级稳定 */
const treeData = computed(() => nodes.value)

function filterNode(value: string, data: NavNode): boolean {
  if (!value) return true
  const kw = value.toLowerCase()
  if (nodeLabel(data).toLowerCase().includes(kw)) return true
  // 车名 / 模式名在所有层级都参与匹配，方便「搜一台车直接看到它所有模式」
  if ((data.vehicle_name || '').toLowerCase().includes(kw)) return true
  if ((data.mode || '').toLowerCase().includes(kw)) return true
  return false
}

watch(filterText, (v) => treeRef.value?.filter(v))

// ── 勾选 ───────────────────────────────────────────────────────────────

/** 全部 segment 叶子 key，用于「全选」 */
const allLeafKeys = computed<string[]>(() => {
  const out: string[] = []
  const walk = (list: NavNode[]) => {
    for (const n of list) {
      if (n.kind === 'segment') out.push(n.key)
      if (n.children?.length) walk(n.children)
    }
  }
  walk(nodes.value)
  return out
})

function leavesOf(node: NavNode): string[] {
  const out: string[] = []
  const walk = (n: NavNode) => {
    if (n.kind === 'segment') out.push(n.key)
    ;(n.children ?? []).forEach(walk)
  }
  walk(node)
  return out
}

/** 收集当前勾选的 segment 叶子 → 去重后的分析单元 */
function collectChecked(): SegmentRef[] {
  const tree = treeRef.value
  if (!tree) return []
  const picked: NavNode[] = tree.getCheckedNodes(true, false)
  const out: SegmentRef[] = []
  const seen = new Set<string>()
  for (const n of picked) {
    if (n.kind !== 'segment' || !n.segment) continue
    const k = `${n.segment.vehicle_id}:${n.segment.mode_id ?? ''}`
    if (seen.has(k)) continue
    seen.add(k)
    out.push(n.segment)
  }
  return out
}

const checkedSegments = ref<SegmentRef[]>([])

function emitChecked() {
  checkedSegments.value = collectChecked()
  emit('change', checkedSegments.value)
}

const checkedVehicleCount = computed(
  () => new Set(checkedSegments.value.map((s) => s.vehicle_id)).size,
)

function selectAll() {
  treeRef.value?.setCheckedKeys(allLeafKeys.value, false)
  emitChecked()
}

function clearAll() {
  treeRef.value?.setCheckedKeys([], false)
  emitChecked()
}

/** 只选某个子树（父节点后面的「选本层」按钮） */
function selectSubtree(node: NavNode) {
  const keys = leavesOf(node)
  if (!keys.length) return
  treeRef.value?.setCheckedKeys(keys, false)
  emitChecked()
}

/** 按给定的分析单元恢复勾选（对比页刷新 / 切换树后使用） */
function restoreSelection(segs: SegmentRef[] = props.selected ?? []) {
  const want = new Set(segs.map((s) => `${s.vehicle_id}:${s.mode_id ?? ''}`))
  if (!want.size) {
    treeRef.value?.setCheckedKeys([], false)
    emitChecked()
    return
  }
  const keys: string[] = []
  const walk = (list: NavNode[]) => {
    for (const n of list) {
      if (
        n.kind === 'segment' &&
        n.segment &&
        want.has(`${n.segment.vehicle_id}:${n.segment.mode_id ?? ''}`)
      ) {
        keys.push(n.key)
      }
      if (n.children?.length) walk(n.children)
    }
  }
  walk(nodes.value)
  treeRef.value?.setCheckedKeys(keys, false)
  emitChecked()
}

// ── 加载 ───────────────────────────────────────────────────────────────

async function load(profile?: string) {
  loading.value = true
  // 切换前记下当前选中的分析单元。segment 是「车 × 模式」，与层级无关，
  // 换一套导航方式后这些单元依然存在 —— 所以要跟着走，不能丢。
  const keep = checkedSegments.value.slice()
  try {
    const [m, t] = await Promise.all([getMeta(), getTree(profile)])
    meta.value = m
    nav.value = t
    applyingProfile = true
    profileCode.value = t.profile?.code ?? ''
    applyingProfile = false

    // 等 el-tree 用新数据重建完，再动勾选状态。
    //
    // 必须显式 setCheckedKeys([])：segment 的 node-key 是
    // `seg|<车id>|<模式id>`，**不含层级索引**，换 profile 后这些 key
    // 照样命中旧勾选。以前只把 checkedSegments 置空、不碰 el-tree，
    // 结果就是「复选框全勾着，但实际一个都没选中」。
    await nextTick()
    treeRef.value?.setCheckedKeys([], false)
    checkedSegments.value = []

    // 外部指定的选中优先（对比页带过来的），否则沿用切换前的选择
    const wanted = props.selected?.length ? props.selected : keep
    if (wanted.length) {
      restoreSelection(wanted)
    } else {
      emit('change', [])
    }
  } catch (e: any) {
    ElMessage.error(e.message || '导航树加载失败')
  } finally {
    loading.value = false
  }
}

/** 正在把「接口返回的 profile」写回 v-model，此时不应再发一次请求 */
let applyingProfile = false

watch(profileCode, (code, old) => {
  if (applyingProfile) return
  if (code && code !== old) load(code)
})

onMounted(() => load())

defineExpose({ reload: load, selectAll, clearAll })
</script>

<template>
  <div class="tree-panel">
    <div class="tree-toolbar">
      <el-select
        v-if="(meta?.tree_profiles?.length ?? 0) > 1"
        v-model="profileCode"
        size="small"
        class="profile-select"
        placeholder="导航方式"
      >
        <el-option
          v-for="p in meta?.tree_profiles ?? []"
          :key="p.code"
          :label="p.name"
          :value="p.code"
        />
      </el-select>

      <el-input
        v-model="filterText"
        placeholder="搜索车型 / 品牌 / 模式"
        clearable
        size="small"
      />

      <div class="btn-row">
        <el-button size="small" type="primary" @click="selectAll">全选</el-button>
        <el-button size="small" @click="clearAll">清空</el-button>
      </div>

      <div class="checked-info">
        已选 <b>{{ checkedSegments.length }}</b> 个分析单元 ·
        <b>{{ checkedVehicleCount }}</b> 台车
        <span v-if="nav?.profile" class="profile-name">{{ nav.profile.name }}</span>
      </div>
    </div>

    <el-scrollbar v-loading="loading" class="tree-scroll">
      <el-tree
        ref="treeRef"
        :data="treeData"
        node-key="key"
        show-checkbox
        default-expand-all
        :expand-on-click-node="false"
        :filter-node-method="filterNode"
        @check="emitChecked"
        class="nav-tree"
      >
        <template #default="{ data }">
          <span class="node-label">
            <span
              class="tag"
              :style="{ background: tagColor(data) }"
            >{{ levelTitle(data) }}</span>
            <span :class="{ 'seg-label': data.kind === 'segment' }">
              {{ nodeLabel(data) }}
            </span>
            <span v-if="data.kind !== 'segment'" class="count">{{ data.count }}</span>
            <el-button
              v-if="data.kind !== 'segment' && data.children?.length"
              link
              size="small"
              type="primary"
              class="pick-btn"
              @click.stop="selectSubtree(data)"
            >
              选本级
            </el-button>
          </span>
        </template>
      </el-tree>

      <el-empty
        v-if="!loading && !nodes.length"
        description="还没有数据，先去「上传数据」导入"
        :image-size="70"
      />
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
.profile-select { width: 100%; }
.btn-row { display: flex; gap: 6px; }
.checked-info {
  font-size: 12px;
  color: #666;
  display: flex;
  align-items: center;
  gap: 4px;
}
.profile-name {
  margin-left: auto;
  color: #aaa;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 55%;
}
.tree-scroll {
  flex: 1;
  min-height: 0;
  padding: 6px 4px;
}
.nav-tree :deep(.el-tree-node__content) { height: 30px; }
.node-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  min-width: 0;
}
.seg-label { color: #444; }
.tag {
  font-size: 10px;
  line-height: 1.6;
  padding: 0 5px;
  border-radius: 3px;
  color: #fff;
  flex-shrink: 0;
}
.count {
  font-size: 11px;
  color: #aaa;
}
.pick-btn {
  margin-left: 4px;
  font-size: 11px;
  visibility: hidden;
}
.nav-tree :deep(.el-tree-node__content:hover) .pick-btn { visibility: visible; }
</style>
