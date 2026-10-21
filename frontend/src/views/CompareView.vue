<script setup lang="ts">
import { computed, nextTick, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import VehicleTree from '@/components/VehicleTree.vue'
import CurveChart, { type ChartLine } from '@/components/CurveChart.vue'
import { getCompare, getConditions, getLastTiming, streamCompare, type StreamCurveEvent, type Timing } from '@/api'
import type { CompareResponse, Condition, CurveMeta, Experiment, Indicator } from '@/types'

// ── conditions ──
const conditions = ref<Condition[]>([])
const conditionId = ref<number | null>(null)

// ── selection ──
const selectedIds = ref<number[]>([])
function onTreeChange(ids: number[]) {
  selectedIds.value = ids
}

// ── data ──
/**
 * 对比数据。用 `shallowRef` 而不是 `ref`：
 *
 * 响应里有几万条 `number[]`（曲线点集）。`ref` 会递归地把它整个包成
 * 响应式代理 —— 几万个 Proxy + 每个数组元素的 getter/setter，光是建这些
 * 代理就要几百毫秒到几秒，而且**每个点都多一层间接**，ECharts 读数据时
 * 还要穿过代理。我们从不修改点集，也不需要深层追踪，所以浅引用就够。
 *
 * 副作用是「改深层属性不会触发更新」—— 这正好是流式想要的：收曲线时
 * 只挂数据、不引发全量重算；图表由 `chartBlocks` 单独驱动。
 */
const data = shallowRef<CompareResponse | null>(null)
const loading = ref(false)
/** 曲线数据（第二阶段）是否还在路上 */
const loadingCurves = ref(false)

/**
 * ── 两阶段加载 ──
 *
 * 第一阶段（meta_only）：只拿「车辆 + 指标 + 曲线元信息」，不含 series。
 *   实测 200 车 / 稳态回转 = 811 KB / 0.1s，所以指标表和曲线选择区
 *   立刻就能渲染，用户不用干等。
 * 第二阶段：只为**选中**的曲线取 series（**流式 NDJSON**，边到边画）。
 *
 * 这两个 Set 不参与渲染，所以用普通 Set（不需要响应式）。
 */
/** 已经拿到 series 的曲线名 */
const loadedCurves = new Set<string>()
/**
 * 正在请求中的曲线名 → 发起时的代际。
 *
 * 用 Map 而不是 Set：过期的请求在 `finally` 里只允许删掉**自己那一代**的标记，
 * 否则它会把新一轮刚加进去的标记删掉，导致重复请求。
 */
const pendingCurves = new Map<string, number>()
/**
 * 每次 `loadData` 递增。
 *
 * 在途的第二阶段请求靠它判断「我拿到的还是不是当前这批数据」—— 用户快速
 * 切工况 / 切口径时，旧请求的结果必须丢弃，否则会把**上一批**的曲线点集
 * 并进**这一批**的骨架里（vehicle_id / experiment_id 可能刚好对得上）。
 */
let loadGen = 0

/**
 * ── 曲线区：只需要「工况 + 曲线组」两个选择 ──
 *
 * 曲线组来自接口实际返回的 groups（目前是 cal / duiqi），不写死。
 */
const group = ref<string>('cal')
const selectedCurves = ref<string[]>([])

// ── 视图状态（只影响展示，不重新请求）──
/** 多车同图时的取线规则：平均优先 / 全部实验叠加 */
const expScope = ref<'平均' | 'all'>('平均')
/**
 * 指标表格的实验口径：只分两种。
 * 每台车每个工况实验次数不固定 → 不做「勾选若干实验号」的集合模型。
 */
const tableOnlyAverage = ref(true)

/**
 * ── 降采样 ──
 *
 * 超过阈值点的曲线在**进入 ECharts 之前**用 LTTB 抽点。
 *
 * 为什么默认开着：稳态回转单条曲线可到 10 万点。屏幕宽度就 1000 来像素，
 * 10 万个点里 99% 是画不出来的重合像素；抽到 ~1 万点肉眼几乎无差别，
 * 但内存和渲染开销降一个数量级。
 *
 * 注意它省的是**渲染**（构建点集 + ECharts 建索引），不省网络传输 ——
 * 降采样发生在前端，数据已经下载完了。要省传输得靠后端的 `exp` / `curves`。
 *
 * ⚠️ 界面上**没有**这个选项（按要求去掉），参数保留在这里。要调整就直接改
 * 这个常量，`null` = 关闭降采样。阈值 10000 意味着「1 万点以下的曲线完全
 * 不受影响」，只有 10 万点级别的工况才会抽点。
 */
const DS_THRESHOLD: number | null = 10000

/** 表格列定义 */
interface TableCol {
  key: string
  vehicleName: string
  expNo: string
  isAverage: boolean
}

/** 曲线组列表（来自响应元信息） */
const groups = computed<string[]>(() => {
  const g = data.value?.curve_meta?.groups
  return g ? Object.keys(g) : []
})

/** 当前曲线组下每条曲线的元信息 */
const curveMetas = computed<CurveMeta[]>(() => {
  const g = data.value?.curve_meta?.groups?.[group.value]
  if (!g) return []
  return Object.values(g.curves)
})

/** 所有实验号（平均最后） */
const allExpNos = computed<string[]>(() => {
  const set = new Set<string>()
  for (const v of data.value?.vehicles ?? []) {
    for (const e of v.experiments) set.add(e.experiment_no)
  }
  return [...set].sort((a, b) => {
    if (a === '平均') return 1
    if (b === '平均') return -1
    return Number(a) - Number(b)
  })
})

/** 是否有「平均」实验 —— 实际数据里平均很少，没有就自动退化为全部 */
const hasAverage = computed(() => allExpNos.value.includes('平均'))

async function loadConditions() {
  conditions.value = await getConditions()
  if (conditions.value.length) {
    // default to 角阶跃 if present
    const step = conditions.value.find((c) => c.name === '角阶跃')
    conditionId.value = (step ?? conditions.value[0]).id
  }
}

watch(conditionId, () => {
  streamAbort?.abort() // 在途的流已经没意义了
  data.value = null
  rebuildAllBlocks()
})

/**
 * ── 请求口径 ──
 *
 * 「同图取线」和「指标实验」两个开关只影响展示，但它们共同决定了
 * **要不要**把非平均的实验拉下来：
 *
 *   两个都要平均 → 请求带 `exp_scope=avg_first`（省 ~2/3）
 *   任一要全部   → 只能老老实实取全量
 *
 * 实测 50 车 / 稳态回转：全量 357 MB → 仅平均 119 MB。
 */
const needAllExps = computed(
  () => expScope.value === 'all' || !tableOnlyAverage.value,
)
const requestScope = computed<'avg_first' | undefined>(() =>
  needAllExps.value ? undefined : 'avg_first',
)

/**
 * 单次请求最多带多少个采样点。
 *
 * 服务端按估算的采样点总数卡准入（见 `app/main.py` 的 MAX_COMPARE_NUMBERS，
 * 默认 5000 万）。这里留点余量按 4000 万规划，超了就**拆成多个流式请求** ——
 * 200 台车 × 7 条 1 万点曲线会拆成 7 个请求，每个都是一个独立的流。
 *
 * 注意别跟「服务端内部按 STREAM_BATCH_CURVES 分批取数」搞混：那个是
 * **单条 SQL 的内存**旋钮，这里是**准入检查**的边界。流式并不需要
 * 前端分批来省内存，分批只是为了不撞 400。
 */
const BUDGET_NUMBERS = 40_000_000

/** 当前数据里单台车最多有几个实验（分批估算要用） */
const maxExps = computed(() => {
  let m = 1
  for (const v of data.value?.vehicles ?? []) {
    if (v.experiments.length > m) m = v.experiments.length
  }
  return m
})

/**
 * 一条曲线大概要传多少个数字。
 *
 * ≈ 车辆数 × 实验数 × 点数 × 2(x,y) × 2(假设「原始数据 + 滤波」两条 series)
 */
function numbersPerCurve(): number {
  const cars = data.value?.vehicles.length ?? 0
  const pts = maxPointCount.value || 1
  return cars * maxExps.value * pts * 2 * 2
}

function planBatches(names: string[]): string[][] {
  const perCurve = numbersPerCurve()
  if (!perCurve) return names.length ? [names] : []
  const perBatch = Math.max(1, Math.floor(BUDGET_NUMBERS / perCurve))
  const out: string[][] = []
  for (let i = 0; i < names.length; i += perBatch) {
    out.push(names.slice(i, i + perBatch))
  }
  return out
}

/**
 * 把流里收到的一条曲线挂到骨架的对应位置。
 *
 * 只改 `series` / `series_names` / `series_map` 三件套，元信息与指标沿用
 * meta 阶段的 —— 不用重建整棵对象树。
 *
 * 这里**故意不 `triggerRef(data)`**：`data` 是 shallowRef，改深层属性本来
 * 就不会触发依赖；而依赖 `data` 的那些 computed（指标表、列定义、预估）
 * 在 meta 阶段就已经确定了，跟曲线数据无关。图表由 `chartBlocks` 单独驱动
 * （见 `rebuildDirtyBlocks`），这样流式收 1400 条曲线不会引发 1400 次
 * 全量重算。
 */
function applyCurveEvent(ev: StreamCurveEvent) {
  const cur = data.value
  if (!cur) return
  // 服务端带了 vehicle_id，先按它缩小范围；万一缺失就退回全遍历
  const pool =
    ev.vehicle_id == null
      ? cur.vehicles
      : cur.vehicles.filter((v) => v.vehicle_id === ev.vehicle_id)
  let target: Experiment | undefined
  for (const v of pool) {
    target = v.experiments.find((x) => x.experiment_id === ev.experiment_id)
    if (target) break
  }
  if (!target) return
  const bucket = ev.group === 'cal' ? target.curves_cal : target.curves_duiqi
  const tgt = bucket?.[ev.curve.name]
  if (!tgt) return
  tgt.series = ev.curve.series ?? []
  if (ev.curve.series_names) tgt.series_names = ev.curve.series_names
  if (ev.curve.series_map) tgt.series_map = ev.curve.series_map
  dirtyBlocks.add(ev.curve.name)
}

/** 在途的流。切工况 / 重新加载时主动掐断，别让服务端白算一遍。 */
let streamAbort: AbortController | null = null

/**
 * 第二阶段：只为还没加载的曲线取 series。
 *
 * ── 走流式（NDJSON），不再是「一个端点一次性返回」 ──
 *
 * 老写法是 `getCompare` + `mergeSeries`：服务端要把整个响应序列化成一个
 * 字符串（实测内存放大 ~8×），前端也要整体 `JSON.parse` 一次。
 * 现在每收一条曲线就挂上骨架、按 200ms 节流重绘，**边到边画**。
 *
 * 仍然按 `planBatches` 分批 —— 那是**准入检查**的分批（服务端
 * `MAX_COMPARE_NUMBERS` 按估算采样点卡），不是渲染的需要。
 */
async function loadCurves(names: string[], gen: number = loadGen) {
  const todo = names.filter(
    (n) => !loadedCurves.has(n) && !pendingCurves.has(n),
  )
  if (!todo.length || !data.value || !conditionId.value) return
  if (!streamAbort) streamAbort = new AbortController() // watcher 先于 loadData 触发时兜底
  // 同步标记（挡住 watcher 与 loadData 的并发重复请求）
  todo.forEach((n) => pendingCurves.set(n, gen))
  loadingCurves.value = true
  try {
    for (const batch of planBatches(todo)) {
      await streamCompare(
        selectedIds.value,
        conditionId.value,
        { curves: batch, expScope: requestScope.value },
        {
          onCurve: (ev) => {
            applyCurveEvent(ev)
            scheduleFlush()
          },
        },
        streamAbort?.signal,
      )
      if (gen !== loadGen) return // 过期：已经被更新的一次加载取代，丢弃
      flushNow()
      batch.forEach((n) => loadedCurves.add(n))
      await nextTick() // 让每批画完再发下一批，用户能看到图逐步出现
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') return // 我们自己掐断的，不是错误
    if (gen === loadGen) ElMessage.error(e.message || '曲线加载失败')
  } finally {
    for (const n of todo) {
      if (pendingCurves.get(n) === gen) pendingCurves.delete(n)
    }
    if (gen === loadGen) loadingCurves.value = false
  }
}

async function loadData() {
  if (!conditionId.value) {
    ElMessage.warning('请选择工况')
    return
  }
  if (!selectedIds.value.length) {
    ElMessage.warning('请至少选择一台车')
    return
  }
  // 掐断上一次在途的流 —— 切工况 / 重复点击时别让服务端白算一遍
  streamAbort?.abort()
  streamAbort = new AbortController()

  loading.value = true
  const t0 = performance.now()
  const gen = ++loadGen
  try {
    // ── 第一阶段：只要骨架（车辆 + 指标 + 曲线元信息，不含 series）──
    //    200 车 / 稳态回转实测 811 KB / 0.1s，所以表格和曲线选择区立刻出来
    const meta = await getCompare(selectedIds.value, conditionId.value, {
      metaOnly: true,
      expScope: requestScope.value,
    })
    if (gen !== loadGen) return // 已被更新的一次加载取代
    const tMenu = performance.now()
    const netMenu = getLastTiming()
    data.value = meta
    loadedCurves.clear()
    rebuildAllBlocks() // 骨架换了，图表先清空（第二阶段到货后逐条补上）
    // 菜单阶段单独打一行 —— 它决定「用户多久能看到表格和曲线选择区」
    logTiming('compare/菜单', t0, tMenu, netMenu)

    // ── 曲线组：优先用响应里有的，否则退回 cal ──
    const gs = meta.curve_meta ? Object.keys(meta.curve_meta.groups) : []
    if (gs.length && !gs.includes(group.value)) group.value = gs[0]

    if (!meta.vehicles.length) {
      ElMessage.warning('所选车辆在该工况下无数据')
      return
    }
    // 该工况完全没有「平均」实验时，「仅平均」会得到空表 —— 自动切到全部。
    // 这一步会把 needAllExps 由 false 变 true，由下面的 watcher 重新加载全量。
    //
    // 不会来回震荡：`hasAverage` 只取决于「有没有任何一台车存在平均」，
    // 而 avg_first 对存在平均的车就是返回平均，所以两种口径下它取值相同。
    if (!hasAverage.value && tableOnlyAverage.value) {
      tableOnlyAverage.value = false
      return
    }

    syncCurveState()

    const pts = maxPointCount.value
    if (pts >= 50000 && meta.vehicles.length > 2) {
      ElMessage.warning(
        `该工况单条曲线 ${pts.toLocaleString()} 点，${meta.vehicles.length} 台车全分辨率叠加会比较吃内存`,
      )
    }

    // ── 第二阶段：只为选中的曲线取数据 ──
    await loadCurves(selectedCurves.value, gen)

    // 等 DOM + ECharts 画完再统计，才是「用户看到图」的时刻
    const tAfterFetch = performance.now()
    await nextTick()
    logTiming('compare/曲线', t0, tAfterFetch, getLastTiming())
    ElMessage.success(`加载完成 ${((performance.now() - t0) / 1000).toFixed(2)}s`)
  } catch (e: any) {
    if (gen === loadGen) {
      ElMessage.error(e.message || '加载失败')
      data.value = null
    }
  } finally {
    if (gen === loadGen) loading.value = false
  }
}

/**
 * 换口径（仅平均 ↔ 全部实验）必须重新取数 —— 数据本身就不一样了。
 * 只在已经有数据时触发，避免首次加载时重复请求。
 */
watch(needAllExps, () => {
  if (data.value) loadData()
})

/**
 * 勾选曲线变化时**只补拉新勾选的**，已经加载过的直接用。
 * 取消勾选不需要做任何事（数据留着，只是不画）。
 *
 * ⚠️ 必须 `deep: true`：Element Plus 的 multiple select 可能**原地**改
 * `v-model` 数组（push/splice），引用不变 —— 浅 watch 不会触发。
 * 以前 `chartBlocks` 是 computed，遍历数组会追踪到内容变化，所以看不出来；
 * 现在图表是手动维护的，漏一次触发就是「勾了曲线但图不出来」。
 * 数组元素只有十来个字符串，deep 的开销可以忽略。
 */
watch(
  selectedCurves,
  (names) => {
    rebuildAllBlocks() // 勾选集变了：图表列表跟着变（新曲线此时还是空的）
    if (data.value) loadCurves(names)
  },
  { deep: true },
)

/**
 * 同图取线口径（平均 / 全部实验）变了 —— `expsFor()` 选出的实验不同，
 * 每条曲线要画的线数就不同，所以图表得重建（数据本身没变，不用重新取）。
 */
watch(expScope, () => {
  if (data.value) rebuildAllBlocks()
})

/**
 * 把耗时拆成四段打到控制台（不占界面）。
 *
 * 服务的「加载完成 X.Xs」是 `performance.now()` 从点击到数据赋值完成，
 * 里面**混着**服务端 SQL+序列化、网络传输、JSON.parse、Vue 赋值 + ECharts 渲染。
 * 拆开打出来才能判断到底慢在哪一段——界面不需要看到这些。
 *
 * 服务端那段读的是响应头 `X-Server-Time-Ms`（后端自报，见 app/timing.py）。
 */
function logTiming(label: string, t0: number, tAfterFetch: number, net: Timing | null) {
  const renderMs = performance.now() - tAfterFetch
  const totalMs = performance.now() - t0
  const fmt = (ms: number | null) =>
    ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms.toFixed(0)}ms`
  console.log(
    `[timing] ${label}  服务端=${fmt(net?.serverMs ?? null)}` +
      `  网络=${fmt(net ? net.networkMs : tAfterFetch - t0)}` +
      `  解析=${fmt(net?.parseMs ?? null)}` +
      `  渲染=${fmt(renderMs)}` +
      `  合计=${fmt(totalMs)}`,
  )
}

/**
 * 换曲线组时重置曲线选择。
 *
 * 数据种类（series 的 curvename）不做用户选择 —— 那是算法侧的产物，
 * 有多少种、叫什么名字都由数据决定，前端一律**全部画出**。
 */
function syncCurveState() {
  selectedCurves.value = curveMetas.value.map((c) => c.name)
}

watch(group, () => {
  syncCurveState()
})

/** 当前组单条曲线的最大采样点数（决定降采样是否触发） */
const maxPointCount = computed<number>(() => {
  if (!data.value) return 0
  let mx = 0
  for (const c of curveMetas.value) {
    if (c.point_count > mx) mx = c.point_count
  }
  return mx
})

/** 当前取值策略下，一台车每条曲线贡献几条线 */
function expsFor(expList: Experiment[]): Experiment[] {
  if (expScope.value === '平均') {
    const avg = expList.filter((e) => e.is_average)
    if (avg.length) return avg
    // 没有平均 → 退回第一次实验，避免图上一片空白
    const first = expList.find((e) => e.experiment_no === '1')
    return first ? [first] : expList.slice(0, 1)
  }
  return expList
}

/**
 * 指标行：所有实验里出现过的 raw_key 的并集。
 *
 * ── 排序为什么按 name 而不是 seq ──
 *
 * `seq` 是 raw_key 的数字前缀，靠不住：
 *   1. **数字可能省略** —— 算法侧有时给 '侧向加速度稳态值(g)'，没有前缀，
 *      此时 seq 为空，靠数字排序会把它踢到表格最底下；
 *   2. **它不是工况内序号，而是跨工况统一编号** —— 同一个「车速」在
 *      角阶跃是 10、角脉冲是 100，表达的是「这是什么指标」而非「排第几」，
 *      信息和 name 重复；
 *   3. **同一工况内还可能不连续**（真实数据删过指标就会断号）。
 *
 * 所以统一按 `name` 排字典序（中文用 localeCompare），顺序稳定且与编号无关。
 */
const indicatorRows = computed<Indicator[]>(() => {
  if (!data.value) return []
  const map = new Map<string, Indicator>()
  for (const v of data.value.vehicles) {
    for (const e of v.experiments) {
      for (const [rawKey, ind] of Object.entries(e.params ?? {})) {
        if (!map.has(rawKey)) map.set(rawKey, ind)
      }
    }
  }
  return [...map.values()].sort((a, b) => {
    const c = (a.name ?? '').localeCompare(b.name ?? '', 'zh')
    // 同名（理论上不该有，但可能有不同单位）时按 raw_key 兜底稳定排序
    return c !== 0 ? c : a.raw_key.localeCompare(b.raw_key, 'zh')
  })
})

/**
 * 表格列：每 (车 × 实验) 一列。
 *
 * ── 口径只分两种：仅平均 / 全部实验 ──
 *
 * 每台车每个工况的实验次数**不固定**，所以不做「勾选若干实验号」这种集合模型
 * （次数一变 UI 就崩），只用布尔口径来判断。
 *
 * key 用 `experiment_id`（全局唯一）而不是 experiment_no ——
 * 实验号在不同车之间会重号（都有 '1'、'2'、'平均'），拼字符串做 key 有撞的风险。
 */
const tableCols = computed<TableCol[]>(() => {
  if (!data.value) return []
  const cols: TableCol[] = []
  for (const v of data.value.vehicles) {
    for (const e of v.experiments) {
      if (tableOnlyAverage.value && !e.is_average) continue
      cols.push({
        key: `exp_${e.experiment_id}`,
        vehicleName: v.vehicle_name,
        expNo: e.experiment_no,
        isAverage: e.is_average,
      })
    }
  }
  return cols
})

/** table rows: one per indicator */
const tableRows = computed(() => {
  if (!data.value) return []
  return indicatorRows.value.map((ind) => {
    const row: Record<string, unknown> = {
      rawKey: ind.raw_key,
      name: ind.name,
      unit: ind.unit ?? '',
    }
    // 只填当前口径下要展示的列
    for (const v of data.value!.vehicles) {
      for (const e of v.experiments) {
        if (tableOnlyAverage.value && !e.is_average) continue
        const key = `exp_${e.experiment_id}`
        const val = e.params?.[ind.raw_key]?.value
        row[key] = val == null ? '-' : Number(val.toFixed(4))
      }
    }
    return row
  })
})

/**
 * 一条曲线 → 若干条待绘制的线。
 *
 * 曲线里的 series 有多少条就画多少条（原始数据 / 滤波 / 以后新增的任何种类），
 * 不做筛选：数据种类由算法侧决定，前端不参与选择。
 *
 * 命名规则：
 *   - series 只有 1 条   → 「车型」（如对齐曲线组，只有滤波，名字带种类反而啰嗦）
 *   - series 多于 1 条   → 「车型 · 种类」，把种类标出来
 *   - 叠加「全部实验」时再追加「#实验号」
 */
function linesForCurve(curveName: string): ChartLine[] {
  if (!data.value) return []
  const lines: ChartLine[] = []
  for (const v of data.value.vehicles) {
    for (const e of expsFor(v.experiments)) {
      const curves = group.value === 'cal' ? e.curves_cal : e.curves_duiqi
      const curve = curves?.[curveName]
      if (!curve) continue
      const list = curve.series ?? []
      if (!list.length) continue
      const expSuffix = expScope.value === '平均' ? '' : ` #${e.experiment_no}`
      const multi = list.length > 1
      for (let i = 0; i < list.length; i++) {
        const s = list[i]
        const kind = s.curvename ?? `series${i}`
        lines.push({
          name: multi
            ? `${v.vehicle_name} · ${kind}${expSuffix}`
            : `${v.vehicle_name}${expSuffix}`,
          x: s.x,
          y: s.y,
        })
      }
    }
  }
  return lines
}

interface ChartBlock {
  name: string
  title: string
  xlabel: string
  ylabel: string
  pointCount: number
  lines: ChartLine[]
}

/**
 * ── 图表块：为什么不是 computed ──
 *
 * 流式收数据时，一条曲线到货就得让对应的图重画，但**不能让所有图都重画**。
 *
 * 如果写成 computed，`data` 里任何一处变化都会让**所有** block 生成新的
 * `lines` 数组（哪怕那条曲线的数据根本没变），于是每个 `CurveChart` 的
 * watch 都被触发。50 车 × 7 条线 × 1 万点 ≈ 350 万个点，一次全量重画
 * 要重建所有 ECharts 索引 —— 收一条画一次，流式的收益全被吃光。
 *
 * 所以这里手动维护：只有数据真的变了的曲线才换新对象，其余保持**同一个
 * 引用**，浅 watch 自然不触发。
 */
const chartBlocks = shallowRef<ChartBlock[]>([])

/** 数据刚变过、需要重建 lines 的曲线名 */
const dirtyBlocks = new Set<string>()

function buildBlock(name: string): ChartBlock {
  const meta = curveMetas.value.find((c) => c.name === name)
  return {
    name,
    title: meta?.title ?? name,
    xlabel: meta?.xlabel ?? '',
    ylabel: meta?.ylabel ?? '',
    pointCount: meta?.point_count ?? 0,
    lines: linesForCurve(name),
  }
}

/** 全量重建：换工况 / 换曲线组 / 换口径 / 勾选变化时 */
function rebuildAllBlocks() {
  dirtyBlocks.clear()
  chartBlocks.value = data.value ? selectedCurves.value.map(buildBlock) : []
}

/** 只重建变过的那几条，其余保持原引用（= 不触发重画） */
function rebuildDirtyBlocks() {
  if (!dirtyBlocks.size) return
  const want = new Set(selectedCurves.value)
  const byName = new Map(chartBlocks.value.map((b) => [b.name, b]))
  for (const n of dirtyBlocks) {
    if (want.has(n)) byName.set(n, buildBlock(n))
  }
  dirtyBlocks.clear()
  // 按 selectedCurves 的顺序输出，并丢掉已取消勾选的
  chartBlocks.value = selectedCurves.value
    .map((n) => byName.get(n))
    .filter((b): b is ChartBlock => !!b)
}

/**
 * 重绘节流间隔（只在 `LIVE_RENDER` 打开时生效）。
 *
 * 太密（80ms）→ 反复重画，卡；太疏（1s）→ 看不出「边到边画」。
 */
const FLUSH_MS = 350

/**
 * 流式过程中是否「边收边画」。
 *
 * ── 为什么默认关掉 ──
 *
 * 一次 `setOption` 要把该图**所有**线的数据重建索引。25 车 × 7 曲线 ×
 * 1 万点 = 350 万个点，单张图就要 ~350ms —— 而边收边画意味着这个代价
 * 要付好几遍。实测（25 车 / 稳态回转，`CARS=25 bash scripts/run_e2e.sh`）：
 * 开着是 6 个长任务、合计 1607ms，用户感觉就是「一直在卡」。
 *
 * 关掉之后：每个流式请求结束时画一次，长任务降到 1~2 个。
 * 用户看到的是「loading 几秒 → 图出来」，比「图一点点长出来但一直在卡」
 * 舒服得多。加载本身的时间没有变化，变的只是重画次数。
 *
 * 数据量小（几千点）时打开它体验更好 —— 那就把它设成 true。
 */
const LIVE_RENDER = false

let flushTimer: ReturnType<typeof setTimeout> | null = null

function scheduleFlush() {
  if (!LIVE_RENDER) return // 流式期间不重画，等这一批结束的 flushNow()
  if (flushTimer !== null) return
  flushTimer = setTimeout(() => {
    flushTimer = null
    rebuildDirtyBlocks()
  }, FLUSH_MS)
}

function flushNow() {
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  rebuildDirtyBlocks()
}

onMounted(loadConditions)
</script>

<template>
  <div class="page">
    <el-row :gutter="12" class="fill-row">
      <!-- left: directory tree -->
      <el-col :span="5" class="tree-col">
        <VehicleTree @change="onTreeChange" />
      </el-col>

      <!-- right: controls + charts + table -->
      <el-col :span="19" class="main-col">
        <el-card shadow="never" class="ctrl-card">
          <!-- 曲线区：只保留 工况 + 曲线组 -->
          <div class="ctrl-row">
            <span class="ctrl-label">工况</span>
            <el-select v-model="conditionId" placeholder="选择工况" size="small" style="width: 150px">
              <el-option
                v-for="c in conditions"
                :key="c.id"
                :label="c.name"
                :value="c.id"
              >
                <span>{{ c.name }}</span>
                <span class="opt-cat">{{ c.category }}</span>
              </el-option>
            </el-select>

            <span class="ctrl-label">曲线组</span>
            <el-radio-group v-model="group" size="small" :disabled="!groups.length">
              <el-radio-button v-for="g in groups" :key="g" :value="g">
                {{ g === 'cal' ? '计算曲线' : g === 'duiqi' ? '对齐曲线' : g }}
              </el-radio-button>
            </el-radio-group>

            <el-button
              type="primary"
              size="small"
              :loading="loading || loadingCurves"
              @click="loadData"
            >
              {{ loadingCurves ? '加载曲线中' : '加载对比' }}
            </el-button>

            <template v-if="data">
              <span class="ctrl-label">同图取线</span>
              <el-radio-group v-model="expScope" size="small">
                <el-radio-button value="平均" :disabled="!hasAverage">平均</el-radio-button>
                <el-radio-button value="all">全部实验</el-radio-button>
              </el-radio-group>
            </template>
          </div>

          <!-- 曲线选择（数据驱动，按实际曲线名列出） -->
          <div v-if="data && curveMetas.length" class="ctrl-row second">
            <span class="ctrl-label">曲线</span>
            <el-checkbox
              size="small"
              :model-value="selectedCurves.length === curveMetas.length"
              :indeterminate="selectedCurves.length > 0 && selectedCurves.length < curveMetas.length"
              @change="(v: any) => (selectedCurves = v ? curveMetas.map((c) => c.name) : [])"
            >
              全选
            </el-checkbox>
            <el-select
              v-model="selectedCurves"
              multiple
              collapse-tags
              collapse-tags-tooltip
              size="small"
              style="min-width: 260px; flex: 1"
              placeholder="选择要绘制的曲线"
            >
              <el-option v-for="c in curveMetas" :key="c.name" :label="c.title || c.name" :value="c.name" />
            </el-select>
            <span class="hint">
              {{ curveMetas.length }} 条曲线 · 单条最多
              {{ maxPointCount.toLocaleString() }} 点（全分辨率）
            </span>
          </div>

          <!-- 数据种类不做用户选择：算法侧给几种就画几种 -->

          <!-- 指标表格：实验筛选只在表格里，且只分 仅平均 / 全部 -->
          <div v-if="data" class="ctrl-row second">
            <span class="ctrl-label">指标实验</span>
            <el-radio-group v-model="tableOnlyAverage" size="small">
              <el-radio-button :value="true" :disabled="!hasAverage">
                仅平均
              </el-radio-button>
              <el-radio-button :value="false">全部实验</el-radio-button>
            </el-radio-group>
            <span class="hint">
              共 {{ tableCols.length }} 列 · {{ indicatorRows.length }} 项指标
              <template v-if="!hasAverage">· 该工况无「平均」实验</template>
              <template v-else-if="!needAllExps">
                · 当前只加载了「平均」（切到「全部实验」会重新取数）
              </template>
            </span>
          </div>
        </el-card>

        <!-- charts -->
        <div
          v-if="data && chartBlocks.length"
          v-loading="loadingCurves"
          element-loading-text="曲线数据加载中…"
          class="charts"
        >
          <el-card
            v-for="b in chartBlocks"
            :key="b.name"
            shadow="never"
            class="chart-card"
          >
            <template #header>
              <div class="chart-title">
                <span>{{ b.title }}</span>
                <span class="curve-tag">
                  {{ b.name }} · {{ b.lines.length }} 条线
                </span>
              </div>
            </template>
            <CurveChart
              :title="b.title"
              :xlabel="b.xlabel"
              :ylabel="b.ylabel"
              :lines="b.lines"
              :point-count="b.pointCount"
              :downsample-threshold="DS_THRESHOLD"
            />
          </el-card>
        </div>

        <el-empty v-else-if="!data" description="选择车辆与工况后点击「加载对比」" />

        <!-- indicator table -->
        <el-card v-if="data && tableRows.length" shadow="never" class="table-card">
          <template #header>
            <div class="chart-title">
              <span>指标对比表</span>
              <span class="curve-tag">
                {{ data.condition_name }} · {{ tableCols.length }} 列 ·
                {{ indicatorRows.length }} 项指标
              </span>
            </div>
          </template>
          <el-table
            :data="tableRows"
            border
            stripe
            size="small"
            height="460"
            class="ind-table"
          >
            <!-- 行号：编号（seq）不可靠，这里只表示第几行 -->
            <el-table-column type="index" label="#" width="52" fixed />
            <el-table-column prop="name" label="指标" width="210" fixed show-overflow-tooltip>
              <template #default="{ row }">
                <span>{{ row.name }}</span>
                <span v-if="row.unit" class="unit">({{ row.unit }})</span>
              </template>
            </el-table-column>
            <el-table-column
              v-for="c in tableCols"
              :key="c.key"
              :prop="c.key"
              min-width="128"
              align="center"
            >
              <template #header>
                <div class="col-head">
                  <span class="veh">{{ c.vehicleName }}</span>
                  <span class="exp">{{ c.isAverage ? '平均' : `第${c.expNo}次` }}</span>
                </div>
              </template>
              <template #default="{ row }">
                <span :class="{ na: row[c.key] === '-' }">{{ row[c.key] }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.page { height: calc(100vh - 92px); }
.fill-row { height: 100%; }
.tree-col { height: 100%; }
.main-col {
  height: 100%;
  overflow-y: auto;
  padding-right: 4px;
}
.ctrl-card { margin-bottom: 12px; }
.ctrl-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ctrl-row.second {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #eee;
}
.ctrl-label {
  font-size: 12px;
  color: #888;
  flex-shrink: 0;
}
.opt-cat {
  float: right;
  color: #aaa;
  font-size: 11px;
  margin-left: 8px;
}
.hint { font-size: 11px; color: #aaa; }
.hint.ok { color: #67c23a; }
.ds-hit {
  font-size: 11px;
  color: #e6a23c;
  background: #fdf6ec;
  padding: 1px 7px;
  border-radius: 4px;
}
.charts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.chart-card :deep(.el-card__header) { padding: 8px 12px; }
.chart-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
}
.curve-tag {
  font-size: 11px;
  font-weight: 400;
  color: #999;
  background: #f5f5f5;
  padding: 2px 7px;
  border-radius: 4px;
}
.table-card :deep(.el-card__header) { padding: 8px 12px; }
.unit { color: #aaa; font-size: 11px; margin-left: 3px; }
.col-head { line-height: 1.25; }
.col-head .veh { display: block; font-size: 12px; font-weight: 600; }
.col-head .exp { display: block; font-size: 10px; color: #999; }
.na { color: #ccc; }
</style>
