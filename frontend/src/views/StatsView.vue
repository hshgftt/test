<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getAllIndicatorsFlat, getConditions, getLastTiming, getTree, type Timing } from '@/api'
import type { Condition, IndicatorFlat, IndicatorPage } from '@/types'

const conditions = ref<Condition[]>([])
const conditionId = ref<number | null>(null)
const conditionName = ref<string>('')

const platforms = ref<string[]>([])
const platform = ref<string>('')

const loading = ref(false)

/**
 * 200 车 × 20 工况会产生上万行，页面 2 走 flat + 分页，
 * 不再一次性把嵌套结构全拉下来。
 */
const total = ref(0)
const page = ref(1)
const pageSize = ref(100)

/**
 * 实验口径：只有两种。
 *
 * 每台车每个工况的实验次数**不固定**（可能 1 次、3 次、或者根本没有平均），
 * 所以这里不做「第一次 / 第二次」这种逐次勾选——那种 UI 在次数不定的数据上
 * 本来就不成立。筛选在**服务端**做，这样分页 total 才是准的。
 */
const expScope = ref<'平均' | 'all'>('all')

interface IndCol {
  rawKey: string
  name: string
  unit: string | null
}

interface Row {
  platform: string
  brand: string
  vehicle: string
  experiment: string
  isAverage: boolean
  [k: string]: unknown
}

const allRows = ref<Row[]>([])
const indCols = ref<IndCol[]>([])

/** 本次结果里是否真的出现了「平均」实验 */
const hasAverage = ref(false)

async function loadConditions() {
  conditions.value = await getConditions()
  if (conditions.value.length) {
    const step = conditions.value.find((c) => c.name === '角阶跃') ?? conditions.value[0]
    conditionId.value = step.id
    conditionName.value = step.name
  }
  const tree = await getTree()
  platforms.value = Object.keys(tree)
}

function flatten(resp: IndicatorPage) {
  const outRows: Row[] = []
  const map = new Map<string, IndCol>()
  let sawAverage = false

  for (const r of resp.rows) {
    const isAvg = r.is_average ?? r.experiment_no === '平均'
    if (isAvg) sawAverage = true
    const row: Row = {
      platform: r.platform,
      brand: r.brand,
      vehicle: r.vehicle,
      experiment: r.experiment_no,
      isAverage: isAvg,
    }
    for (const [rawKey, v] of Object.entries(
      r.indicators as Record<string, IndicatorFlat>,
    )) {
      const val = v?.value
      row[rawKey] = val == null ? '-' : Number(Number(val).toFixed(4))
      if (!map.has(rawKey)) {
        map.set(rawKey, {
          rawKey,
          name: v?.name ?? rawKey,
          unit: v?.unit ?? null,
        })
      }
    }
    outRows.push(row)
  }

  allRows.value = outRows
  total.value = resp.total
  hasAverage.value = sawAverage
  // 按指标名排字典序。不要用 raw_key 的数字前缀：
  // 数字可能省略，且同一指标在不同工况下编号不同（车速：角阶跃10 / 角脉冲100）。
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
      platform: platform.value || undefined,
      condition: conditionName.value || undefined,
      average: expScope.value === '平均',
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    const net = getLastTiming()
    const tAfterFetch = performance.now()
    flatten(resp)
    // 等表格真正渲染完（v-loading 撤下、行 DOM 生成）再统计
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

/**
 * 把耗时拆成四段打到控制台（不占界面）。
 *
 * 服务的 `serverMs` 读的是响应头 `X-Server-Time-Ms`（后端自报的 SQL + 序列化），
 * 其余三段是前端侧测的。拆开打才能判断慢在数据库还是渲染。
 */
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

/** 服务端已按口径过滤，这里直接展示 */
const rows = computed<Row[]>(() => allRows.value)

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
  page.value = 1
  loadData()
}

function onPlatformChange() {
  page.value = 1
  loadData()
}

function onScopeChange() {
  page.value = 1
  loadData()
}

/** export current table to CSV */
function exportCsv() {
  if (!rows.value.length) {
    ElMessage.warning('暂无数据')
    return
  }
  const head = ['平台', '品牌', '车型', '实验', ...indCols.value.map((c) =>
    c.unit ? `${c.name}(${c.unit})` : c.name,
  )]
  const body = rows.value.map((r) => [
    r.platform,
    r.brand,
    r.vehicle,
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
  await loadConditions()
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
          style="width: 160px"
          @change="onConditionChange"
        >
          <el-option v-for="c in conditions" :key="c.id" :label="c.name" :value="c.id">
            <span>{{ c.name }}</span>
            <span class="opt-cat">{{ c.category }}</span>
          </el-option>
        </el-select>

        <span class="ctrl-label">平台</span>
        <el-select
          v-model="platform"
          placeholder="全部平台"
          clearable
          size="small"
          style="width: 150px"
          @change="onPlatformChange"
        >
          <el-option label="全部平台" value="" />
          <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
        </el-select>

        <!-- 实验：只有两种口径（次数不固定，不做逐次勾选） -->
        <span class="ctrl-label">实验</span>
        <el-radio-group v-model="expScope" size="small" @change="onScopeChange">
          <el-radio-button value="平均">仅平均</el-radio-button>
          <el-radio-button value="all">全部</el-radio-button>
        </el-radio-group>
        <span class="hint">
          每台车每个工况的实验次数不固定
          <template v-if="expScope === '平均' && !hasAverage">· 当前结果里没有「平均」实验</template>
        </span>

        <el-button type="primary" size="small" :loading="loading" @click="loadData">
          查询
        </el-button>
        <el-button size="small" @click="exportCsv" :disabled="!rows.length">
          导出 CSV
        </el-button>
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
        height="calc(100vh - 220px)"
      >
        <el-table-column prop="platform" label="平台" width="110" fixed />
        <el-table-column prop="brand" label="品牌" width="100" fixed />
        <el-table-column prop="vehicle" label="车型" width="120" fixed />
        <el-table-column prop="experiment" label="实验" width="72" fixed align="center">
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
.ctrl-label { font-size: 12px; color: #888; flex-shrink: 0; }
.hint { font-size: 11px; color: #aaa; }
.opt-cat { float: right; color: #aaa; font-size: 11px; margin-left: 8px; }
.summary { margin-left: auto; font-size: 12px; color: #888; }
.table-card { height: calc(100% - 76px); }
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
