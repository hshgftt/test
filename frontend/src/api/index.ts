import axios from 'axios'
import type {
  AllIndicators,
  CompareResponse,
  Condition,
  Curve,
  IndicatorPage,
  TreeResponse,
  VehicleBrief,
} from '@/types'

const http = axios.create({
  // 10万点曲线 + 多车对比的响应可以到几百 MB，超时给足
  baseURL: '/api',
  timeout: 300000,
})

/**
 * 单次请求的耗时拆分。
 *
 * 服务端会在响应头回 `X-Server-Time-Ms`（后端路由 + SQL + 序列化的总耗时），
 * 这里把前端侧的时间也记下来，两者的差就是真正的「网络 + 解析 + 渲染」开销。
 */
export interface Timing {
  /** 后端自报耗时（ms），读不到响应头时为 null */
  serverMs: number | null
  /** 从发请求到 axios 拿到响应对象（含网络传输 + 响应体接收），ms */
  networkMs: number
  /** 从发请求到调用方拿到已解析的 JS 对象（含 JSON.parse），ms */
  parseMs: number
}

let lastTiming: Timing | null = null

/** 取最近一次请求的耗时拆分 */
export function getLastTiming(): Timing | null {
  return lastTiming
}

function readServerMs(headers: any): number | null {
  const v = headers?.['x-server-time-ms']
  if (v === undefined || v === null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

http.interceptors.response.use(
  (res) => {
    const t = (res.config as any).__t0
    if (typeof t === 'number') {
      lastTiming = {
        serverMs: readServerMs(res.headers),
        networkMs: performance.now() - t,
        parseMs: performance.now() - t,
      }
    }
    return res
  },
  (err) => {
    const msg = err?.response?.data?.detail || err.message || '请求失败'
    return Promise.reject(new Error(msg))
  },
)

/** 给请求打上起始时间戳，并解析出 JSON 后的耗时 */
async function timed<T>(p: Promise<{ data: T; config: any; headers: any }>): Promise<T> {
  const res = await p
  const t = res.config?.__t0
  if (typeof t === 'number') {
    lastTiming = {
      serverMs: readServerMs(res.headers),
      networkMs: (res.config.__t1 ?? performance.now()) - t,
      parseMs: performance.now() - t,
    }
  }
  return res.data
}

/** 在所有请求发出前盖时间戳 */
http.interceptors.request.use((config) => {
  ;(config as any).__t0 = performance.now()
  return config
})
http.interceptors.response.use((res) => {
  if ((res.config as any).__t1 === undefined) {
    ;(res.config as any).__t1 = performance.now()
  }
  return res
})

/** API 1 — directory tree: platform → brand → vehicle */
export function getTree(platform?: string): Promise<TreeResponse> {
  return timed(http.get('/tree', { params: { platform } }))
}

/** Flat vehicle list (with platform / brand / drive_type) */
export function getVehicles(): Promise<
  (VehicleBrief & { brand: string; platform: string; drive_type: string | null })[]
> {
  return timed(http.get('/vehicles'))
}

/** All conditions (工况) */
export function getConditions(): Promise<Condition[]> {
  return timed(http.get('/conditions'))
}

/** Conditions available for one vehicle */
export function getVehicleConditions(vehicleId: number): Promise<Condition[]> {
  return timed(http.get(`/vehicles/${vehicleId}/conditions`))
}

/** 只取指定实验号 / 曲线，用来收敛大工况（10万点）的响应体积 */
export interface DataQuery {
  exp?: string[] // ['平均'] / ['1', '2'] — 不传 = 全部
  curves?: string[] // 不传 = 全部曲线
  /**
   * 实验口径。`'avg_first'` = 每台车只取「平均」（没有平均则取第一次）。
   *
   * 前端默认视图（曲线只画平均、指标表只显示平均列）要的就是这一次实验，
   * 全量拉下来有 2/3 是白拉。实测 50 车 / 稳态回转：357MB → 119MB。
   */
  expScope?: 'avg_first'
  /**
   * 只要元信息，不要 series 数据。
   *
   * 用于「先看菜单再点菜」：稳态回转 7~14 条曲线时，
   * 实测 357MB → 205KB（0.03s），所以曲线选择区可以立刻渲染出来，
   * 用户勾完再按需取真正要画的那几条。
   */
  metaOnly?: boolean
}

/** Single vehicle × condition */
export function getVehicleData(
  vehicleId: number,
  conditionId: number,
  q: DataQuery = {},
) {
  return timed(
    http.get(`/vehicles/${vehicleId}/conditions/${conditionId}/data`, {
      params: {
        exp: q.exp?.length ? q.exp.join(',') : undefined,
        curves: q.curves?.length ? q.curves.join(',') : undefined,
        exp_scope: q.expScope,
        meta_only: q.metaOnly ? true : undefined,
      },
    }),
  )
}

/** API 2b — multi-vehicle comparison (page 1) */
export function getCompare(
  vehicleIds: number[],
  conditionId: number,
  q: DataQuery = {},
): Promise<CompareResponse> {
  return timed(
    http.get('/compare', {
      params: {
        vehicle_ids: vehicleIds.join(','),
        condition_id: conditionId,
        exp: q.exp?.length ? q.exp.join(',') : undefined,
        curves: q.curves?.length ? q.curves.join(',') : undefined,
        exp_scope: q.expScope,
        meta_only: q.metaOnly ? true : undefined,
      },
    }),
  )
}

/** API 3 — all indicators nested by hierarchy (page 2) */
export function getAllIndicators(params?: {
  platform?: string
  condition?: string
  indicator?: string
}): Promise<AllIndicators> {
  return timed(http.get('/indicators/all', { params }))
}

/** API 3 (flat + paging) — 200车 × 20工况 时页面2必须走这个 */
export function getAllIndicatorsFlat(params?: {
  platform?: string
  condition?: string
  indicator?: string
  /** true = 只统计「平均」实验；每车每工况实验次数不固定，只有这两种口径 */
  average?: boolean
  limit?: number
  offset?: number
}): Promise<IndicatorPage> {
  return timed(http.get('/indicators/all', { params: { ...params, flat: true } }))
}

/** 曲线名列表（用于按需只加载部分曲线） */
export function getCurveNames(conditionId: number): Promise<string[]> {
  return timed(http.get('/curve-names', { params: { condition_id: conditionId } }))
}

// ── 上传导入 ─────────────────────────────────────────────────────────────

/** 服务端对「这次要写什么」的汇总（预览和导入都会返回） */
export interface UploadSummary {
  files: number
  vehicles: number
  conditions: number
  experiments: number
  indicators: number
  curves: number
  points: number
  /** 识别出的车型名（来自文件夹名） */
  vehicle_names: string[]
  condition_names: string[]
  /** vehicles.csv 里有多少台车（与上面不同是正常的，见 DESIGN.md） */
  meta_vehicles: number
}

export interface UploadImported {
  files: number
  experiments: number
  indicators: number
  curves: number
  points: number
  platforms: number
  brands: number
  vehicles: number
  conditions: number
  seconds: number
}

export interface UploadResult {
  ok: boolean
  dry_run: boolean
  mode: 'replace' | 'append'
  received: { files: number; bytes: number }
  summary: UploadSummary
  vehicles_csv: string | null
  conditions_csv: string | null
  /** dry_run 时为 null */
  imported: UploadImported | null
  curve_meta_fixed: number
  errors: { file: string; error: string }[]
  error_count: number
  skipped: string[]
  skipped_count: number
}

export interface UploadOptions {
  /** 与 files 顺序一一对应的相对路径（'车型/工况.json'） */
  paths: string[]
  /** replace = 先清空再导入；append = 按「车 × 工况」覆盖 */
  mode: 'replace' | 'append'
  /** true = 只解析预览，一个字都不写库 */
  dryRun?: boolean
}

/**
 * 上传数据文件夹并导入（`POST /api/upload`，multipart）。
 *
 * **为什么路径要单独传**：浏览器 multipart 只会带上 `filename`（文件名本身），
 * `webkitRelativePath`（`车型/工况.json`）**不会**进请求体。不把路径一起发过去，
 * 服务端就只能拿到一堆光秃秃的 `角阶跃.json`，没法知道它属于哪台车。
 *
 * `timeout: 0`（不超时）：几百 MB 的数据 + 服务端解析入库可能要好几分钟，
 * 沿用 axios 实例上那个 5 分钟的超时会中途掐断。
 */
export function uploadData(
  files: File[],
  opts: UploadOptions,
  onProgress?: (loaded: number, total: number) => void,
): Promise<UploadResult> {
  const form = new FormData()
  for (const f of files) form.append('files', f, f.name)
  form.append('paths', JSON.stringify(opts.paths))
  form.append('mode', opts.mode)
  form.append('dry_run', opts.dryRun ? 'true' : 'false')

  return timed(
    http.post('/upload', form, {
      timeout: 0,
      onUploadProgress: (e) => {
        onProgress?.(e.loaded, e.total ?? 0)
      },
    }),
  )
}

// ── 流式对比（NDJSON）────────────────────────────────────────────────────

/** 流式端点第一行：车辆 + 指标 + 曲线元信息（不含 series） */
export interface StreamMeta extends CompareResponse {
  type: 'meta'
}

/** 流式端点每来一条曲线就是一行 */
export interface StreamCurveEvent {
  type: 'curve'
  vehicle_id: number | null
  experiment_id: number
  /** 'cal' | 'duiqi' */
  group: string
  curve: Curve
}

export interface StreamDoneEvent {
  type: 'done'
  curves: number
  points: number
  /** 服务端从开始生成到结束的耗时（ms）。流式响应写不了响应头，只能放这 */
  ms: number
}

export interface StreamQuery {
  exp?: string[]
  curves?: string[]
  expScope?: 'avg_first'
}

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onCurve?: (ev: StreamCurveEvent) => void
  onDone?: (ev: StreamDoneEvent) => void
}

/**
 * 流式拉多车对比数据（`/api/compare/stream`，NDJSON）。
 *
 * 为什么不用 `getCompare`：那个端点要先把**整个响应**序列化成一个字符串，
 * 几百 MB 的 body 在浏览器里还得整体 `JSON.parse` 一次，那个字符串本身
 * 也要占几百 MB 内存。这里逐行 parse，且**第一行 meta 到手就能渲染
 * 表格和曲线选择区**，不用等最后一条曲线。
 *
 * 服务端侧同理：实测 50 车 × 7 条 1 万点曲线，RSS 峰值增量
 * 794 MB → 11.6 MB（见 `scripts/verify_stream.py`）。
 *
 * @param signal 传入 AbortSignal 可在切工况时主动掐断在途的流
 */
export async function streamCompare(
  vehicleIds: number[],
  conditionId: number,
  q: StreamQuery = {},
  h: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<void> {
  const params = new URLSearchParams()
  params.set('vehicle_ids', vehicleIds.join(','))
  params.set('condition_id', String(conditionId))
  if (q.exp?.length) params.set('exp', q.exp.join(','))
  if (q.curves?.length) params.set('curves', q.curves.join(','))
  if (q.expScope) params.set('exp_scope', q.expScope)

  const t0 = performance.now()
  const res = await fetch(`/api/compare/stream?${params}`, { signal })

  if (!res.ok) {
    // 准入检查（400）发生**在开始流之前**，所以是一个干净的 JSON 响应
    const text = await res.text().catch(() => '')
    let msg = `HTTP ${res.status}`
    try {
      const j = JSON.parse(text)
      if (j?.detail) msg = j.detail
    } catch {
      /* 保持默认信息 */
    }
    throw new Error(msg)
  }
  if (!res.body) throw new Error('当前浏览器不支持流式响应')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let serverMs: number | null = null

  const dispatch = (line: string) => {
    const ev = JSON.parse(line)
    switch (ev.type) {
      case 'meta':
        h.onMeta?.(ev)
        break
      case 'curve':
        h.onCurve?.(ev)
        break
      case 'done':
        serverMs = ev.ms ?? null
        h.onDone?.(ev)
        break
      case 'error':
        // HTTP 200 已经发出去了，服务端只能把错误当成一行数据发回来
        throw new Error(ev.detail || '流式加载失败')
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let nl = buf.indexOf('\n')
    while (nl >= 0) {
      const line = buf.slice(0, nl)
      buf = buf.slice(nl + 1)
      nl = buf.indexOf('\n')
      if (line.trim()) dispatch(line)
    }
  }
  if (buf.trim()) dispatch(buf) // 最后一行可能没有换行符

  // 流式的服务端耗时只能从 done 行拿（响应头在 body 之前就发了）
  lastTiming = {
    serverMs,
    networkMs: performance.now() - t0,
    parseMs: performance.now() - t0,
  }
}
