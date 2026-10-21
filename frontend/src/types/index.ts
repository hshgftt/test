/** Shared TypeScript types mirroring the FastAPI responses. */

export interface VehicleBrief {
  id: number
  name: string
}

/** /api/tree → { platform: { brand: [{ id, name }] } } */
export type TreeResponse = Record<string, Record<string, VehicleBrief[]>>

export interface Indicator {
  name: string
  unit: string | null
  raw_key: string
  value: number | null
  text_value: string | null
}

/**
 * 曲线里的一条数据线。
 *
 * ⚠️ `curvename` 是**数据驱动**的：不要假设只有「原始数据 / 滤波」两种，
 * 也可能更多、更少，或者叫别的名字。UI 一律从接口返回的
 * `series_names` / `series_map` 动态生成选项。
 */
export interface SeriesItem {
  x: number[]
  y: number[]
  curvetype?: string
  curvename?: string
}

/** 某种数据种类在某条曲线里的位置与规模 */
export interface SeriesMeta {
  index: number
  point_count: number
}

export interface Curve {
  name: string
  title: string | null
  xlabel: string | null
  ylabel: string | null
  /** 原始采样点数（后端冗余字段，前端据此提示渲染量级） */
  point_count?: number
  /** 该曲线实际存在的数据种类，按存储顺序（如 ['原始数据','滤波']） */
  series_names?: string[]
  /** { 数据种类: { index, point_count } } */
  series_map?: Record<string, SeriesMeta>
  series: SeriesItem[]
}

export interface Experiment {
  experiment_id: number
  experiment_no: string // '1' | '2' | '平均'（次数不固定）
  is_average: boolean
  params: Record<string, Indicator>
  curves_cal: Record<string, Curve>
  curves_duiqi: Record<string, Curve>
}

export interface CompareVehicle {
  vehicle_id: number
  vehicle_name: string
  brand_name: string | null
  platform_name: string | null
  drive_type: string | null
  experiments: Experiment[]
}

/** 曲线名 + 该曲线的元信息（不含 series 本体） */
export interface CurveMeta {
  name: string
  title: string | null
  xlabel: string | null
  ylabel: string | null
  point_count: number
  /**
   * 该曲线有哪些数据种类。
   *
   * ⚠️ `meta_only=1`（「先看菜单」那次请求）拿不到它 —— 数据种类存在 series
   * 里，而 meta_only 刻意不读 series。要用的话得等第二阶段的数据到齐。
   */
  series_names?: string[]
}

export interface CurveMetaGroup {
  /** 该组下所有曲线出现过的数据种类并集 */
  series_names: string[]
  curves: Record<string, CurveMeta>
}

/** 后端 aggregate 出来的曲线元信息，前端据此渲染曲线面板 */
export interface CurveMetaResponse {
  groups: Record<string, CurveMetaGroup>
}

export interface CompareResponse {
  condition_id: number
  condition_name: string
  vehicles: CompareVehicle[]
  curve_meta?: CurveMetaResponse
}

export interface Condition {
  id: number
  name: string
  description: string | null
  category: string | null
}

/** Flattened indicator value inside /api/indicators/all */
export interface IndicatorFlat {
  value: number | null
  text_value: string | null
  unit: string | null
  name: string
}

/**
 * /api/indicators/all
 * platform → brand → vehicle → condition → experiment_no → { raw_key: IndicatorFlat }
 */
export type AllIndicators = Record<
  string,
  Record<string, Record<string, Record<string, Record<string, Record<string, IndicatorFlat>>>>>
>

/** One row of the flattened stats table. */
export interface StatsRow {
  platform: string
  brand: string
  vehicle: string
  condition: string
  experiment: string
  [rawKey: string]: unknown
}

/** /api/indicators/all?flat=true → 分页扁平响应（大数据量用这个） */
export interface IndicatorPage {
  total: number
  offset: number
  limit: number
  rows: {
    platform: string
    brand: string
    vehicle: string
    condition: string
    experiment_no: string
    is_average?: boolean
    indicators: Record<string, IndicatorFlat>
  }[]
}
