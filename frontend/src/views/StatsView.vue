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
      condition: conditionName.value || undefined,
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
  const head = [
    ...listAttrs.value.map((a) => a.name),
    '试验模式',
    '实验',
    ...indCols.value.map((c) => (c.unit ? `${c.name}(${c.unit})` : c.name)),
  ]
  const body = rows.value.map((r) => [
    ...listAttrs.value.map((a) => String(r[a.code] ?? '')),
    r.mode ?? '',
    r.isAverage ? '平均' : `第${r.experiment}次`,
    ...indCols.value.map((c) => String(r[c.rawKey] ?? '')),
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

const summary = computed(
  () => `${rows.value.length} 行 / 共 ${total.value} 行 · ${indCols.value.length} 项指标`,
)

onMounted(async () => {
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
        <!-- 车辆属性列：数量与名称都由 attr_defs 决定 -->
        <el-table-column
          v-for="a in listAttrs"
          :key="a.code"
          :prop="a.code"
          :label="a.name"
          min-width="98"
          fixed
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
          v-for="c in indCols"
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
</style>
