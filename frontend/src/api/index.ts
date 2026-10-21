import axios from 'axios'
import type {
  CompareResponse,
  Condition,
  Curve,
  Meta,
  NavTree,
  SegmentData,
  StatsPage,
  VehicleBrief,
  VehicleDetail,
  VehicleIn,
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

// ── 配置 ───────────────────────────────────────────────────────────────

/** GET /api/meta — 字段注册表、维度、树配置、数据格式、工况、模式建议 */
export function getMeta(): Promise<Meta> {
  return timed(http.get('/meta'))
}

// ── 导航树 ─────────────────────────────────────────────────────────────

/**
 * GET /api/tree
 *
 * 层级数量 / 顺序 / 来源全部由 `profile` 指向的 tree_profiles 决定。
 * `filters` 的 key 是 attr_defs.code，任意字段都能过滤，不需要新接口。
 */
export function getTree(
  profile?: string,
  filters?: Record<string, string>,
): Promise<NavTree> {
  const params: Record<string, string> = {}
  if (profile) params.profile = profile
  for (const [code, value] of Object.entries(filters ?? {})) {
    if (value !== '') params[`f_${code}`] = value
  }
  return timed(http.get('/tree', { params }))
}

// ── 车辆 ───────────────────────────────────────────────────────────────

export function getVehicles(filters?: Record<string, string>): Promise<VehicleBrief[]> {
  const params: Record<string, string> = {}
  for (const [code, value] of Object.entries(filters ?? {})) {
    if (value !== '') params[`f_${code}`] = value
  }
  return timed(http.get('/vehicles', { params }))
}

export function getVehicle(id: number): Promise<VehicleDetail> {
  return timed(http.get(`/vehicles/${id}`))
}

export function createVehicle(payload: VehicleIn): Promise<VehicleDetail> {
  return timed(http.post('/vehicles', payload))
}

export function updateVehicle(
  id: number,
  payload: Partial<VehicleIn>,
): Promise<VehicleDetail> {
  return timed(http.put(`/vehicles/${id}`, payload))
}

export function deleteVehicle(id: number): Promise<{ ok: boolean }> {
  return timed(http.delete(`/vehicles/${id}`))
}

// ── 工况 / 曲线名 / 指标名 ────────────────────────────────────────────

/** All conditions (工况)。可按车 / 模式 / 数据格式收敛。 */
export function getConditions(params?: {
  vehicle_id?: number
  mode_id?: number
  dataset_type?: string
}): Promise<Condition[]> {
  return timed(http.get('/conditions', { params }))
}

/** 曲线名列表（用于按需只加载部分曲线） */
export function getCurveNames(
  conditionId: number,
  datasetType?: string,
): Promise<string[]> {
  return timed(
    http.get('/curve-names', {
      params: { condition_id: conditionId, dataset_type: datasetType },
    }),
  )
}

export function getIndicatorNames(
  conditionId: number,
  datasetType?: string,
): Promise<string[]> {
  return timed(
    http.get('/indicator-names', {
      params: { condition_id: conditionId, dataset_type: datasetType },
    }),
  )
}

// ── 数据查询 ───────────────────────────────────────────────────────────

/** 只取指定实验号 / 曲线，用来收敛大工况（10万点）的响应体积 */
export interface DataQuery {
  exp?: string[] // ['平均'] / ['1', '2'] — 不传 = 全部
  curves?: string[] // 不传 = 全部曲线
  /**
   * 实验口径。`'avg_first'` = 每个 segment 只取「平均」（没有平均则取第一次）。
   *
   * 前端默认视图（曲线只画平均、指标表只显示平均列）要的就是这一次实验，
   * 全量拉下来有 2/3 是白拉。
   */
  expScope?: 'avg_first'
  /**
   * 只要元信息，不要 series 数据。
   *
   * 用于「先看菜单再点菜」：曲线选择区立刻渲染，用户勾完再按需取真正要画的。
   */
  metaOnly?: boolean
  datasetType?: string
}

/** Single segment (车 × 模式) × condition */
export function getSegmentData(
  vehicleId: number,
  conditionId: number,
  modeId: number | null,
  q: DataQuery = {},
): Promise<SegmentData> {
  return timed(
    http.get(`/segments/${vehicleId}/conditions/${conditionId}/data`, {
      params: {
        mode_id: modeId ?? undefined,
        dataset_type: q.datasetType,
        exp: q.exp?.length ? q.exp.join(',') : undefined,
        curves: q.curves?.length ? q.curves.join(',') : undefined,
        exp_scope: q.expScope,
        meta_only: q.metaOnly ? true : undefined,
      },
    }),
  )
}

/**
 * segment 的传输格式：`vehicle_id:mode_id`，多个用逗号分隔。
 * mode_id 为空写成 `vehicle_id:`（服务端解析成 None）。
 */
export function segmentKey(vehicleId: number, modeId: number | null): string {
  return `${vehicleId}:${modeId ?? ''}`
}

/** Multi-segment comparison (meta + series) */
export function getCompare(
  segments: string[],
  conditionId: number,
  q: DataQuery = {},
): Promise<CompareResponse> {
  return timed(
    http.get('/compare', {
      params: {
        segments: segments.join(','),
        condition_id: conditionId,
        dataset_type: q.datasetType,
        exp: q.exp?.length ? q.exp.join(',') : undefined,
        curves: q.curves?.length ? q.curves.join(',') : undefined,
        exp_scope: q.expScope,
        meta_only: q.metaOnly ? true : undefined,
      },
    }),
  )
}

// ── 指标统计 ───────────────────────────────────────────────────────────

export interface IndicatorsQuery {
  /** 任意字段过滤：key 是 attr_defs.code */
  filters?: Record<string, string>
  condition?: string
  /** 工况 ID（优先于名字，避免把中文放进 URL query） */
  condition_id?: number
  indicator?: string
  average?: boolean
  dataset_type?: string
  mode_id?: number
  limit?: number
  offset?: number
}

/**
 * 指标统计。
 *
 * 工况名是中文（角阶跃 / 稳态回转），部分反向代理会把百分号编码解码后再转发，
 * 导致 uvicorn 在 HTTP 层直接 400。所以**默认走 POST + JSON body**，
 * 把所有可能含中文的参数从 URL 剥离出去。
 */
export function getAllIndicatorsFlat(params?: IndicatorsQuery): Promise<StatsPage> {
  const body: Record<string, unknown> = {}
  if (params?.filters) body.filters = params.filters
  if (params?.condition_id !== undefined) body.condition_id = params.condition_id
  if (params?.condition) body.condition = params.condition
  if (params?.indicator) body.indicator = params.indicator
  if (params?.average !== undefined) body.average = params.average
  if (params?.dataset_type) body.dataset_type = params.dataset_type
  if (params?.mode_id !== undefined) body.mode_id = params.mode_id
  if (params?.limit !== undefined) body.limit = params.limit
  if (params?.offset !== undefined) body.offset = params.offset
  return timed(http.post('/indicators/all', body))
}

// ── 上传导入 ─────────────────────────────────────────────────────────────

/** 服务端对「这次要写什么」的汇总（预览和导入都会返回） */
export interface UploadSummary {
  files: number
  vehicles: number
  conditions: number
  modes: number
  dataset_types: number
  datasets: number
  indicators: number
  curves: number
  points: number
  /** 识别出的车型名（来自文件夹名） */
  vehicle_names: string[]
  condition_names: string[]
  mode_names: string[]
  /** vehicles.csv 里有多少台车 */
  meta_vehicles: number
}

export interface UploadImported {
  files: number
  datasets: number
  experiments: number
  indicators: number
  curves: number
  points: number
  modes: number
  vehicles: number
  conditions: number
  dataset_types: number
  seconds: number
}

export interface UploadResult {
  ok: boolean
  dry_run: boolean
  mode: 'replace' | 'append'
  dataset_type: string
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
  /** 文件名不像工况名、靠内容里的指标名改判成功的 */
  inferred_conditions: { file: string; filename: string; condition: string }[]
  inferred_count: number
  /** 文件名和内容都定不下来，只能拿文件名当工况的 */
  unresolved_conditions: { file: string; filename: string; candidates: string[] }[]
}

export interface UploadOptions {
  /** 与 files 顺序一一对应的相对路径（'车型/[模式/]工况.json'） */
  paths: string[]
  /** replace = 先清空再导入；append = 按 slot 覆盖 */
  mode: 'replace' | 'append'
  /** true = 只解析预览，一个字都不写库 */
  dryRun?: boolean
  /** 本次数据按哪种格式解析（dataset_types.code） */
  datasetType?: string
}

/**
 * 上传数据文件夹并导入（`POST /api/upload`，multipart）。
 *
 * **为什么路径要单独传**：浏览器 multipart 只会带上 `filename`（文件名本身），
 * `webkitRelativePath`（`车型/工况.json`）**不会**进请求体。不把路径一起发过去，
 * 服务端就只能拿到一堆光秃秃的 `角阶跃.json`，没法知道它属于哪台车。
 *
 * `timeout: 0`（不超时）：几百 MB 的数据 + 服务端解析入库可能要好几分钟。
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
  if (opts.datasetType) form.append('dataset_type', opts.datasetType)

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
  mode_id: number | null
  dataset_id: number
  /** 'cal' | 'duiqi' | 数据驱动的其它分组 */
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
  datasetType?: string
}

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onCurve?: (ev: StreamCurveEvent) => void
  onDone?: (ev: StreamDoneEvent) => void
}

/**
 * 流式拉多 segment 对比数据（`/api/compare/stream`，NDJSON，**POST**）。
 *
 * 曲线名可能是中文（如「方向盘转角-时间」）。放进 URL query 会被某些代理
 * 解码成原始字节，uvicorn/h11 直接 400。所以改用 fetch + body POST，
 * 并手动读取 NDJSON 流（axios 不支持流式 response body）。
 *
 * 第一行 meta 到手就能渲染表格和曲线选择区，不用等最后一条曲线。
 *
 * @param signal 传入 AbortSignal 可在切工况时主动掐断在途的流
 */
export async function streamCompare(
  segments: string[],
  conditionId: number,
  q: StreamQuery = {},
  h: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<void> {
  const t0 = performance.now()
  const res = await fetch('/api/compare/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      segments,
      condition_id: conditionId,
      dataset_type: q.datasetType ?? null,
      exp: q.exp ?? null,
      curves: q.curves ?? null,
      exp_scope: q.expScope ?? null,
    }),
    signal,
  })

  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let msg = `HTTP ${res.status}`
    try {
      const j = JSON.parse(text)
      if (j?.detail) msg = j.detail
      else if (j?.message) msg = j.message
    } catch {
      if (text) msg = `${msg}: ${text.slice(0, 120)}`
    }
    throw new Error(msg)
  }
  if (!res.body) throw new Error('当前浏览器不支持流式响应')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let serverMs: number | null = null
  let sawMeta = false

  const dispatch = (line: string) => {
    const ev = JSON.parse(line)
    switch (ev.type) {
      case 'meta':
        sawMeta = true
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
  if (!sawMeta) throw new Error('对比接口没有返回元信息')

  // 流式的服务端耗时只能从 done 行拿（响应头在 body 之前就发了）
  lastTiming = {
    serverMs,
    networkMs: performance.now() - t0,
    parseMs: performance.now() - t0,
  }
}
