/** Shared TypeScript types mirroring the FastAPI responses (metadata-driven API). */

// ── 配置层 ─────────────────────────────────────────────────────────────

/** 车辆字段注册表的一项。新增字段只加配置，不改表结构。 */
export interface AttrDef {
  id: number
  /** 程序引用的稳定键，如 brand / platform */
  code: string
  /** 展示名，如 品牌 / 平台 */
  name: string
  /** text / number / bool / date / dim */
  value_type: string
  /** 仅 value_type='dim'：指向 dim_entries.dim_code */
  dim_code: string | null
  unit: string | null
  required: boolean
  show_in_list: boolean
  /** 非维度字段的候选值（前端直接渲染下拉） */
  options: unknown[] | null
  sort_order: number
}

/** 维度字典节点（品牌→子品牌、平台→子平台等，可任意层级）。 */
export interface DimEntry {
  id: number
  dim_code: string
  name: string
  parent_id: number | null
  path: string
  sort_order: number
}

/** 导航树的一层配置。source 决定这一层的数据来源。 */
export interface TreeLevel {
  source: 'attr' | 'vehicle' | 'test_mode' | string
  /** source='attr' 时引用 attr_defs.code */
  key?: string | null
  label?: string | null
  /** 'skip' = 该车没有此字段时跳过这一层 */
  missing?: string | null
  empty_label?: string | null
}

export interface TreeProfile {
  code: string
  name: string
  levels: TreeLevel[]
  is_default: boolean
}

/** 一种试验数据格式（操稳 / 平顺 / 未来的新格式）。 */
export interface DatasetType {
  code: string
  name: string
  description: string | null
}

export interface Condition {
  id: number
  name: string
  description: string | null
  category: string | null
}

/** GET /api/meta — 前端渲染所需的全部配置。 */
export interface Meta {
  attrs: AttrDef[]
  dims: Record<string, DimEntry[]>
  tree_profiles: TreeProfile[]
  dataset_types: DatasetType[]
  conditions: Condition[]
  mode_suggestions: string[]
}

// ── 导航树 ─────────────────────────────────────────────────────────────

/** 分析单元 = 车 × 试验模式。树叶子、对比、取数都以它为单位。 */
export interface SegmentRef {
  vehicle_id: number
  vehicle_name: string
  mode_id: number | null
  mode: string | null
}

/** 导航树节点。层级数量 / 顺序 / 来源完全由 tree_profiles.levels 决定。 */
export interface NavNode {
  key: string
  label: string
  kind: 'attr' | 'vehicle' | 'test_mode' | 'segment' | string
  level: number
  count: number
  sort_order: number
  children: NavNode[]
  /** 仅 kind='attr' */
  code?: string | null
  value_id?: number | null
  /** 仅 kind='segment' */
  segment?: SegmentRef
  vehicle_id?: number
  vehicle_name?: string
  mode_id?: number | null
  mode?: string | null
  /** 该车全部动态属性的扁平值（code → value） */
  attrs?: Record<string, unknown>
}

export interface NavTree {
  profile: TreeProfile | null
  levels: TreeLevel[]
  nodes: NavNode[]
}

// ── 车辆 ───────────────────────────────────────────────────────────────

export interface VehicleMode {
  id: number
  mode: string
  sort_order: number
}

export interface TestOrder {
  id: number
  discipline: string
  order_no: string
}

/** 车辆扁平列表：attrs 是 code → 值。 */
export interface VehicleBrief {
  id: number
  name: string
  attrs: Record<string, unknown>
  modes: VehicleMode[]
  dataset_count: number
}

/**
 * 车辆详情里的一项属性。
 * 比扁平列表多带 name / unit / value_id，方便直接展示。
 */
export interface AttrValue {
  code: string
  name: string
  value: unknown
  value_id: number | null
  unit: string | null
  value_type: string
  sort_order: number
}

export interface VehicleDetail {
  id: number
  name: string
  attrs: Record<string, AttrValue>
  modes: VehicleMode[]
  test_orders: TestOrder[]
  dataset_count: number
}

export interface VehicleIn {
  name: string
  attrs: Record<string, unknown>
  modes: string[]
  test_orders: (string | { discipline: string; order_no: string })[]
}

// ── 数据 ───────────────────────────────────────────────────────────────

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
 * ⚠️ `curvename` 是数据驱动的：不要假设只有「原始数据 / 滤波」两种，
 * UI 一律从接口返回的 series 动态生成，不参与选择。
 */
export interface SeriesItem {
  x: number[]
  y: number[]
  curvetype?: string
  curvename?: string
}

export interface SeriesMeta {
  index: number
  point_count: number
}

export interface Curve {
  name: string
  title: string | null
  xlabel: string | null
  ylabel: string | null
  /** 原始采样点数 */
  point_count?: number
  /** 该曲线实际存在的数据种类，按存储顺序 */
  series_names?: string[]
  /** { 数据种类: { index, point_count } } */
  series_map?: Record<string, SeriesMeta>
  series: SeriesItem[]
}

/**
 * 一条数据集（≈ 一次实验）。
 *
 * `curves` 的 key 是数据驱动的分组（cal / duiqi / 以后的新分组），
 * 不写死。
 */
export interface Experiment {
  dataset_id: number
  /** '1' | '2' | '平均'（次数不固定） */
  exp_no: string
  is_average: boolean
  name: string
  dataset_type: string | null
  payload: Record<string, unknown> | null
  params: Record<string, Indicator>
  curves: Record<string, Record<string, Curve>>
}

/** 对比结果里的一个分析单元（车 × 模式）。 */
export interface CompareSegment {
  vehicle_id: number
  vehicle_name: string
  mode_id: number | null
  mode: string | null
  /** code → 属性（含展示名与单位） */
  attrs: Record<string, AttrValue>
  experiments: Experiment[]
}

/** 曲线名 + 该曲线的元信息（不含 series 本体） */
export interface CurveMeta {
  name: string
  title: string | null
  xlabel: string | null
  ylabel: string | null
  point_count: number
  series_names?: string[]
}

export interface CurveMetaGroup {
  /** 该组下所有曲线出现过的数据种类并集 */
  series_names: string[]
  curves: Record<string, CurveMeta>
}

export interface CurveMetaResponse {
  groups: Record<string, CurveMetaGroup>
}

export interface CompareResponse {
  condition_id: number
  condition_name: string
  dataset_type: string | null
  vehicles: CompareSegment[]
  curve_meta?: CurveMetaResponse
}

/** GET /api/segments/{vehicle_id}/conditions/{condition_id}/data */
export interface SegmentData extends CompareSegment {
  condition_id: number
  condition_name: string
  dataset_type: string | null
}

/** Flattened indicator value inside /api/indicators/all */
export interface IndicatorFlat {
  value: number | null
  text_value: string | null
  unit: string | null
  name: string
}

/** One row of the paginated indicator table. */
export interface StatsRow {
  vehicle_id: number
  vehicle: string
  mode_id: number | null
  mode: string | null
  condition: string
  dataset_id: number
  exp_no: string
  is_average: boolean
  attrs: Record<string, AttrValue>
  indicators: Record<string, IndicatorFlat>
}

export interface StatsPage {
  total: number
  offset: number
  limit: number
  rows: StatsRow[]
}
