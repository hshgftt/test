<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getAllIndicatorsFlat,
  getLastTiming,
  getMeta,
  getVehicles,
  type Timing,
} from '@/api'
import type {
  AttrDef,
  Condition,
  DatasetType,
  IndicatorFlat,
  Meta,
  StatsPage,
  StatsRow,
} from '@/types'

/**
 * 指标统计。
 *
 * ── 为什么筛选条件是「动态」的 ──
 *
 * 车辆字段会持续增加，所以这里不写死「平台 / 品牌」两个下拉，而是从
 * `meta.attrs` 里挑可选值有限的字段（维度字段 + 带 options 的字段）自动生成。
 * 新增一个「能源形式」筛选，后端加一行 attr_defs，这里不用改代码。
 *
 * 过滤条件以 `f_<code>` 交给服务端（`/api/indicators/all`），这样分页
 * `total` 才是准的；前端只负责把当前页画出来。
 */
const meta = ref<Meta | null>(null)
const conditions = ref<Condition[]>([])
const datasetTypes = ref<DatasetType[]>([])
const conditionId = ref<number | null>(null)
const conditionName = ref<string>('')
const datasetType = ref<string>('')

const loading = ref(false)

const total = ref(0)
const page = ref(1)
const pageSize = ref(100)

/** 只分两种口径：次数不固定，不做逐次勾选 */
const expScope = ref<'平均' | 'all'>('all')

/** code → 选中的值 */
const filters = reactive<Record<string, string>>({})

/** 能作为筛选器的字段：维度字段或有固定候选值 */
const filterAttrs = computed<AttrDef[]>(() =>
  (meta.value?.attrs ?? []).filter(
    (a) => a.value_type === 'dim' || (a.options?.length ?? 0) > 0,
  ),
)

/** 表格里展示的车辆属性列（show_in_list） */
const listAttrs = computed<AttrDef[]>(() =>
  (meta.value?.attrs ?? []).filter((a) => a.show_in_list),
)

/**
 * 维度字段的候选值取「车辆实际用到的值」，而不是字典全量。
 *
 * 维度字典里同一 dim_code 下可能同时有品牌与子品牌（父节点关系在导入时
 * 是平铺的），全量列出来会让「品牌」下拉里混进子品牌名，选了必然查不到。
 * 用真实数据收敛是唯一不会错的口径。
 */
const attrValues = ref<Record<string, string[]>>({})

function optionsFor(attr: AttrDef): string[] {
  if (attr.value_type === 'dim') {
    return attrValues.value[attr.code] ?? []
  }
  return (attr.options ?? []).map((o) => String(o))
}

interface IndCol {
  rawKey: string
  name: string
  unit: string | null
}

interface Row {
  vehicle: string
  mode: string | null
  experiment: string
  isAverage: boolean
  [k: string]: unknown
}

const allRows = ref<Row[]>([])
const indCols = ref<IndCol[]>([])
const hasAverage = ref(false)

// ── 列可见性 ───────────────────────────────────────────────────────────
//
// 固定列只有「车名 / 试验模式 / 实验」三列，其余全部可选：
// 属性列（attr_defs 里 show_in_list 的那些）默认**不显示** —— 十几列属性
// 铺开就把指标挤到屏幕外了；指标列默认全显示，按需关掉。
// 偏好存 localStorage，换工况 / 刷新后保持。

const COL_PREF_KEY = 'vedb.stats.columns.v1'

/** code → 是否显示；缺省为 false（属性默认收起） */
const attrVisible = ref<Record<string, boolean>>({})
/** rawKey → 是否显示；缺省为 true（指标默认展开） */
const indVisible = ref<Record<string, boolean>>({})

function loadColPref() {
  try {
    const raw = localStorage.getItem(COL_PREF_KEY)
    if (!raw) return
    const p = JSON.parse(raw)
    if (p?.attrs && typeof p.attrs === 'object') attrVisible.value = p.attrs
    if (p?.inds && typeof p.inds === 'object') indVisible.value = p.inds
  } catch {
    // 偏好坏了就当没有，不影响查询
  }
}

function saveColPref() {
  try {
    localStorage.setItem(
      COL_PREF_KEY,
      JSON.stringify({ attrs: attrVisible.value, inds: indVisible.value }),
    )
  } catch {
    // 隐私模式写不进去，忽略
  }
}

const visibleAttrs = computed(() => listAttrs.value.filter((a) => attrVisible.value[a.code] === true))
const visibleInds = computed(() => indCols.value.filter((c) => indVisible.value[c.rawKey] !== false))

const hiddenAttrCount = computed(() => listAttrs.value.length - visibleAttrs.value.length)
const hiddenIndCount = computed(() => indCols.value.length - visibleInds.value.length)

function setAttr(code: string, on: boolean) {
  attrVisible.value = { ...attrVisible.value, [code]: on }
  saveColPref()
}

function setInd(key: string, on: boolean) {
  indVisible.value = { ...indVisible.value, [key]: on }
  saveColPref()
}

function toggleAllAttrs(on: boolean) {
  const next: Record<string, boolean> = {}
  for (const a of listAttrs.value) next[a.code] = on
  attrVisible.value = next
  saveColPref()
}

function toggleAllInds(on: boolean) {
  const next: Record<string, boolean> = {}
  for (const c of indCols.value) next[c.rawKey] = on
  indVisible.value = next
  saveColPref()
}

function flatten(resp: StatsPage) {
  const outRows: Row[] = []
  const map = new Map<string, IndCol>()
  let sawAverage = false

  for (const r of resp.rows as StatsRow[]) {
    const isAvg = r.is_average ?? r.exp_no === '平均'
    if (isAvg) sawAverage = true
    const row: Row = {
      vehicle: r.vehicle,
      mode: r.mode,
      experiment: r.exp_no,
      isAverage: isAvg,
    }
    // 车辆属性是动态的：code → AttrValue
    for (const [code, av] of Object.entries(r.attrs ?? {})) {
      row[code] = av?.value ?? '-'
    }
    for (const [rawKey, v] of Object.entries(r.indicators ?? {})) {
      const val = (v as IndicatorFlat)?.value
      row[rawKey] = val == null ? '-' : Number(Number(val).toFixed(4))
      if (!map.has(rawKey)) {
        map.set(rawKey, {
          rawKey,
          name: (v as IndicatorFlat)?.name ?? rawKey,
          unit: (v as IndicatorFlat)?.unit ?? null,
        })
      }
    }
    outRows.push(row)
  }

  allRows.value = outRows
  total.value = resp.total
  hasAverage.value = sawAverage
  // 按指标名排字典序（raw_key 的数字前缀不可靠，见 README/历史注释）
  indCols.value = [...map.values()].sort((a, b) => {
    const c = a.name.localeCompare(b.name, 'zh')
    return c !== 0 ? c : a.rawKey.localeCompare(b.rawKey, 'zh')
  })
}

async function loadData() {
  loading.value = true
  const t0 = performance.now()
  try {
    const resp = await getAllIndicatorsFlat({
      filters: { ...filters },
      // 用 ID 不用名字（中文名进 URL query 会被某些代理解码成原始字节导致 400）
      condition_id: conditionId.value ?? undefined,
      average: expScope.value === '平均',
      dataset_type: datasetType.value || undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    const net = getLastTiming()
    const tAfterFetch = performance.now()
    flatten(resp)
    await nextTick()
    logTiming('indicators', t0, tAfterFetch, net, resp.rows?.length ?? 0)
  } catch (e: any) {
    ElMessage.error(e.message || '加载失败')
    allRows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function logTiming(
  label: string,
  t0: number,
  tAfterFetch: number,
  net: Timing | null,
  rows: number,
) {
  const renderMs = performance.now() - tAfterFetch
  const totalMs = performance.now() - t0
  const fmt = (ms: number | null) =>
    ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms.toFixed(0)}ms`
  console.log(
    `[timing] ${label}  服务端=${fmt(net?.serverMs ?? null)}` +
      `  网络=${fmt(net ? net.networkMs : tAfterFetch - t0)}` +
      `  解析=${fmt(net?.parseMs ?? null)}` +
      `  渲染=${fmt(renderMs)}` +
      `  合计=${fmt(totalMs)}` +
      `  行数=${rows}`,
  )
}

const rows = computed<Row[]>(() => allRows.value)

function reload() {
  page.value = 1
  loadData()
}

function onPageChange(p: number) {
  page.value = p
  loadData()
}

function onSizeChange(s: number) {
  pageSize.value = s
  page.value = 1
  loadData()
}

function onConditionChange(id: number | null) {
  const c = conditions.value.find((x) => x.id === id)
  conditionId.value = id
  conditionName.value = c?.name ?? ''
  reload()
}

/** export current table to CSV */
function exportCsv() {
  if (!rows.value.length) {
    ElMessage.warning('暂无数据')
    return
  }
  // 导出与屏幕上的列保持一致（所见即所得），避免导出一堆隐藏列
  const head = [
    '车名',
    ...visibleAttrs.value.map((a) => a.name),
    '试验模式',
    '实验',
    ...visibleInds.value.map((c) => (c.unit ? `${c.name}(${c.unit})` : c.name)),
  ]
  const body = rows.value.map((r) => [
    r.vehicle,
    ...visibleAttrs.value.map((a) => String(r[a.code] ?? '')),
    r.mode ?? '',
    r.isAverage ? '平均' : `第${r.experiment}次`,
    ...visibleInds.value.map((c) => String(r[c.rawKey] ?? '')),
  ])
  const csv = [head, ...body]
    .map((line) => line.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','))
    .join('\n')
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `指标统计_${conditionName.value || '全部'}_${
    expScope.value === '平均' ? '仅平均' : '全部实验'
  }.csv`
  a.click()
  URL.revokeObjectURL(url)
}

const summary = computed(() => {
  const shown = visibleInds.value.length
  const all = indCols.value.length
  const ind = shown === all ? `${all} 项指标` : `${shown}/${all} 项指标`
  return `${rows.value.length} 行 / 共 ${total.value} 行 · ${ind}`
})

onMounted(async () => {
  loadColPref()
  try {
    const [m, vehicles] = await Promise.all([getMeta(), getVehicles()])
    meta.value = m
    conditions.value = m.conditions
    datasetTypes.value = m.dataset_types

    // 汇总每个字段在真实车辆上出现过的值（去重排序）
    const values: Record<string, Set<string>> = {}
    for (const v of vehicles) {
      for (const [code, raw] of Object.entries(v.attrs ?? {})) {
        if (raw === null || raw === undefined || raw === '') continue
        const s = String(raw)
        ;(values[code] ??= new Set<string>()).add(s)
      }
    }
    attrValues.value = Object.fromEntries(
      Object.entries(values).map(([k, set]) => [
        k,
        [...set].sort((a, b) => a.localeCompare(b, 'zh')),
      ]),
    )

    if (conditions.value.length) {
      const step = conditions.value.find((c) => c.name === '角阶跃') ?? conditions.value[0]
      conditionId.value = step.id
      conditionName.value = step.name
    }
    if (datasetTypes.value.length) datasetType.value = datasetTypes.value[0].code
  } catch (e: any) {
    ElMessage.error(e.message || '配置加载失败')
  }
  await loadData()
})
</script>

<template>
  <div class="page">
    <el-card shadow="never" class="ctrl-card">
      <div class="ctrl-row">
        <span class="ctrl-label">工况</span>
        <el-select
          :model-value="conditionId"
          placeholder="选择工况"
          size="small"
          style="width: 150px"
          @change="onConditionChange"
        >
          <el-option v-for="c in conditions" :key="c.id" :label="c.name" :value="c.id">
            <span>{{ c.name }}</span>
            <span class="opt-cat">{{ c.category }}</span>
          </el-option>
        </el-select>

        <template v-if="datasetTypes.length > 1">
          <span class="ctrl-label">数据格式</span>
          <el-select
            v-model="datasetType"
            size="small"
            style="width: 150px"
            @change="reload"
          >
            <el-option
              v-for="t in datasetTypes"
              :key="t.code"
              :label="t.name"
              :value="t.code"
            />
          </el-select>
        </template>

        <!-- 筛选条件由字段注册表生成：新增字段不需要改这里 -->
        <template v-for="a in filterAttrs" :key="a.code">
          <span class="ctrl-label">{{ a.name }}</span>
          <el-select
            v-model="filters[a.code]"
            :placeholder="`全部${a.name}`"
            clearable
            filterable
            size="small"
            style="width: 130px"
            @change="reload"
          >
            <el-option
              v-for="v in optionsFor(a)"
              :key="v"
              :label="v"
              :value="v"
            />
          </el-select>
        </template>

        <span class="ctrl-label">实验</span>
        <el-radio-group v-model="expScope" size="small" @change="reload">
          <el-radio-button value="平均">仅平均</el-radio-button>
          <el-radio-button value="all">全部</el-radio-button>
        </el-radio-group>

        <el-button type="primary" size="small" :loading="loading" @click="reload">
          查询
        </el-button>
        <el-button size="small" :disabled="!rows.length" @click="exportCsv">
          导出 CSV
        </el-button>

        <!-- 列设置：属性列 + 指标列都能开关，偏好存本地 -->
        <el-popover placement="bottom-end" :width="300" trigger="click">
          <template #reference>
            <el-button size="small">
              列设置
              <span v-if="hiddenAttrCount + hiddenIndCount" class="hidden-n">
                隐藏 {{ hiddenAttrCount + hiddenIndCount }}
              </span>
            </el-button>
          </template>
          <div class="col-panel">
            <div class="col-group">
              <div class="col-group-head">
                <span>车辆属性</span>
                <span class="col-actions">
                  <el-button link size="small" @click="toggleAllAttrs(true)">全选</el-button>
                  <el-button link size="small" @click="toggleAllAttrs(false)">清空</el-button>
                </span>
              </div>
              <div v-if="!listAttrs.length" class="col-empty">没有可显示的属性</div>
              <div v-else class="col-list">
                <el-checkbox
                  v-for="a in listAttrs"
                  :key="a.code"
                  :model-value="attrVisible[a.code] === true"
                  size="small"
                  @change="(v: any) => setAttr(a.code, !!v)"
                >{{ a.name }}</el-checkbox>
              </div>
            </div>

            <el-divider class="col-divider" />

            <div class="col-group">
              <div class="col-group-head">
                <span>指标（{{ indCols.length }}）</span>
                <span class="col-actions">
                  <el-button link size="small" @click="toggleAllInds(true)">全选</el-button>
                  <el-button link size="small" @click="toggleAllInds(false)">清空</el-button>
                </span>
              </div>
              <div v-if="!indCols.length" class="col-empty">先查询一次</div>
              <div v-else class="col-list">
                <el-checkbox
                  v-for="c in indCols"
                  :key="c.rawKey"
                  :model-value="indVisible[c.rawKey] !== false"
                  size="small"
                  @change="(v: any) => setInd(c.rawKey, !!v)"
                >{{ c.name }}</el-checkbox>
              </div>
            </div>
          </div>
        </el-popover>
      </div>
      <div class="ctrl-row second">
        <span class="hint">
          每台车每个工况的实验次数不固定
          <template v-if="expScope === '平均' && !hasAverage">
            · 当前结果里没有「平均」实验
          </template>
        </span>
        <span class="summary">{{ summary }}</span>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card" v-loading="loading">
      <el-table
        v-if="rows.length"
        :data="rows"
        border
        stripe
        size="small"
        height="calc(100vh - 260px)"
      >
        <!-- 车名：固定列，永远显示 -->
        <el-table-column prop="vehicle" label="车名" width="140" fixed>
          <template #default="{ row }">{{ row.vehicle ?? '-' }}</template>
        </el-table-column>

        <!-- 车辆属性列：数量与名称都由 attr_defs 决定，可在「列设置」里开关 -->
        <el-table-column
          v-for="a in visibleAttrs"
          :key="a.code"
          :prop="a.code"
          :label="a.name"
          min-width="98"
        >
          <template #default="{ row }">{{ row[a.code] ?? '-' }}</template>
        </el-table-column>

        <el-table-column label="试验模式" width="96" fixed align="center">
          <template #default="{ row }">{{ row.mode ?? '—' }}</template>
        </el-table-column>

        <el-table-column label="实验" width="76" fixed align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.isAverage ? 'warning' : 'info'">
              {{ row.isAverage ? '平均' : `第${row.experiment}次` }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column
          v-for="c in visibleInds"
          :key="c.rawKey"
          :prop="c.rawKey"
          min-width="130"
          align="center"
        >
          <template #header>
            <div class="col-head">
              <span class="nm">{{ c.name }}</span>
              <span v-if="c.unit" class="un">{{ c.unit }}</span>
            </div>
          </template>
          <template #default="{ row }">
            <span :class="{ na: row[c.rawKey] === '-' }">{{ row[c.rawKey] }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无数据，请点击查询" />

      <div v-if="total" class="pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          :page-sizes="[50, 100, 200, 500]"
          layout="total, sizes, prev, pager, next, jumper"
          size="small"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.page { height: calc(100vh - 92px); }
.ctrl-card { margin-bottom: 12px; }
.ctrl-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ctrl-row.second {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #eee;
}
.ctrl-label { font-size: 12px; color: #888; flex-shrink: 0; }
.hint { font-size: 11px; color: #aaa; }
.opt-cat { float: right; color: #aaa; font-size: 11px; margin-left: 8px; }
.summary { margin-left: auto; font-size: 12px; color: #888; }
.table-card { height: calc(100% - 116px); }
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 10px 4px 0;
}
.col-head { line-height: 1.2; }
.col-head .nm { display: block; font-size: 11px; }
.col-head .un { display: block; font-size: 10px; color: #aaa; }
.na { color: #ccc; }

/* ── 列设置面板 ── */
.hidden-n {
  margin-left: 4px;
  font-size: 11px;
  color: #e6a23c;
}
.col-panel { max-height: 62vh; overflow-y: auto; }
.col-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #666;
  margin-bottom: 6px;
}
.col-actions { display: flex; gap: 2px; }
.col-list {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  max-height: 190px;
  overflow-y: auto;
}
.col-list :deep(.el-checkbox) { margin-right: 0; height: 24px; }
.col-divider { margin: 8px 0; }
.col-empty { font-size: 12px; color: #bbb; padding: 2px 0 6px; }
</style>
