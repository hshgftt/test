# 整车试验数据平台 — 设计思路与操作手册

> 面向：接手这个项目的人（包括三个月后的你自己）。
> 读完应该能独立跑起来、导数据、改结构。

**一句话**：车辆字段、导航树层级、试验数据格式，全部由数据库里的**配置**决定；
新增字段 / 新增层级 / 新增格式都只加数据，不改表结构、不大改代码。

---

## 目录

1. [这个项目解决什么问题](#1-这个项目解决什么问题)
2. [设计原则：为什么是这样](#2-设计原则为什么是这样)
   - 2.1 车辆字段 → 字段注册表 + EAV
   - 2.2 维度字典 → 同一维度内部可多层
   - 2.3 导航树 → 运行时投影，只存层级配置
   - 2.4 分析单元 segment → (车, 模式)
   - 2.5 模式与单号 → 属于试验侧，数量不固定
   - 2.6 数据格式 → datasets 统一信封
   - 2.7 指标与曲线 → 查询性能不能退化
   - 2.8 实验号是字符串
   - 2.9 过滤下沉到 SQL + 真分页
   - 2.10 大数据量 → 按实际负载治理 + NDJSON 流式
   - 2.11 中文参数 → 全部走 POST body
   - 2.12 前端渲染 → 降采样 / Float64Array / shallowRef
3. [数据模型](#3-数据模型)
4. [API 一览](#4-api-一览)
5. [环境准备](#5-环境准备)
6. [操作 A：生成演示数据](#6-操作-a生成演示数据)
7. [操作 B：导入真实数据](#7-操作-b导入真实数据)
8. [日常运行](#8-日常运行)
9. [性能与耗时观测](#9-性能与耗时观测)
10. [常见问题排查](#10-常见问题排查)
11. [扩展指南](#11-扩展指南)

---

## 1. 这个项目解决什么问题

整车操稳试验（角阶跃、角脉冲、稳态回转等）会产生大量结构化结果：

- **指标**（标量）：每台车每个工况每次实验，约 10~30 个数值
- **曲线**（向量）：每台车每个工况每次实验，约 7~10 条曲线，每条 500 ~ 10 万个采样点

规模目标是 **200 台车 × 20 个工况 × 多次实验**，全量曲线数据可达 **40 GB 量级**。

平台要做的事：

1. 把算法侧产出的 dict/文件**原样收进来**，不丢信息
2. 支持**多车对比**：同一工况下横向比指标、叠曲线
3. 支持**全量指标检索**：跨车、跨工况查某个指标
4. **字段、层级、数据格式都能随时扩展，不用改表结构、不用大改代码**

而真实世界里，前三条都不难，难的是第四条：

- 车辆基本信息字段会持续增删（今天有「销售地」，明天要加「整备质量」）
- 导航目录不能固定成「平台 → 品牌 → 车」三层，也可能要「品牌 → 平台」、要子品牌、要把试验模式也放进去
- 试验数据格式会扩展（操稳、平顺、以后可能还有别的）
- 一台车可能有多个操稳单号，也可能没有单号
- 不同车辆拥有的试验模式数量不一样（有的没模式，有的只有「舒适」，有的「舒适 + 运动」）

下面所有设计都是围绕这几点做的。

---

## 2. 设计原则：为什么是这样

### 2.1 车辆字段 → 字段注册表 + EAV

**需求：车辆基本信息字段未来持续增删，不能硬编码固定表结构。**

如果给每个字段建列：

```sql
vehicles(id, name, brand, sub_brand, platform, sub_platform,
         vehicle_type, production_date, vin, front_suspension, ...)
```

每加一个字段就要 `ALTER TABLE`；字段一多，表变成几十列的宽表，读写都难受。

所以走**字段注册表 + EAV**：

```
attr_defs              一个字段一行（code / name / value_type / dim_code / options …）
vehicle_attr_values    一台车 × 一个字段一行（value_text / value_num / value_dim_id）
```

新增字段 = 在 `attr_defs` 插一行，**零 migration**；字段删除 = 停用（`is_active=false`），历史值不受影响。

`value_type` 决定值落到哪一列：

| value_type | 落库 | 说明 |
|---|---|---|
| `text` | `value_text` | 普通字符串 |
| `number` | `value_num`（同时回填 `value_text`） | 数值，可排序/筛选 |
| `bool` | `value_num`（0/1） | 布尔 |
| `date` | `value_text` | ISO 字符串 |
| `dim` | `value_dim_id` | 引用维度字典，见 2.2 |

`options` 是非维度字段的枚举候选（如「能源形式」），前端直接渲染下拉。

> CSV 里出现注册表没有的列时，导入器会**自动注册**成新字段
> （`importer._register_attr`），连「提前加字段」这一步都省了。

### 2.2 维度字典 → 同一维度内部可多层

品牌有「品牌 → 子品牌」，平台有「平台 → 子平台」。这类值不适合当自由文本，
否则「星河」和「星河汽车」会变成两个品牌。

`dim_entries` 一张表同时表达**维度**和**维度内的层级**：

```
dim_entries(id, dim_code, name, parent_id, path, sort_order)
              品牌          子品牌      /1/7/
```

- `dim_code` 区分不同维度（`brand` / `platform` …）
- `parent_id` 表达同一维度内部的父子级
- `path` 是物化路径，便于一次查出祖先链

**车辆只引用最深节点**。比如只填了「子品牌」，读属性时沿 `parent_id` 向上补齐
「品牌」层，导航树照样能按品牌分组。

哪些字段属于哪个维度、层级顺序是什么，由 `attr_defs` 决定：

| code | dim_code | sort_order |
|---|---|---|
| `brand` | `brand` | 10 |
| `sub_brand` | `brand` | 11 |
| `platform` | `platform` | 20 |
| `sub_platform` | `platform` | 21 |

同一 `dim_code` 下按 `sort_order` 排序即层级顺序（0 开始）。这是一条**配置约定**，
不需要额外的表字段，也不需要改代码。

### 2.3 导航树 → 运行时投影，只存层级配置

**需求：目录树不能固定 3 层，未来要任意层级、任意顺序、任意字段组合。**

所以**不建树节点表，也不存父子关系**。`tree_profiles.levels` 只声明「用哪些层级、
什么顺序」，树在每次请求时按车辆实际数据**投影**出来：

```json
[
  {"source": "attr",      "key": "platform",  "missing": "skip"},
  {"source": "attr",      "key": "sub_platform", "missing": "skip"},
  {"source": "attr",      "key": "brand",     "missing": "skip"},
  {"source": "attr",      "key": "sub_brand", "missing": "skip"},
  {"source": "vehicle",   "label": "车名"},
  {"source": "test_mode", "label": "试验模式",
   "missing": "label", "empty_label": "未分模式"}
]
```

`source` 只有三种：

| source | 取值来自 | 说明 |
|---|---|---|
| `attr` | `attr_defs.code` | 任意车辆字段，包括以后新加的 |
| `vehicle` | `vehicles.name` | 车名 |
| `test_mode` | `vehicle_modes.mode` | 该车拥有的试验模式 |

`missing` 决定某台车没有这个字段时的行为：

- `skip`：跳过这一层，不生成空节点（子品牌、子平台这类可选字段用这个）
- `label`：生成一个占位节点（`empty_label`），常用于「未分模式」

改层级 = 改一行 JSON（`PUT /api/tree-profiles/{code}`），**不用改代码、不用迁数据**。
默认内置三套配置（`default` / `brand_first` / `flat`），前端可在 `meta.tree_profiles` 里切换。

树的叶子不是车，而是 **segment**（见 2.4），所以「同一台车两个模式」自然成为两个可选项。

### 2.4 分析单元 segment → (车, 模式)

`segment = (vehicle_id, mode_id)`，是导航树叶子、对比、取数、统计的基本单位。

- `mode_id` 可以为空：该车没有模式，或数据本身不区分模式
- API 里写作 `vehicle_id:mode_id`，如 `1:2`；无模式写 `1`（省略冒号）

**为什么不用 vehicle_id 直接做单位**：同一台车的「舒适」和「运动」是两组独立数据，
如果按车选，就必须在同一个响应里塞两套曲线，前端还得自己区分。用 segment 之后，
勾选、对比、图例命名都以「车 · 模式」为单位，逻辑自然。

### 2.5 模式与单号 → 属于试验侧，数量不固定

**试验模式**（舒适 / 运动 / …）不是车辆的固定属性，而是试验侧的维度：

```
vehicle_modes(id, vehicle_id, mode, sort_order)   -- (vehicle_id, mode) 唯一
```

不同车辆的模式数量不固定，所以按车建行，不做全局字典。目录结构里
`<车名>/<模式>/<工况>.json` 的中间目录就是模式（可以多层，按 `/` 拼接）。

**试验单号**（操稳单号 / 平顺单号）一台车可以有多个，也可能缺失：

```
test_orders(id, vehicle_id, discipline, order_no)  -- (车, 学科, 单号) 唯一
datasets.test_order_id 可空
```

- `discipline` 已预留 `操稳` / `平顺`，平顺数据暂不设计，后续补表不需要改这里
- 文件夹名可以带单号：`极光EV-01(CS-2024-001)`，括号里的内容记为操稳单号
- CSV 也可以写 `操稳单号` / `平顺单号`，多个单号用 `; , | 、` 分隔

单号是**试验属性**，不是车辆唯一属性，所以绝不放在 `vehicles` 表上。

车名**全局唯一**（`vehicles.name` unique），不会跨品牌重复。

### 2.6 数据格式 → datasets 统一信封

**需求：试验数据格式未来会扩展，不能只支持「指标 + 曲线」。**

`datasets` 是统一信封：一条数据集（≈ 一次实验）一行，`dataset_type_id` 决定
这条数据怎么解释。新增格式 = 在 `dataset_types` 加一行配置：

```
handling_stability  操稳数据（指标 + 计算曲线 / 对齐曲线）
ride_comfort        平顺数据（预留）
generic             通用数据（整块放 payload）
```

数据本身**按用途拆三处**，不塞进一个大 payload：

| 内容 | 存放 | 原因 |
|---|---|---|
| 标量指标 | `metric_values` | 要跨车统计、按指标名筛选，必须能走索引 |
| 曲线 | `curves`（一条曲线一行，`series` 整块 JSONB） | 要能只取勾选的曲线、`meta_only` 完全不碰 series |
| 其余结构 | `datasets.payload`（可空） | 新格式自带特殊结构时兜底，不丢数据 |

已知格式（`handling_stability`）由 `importer.parse_payload()` 拆成指标和曲线；
**认不出的结构**不会被丢弃，整块进 `payload`（`kind='payload'`），后续再补解析器即可。

### 2.7 指标与曲线 → 查询性能不能退化

这是旧版最关键的设计，重构后**原样保留**。

**① 指标 EAV 行存储**

```
metric_values(id, dataset_id, name, unit, raw_key, value, text_value, category)
```

一行一个指标。新增指标名 = `INSERT` 一行，零 migration。
`raw_key` 原样保留，数字前缀不存（见下）。

**② 曲线整块 JSONB，但一条曲线一行**

**关键前提：曲线只会整体取回来画图，不会按 y 值查询。**
所以不拆 `points(curve_id, x, y)`——一次画图要 join 几万行。`series` 整块存：

```json
[{"x": [...], "y": [...], "curvetype": "line", "curvename": "原始数据"}]
```

**但曲线必须独立成表**，不能塞进 `datasets.payload`：

- 单条曲线可达 10 万点，一个数据集全部曲线几十万点
- 只有一条曲线一行，`curves=xxx` 过滤才能**下推到 SQL**，只加载命中的 `Curve.series`
- 只有一条曲线一行，`meta_only=1` 才能只读元信息、**完全不碰 series**
- `point_count` 冗余存储，估算负载时不用解析 JSON

如果曲线跟着 dataset 一起存，读任何一条线都要把全部线反序列化出来——
「只画 1 条」也要付 7 条的代价。这条是硬约束，改代码时不要合并回去。

**③ 数字前缀（seq）刻意不存**

算法侧的 key 形如 `'13-侧向加速度稳态值(g)'`。只拆出 `name` / `unit` 做检索，
`raw_key` 原样保留保证无损还原。数字前缀不入库，因为它：

| 问题 | 具体表现 |
|---|---|
| 可能省略 | 真实数据里会出现 `'侧向加速度稳态值(g)'` |
| 跨工况含义不同 | 同一个「车速」：角阶跃里是 `10`，角脉冲里是 `100` |
| 不唯一 | `17-横摆角速度峰值响应时间` 和 `17-横摆角速度峰值迟滞` 同为 17 |

排序统一按 `name` 字典序（中文用 `localeCompare('zh')`）。这是被真实数据三次推翻后
的结论，**不要再加回来**。

### 2.8 实验号是字符串

每台车每个工况的实验次数不固定——可能 1 次、3 次，或者没有平均。所以：

- `datasets.exp_no` 存字符串：`'1'` / `'2'` / `'平均'`
- 配 `is_average` 布尔 + `sort_order` 整数（平均 = 999）
- UI 上只有「仅平均 / 全部」两种口径，不做「勾选第一次/第二次」的集合模型

### 2.9 过滤下沉到 SQL + 真分页

分页场景下如果在前端过滤，`total` 会是错的（前端只看到当前页）。
所有过滤条件（任意车辆字段、工况、指标名、仅平均、数据格式）都在 SQL `WHERE` 里完成，
`COUNT` 和 `SELECT` 用同一套条件。

**任意车辆字段过滤**用 `f_<attr_code>` 表达，例如：

```
GET /api/tree?f_brand=星河&f_platform=BEV平台
GET /api/indicators/all?f_energy_type=纯电&condition_id=3
```

服务端不用为每个字段加查询参数，新增字段后前端筛选框自动出现（`meta.attrs` 驱动）。

分页也是**真下推**：先在数据集级 key 上 `COUNT` / `ORDER BY … LIMIT OFFSET`，
再只为这一页的数据集用一次 `IN` 查询取指标。指标名过滤用 `EXISTS` 子查询，
否则 `total` 会把没有该指标的数据集也算进去。

### 2.10 大数据量 → 按实际负载治理 + NDJSON 流式

「200 台车选稳态回转会不会崩」——实测（7 条曲线 × 1 万点，每条 2 个 series）：

| 场景 | gzip | 原始 JSON | 耗时 |
|---|---|---|---|
| 50 车 × 全部实验 × 全部曲线 | 176.8 MB | 357 MB | 11.5 s |
| 50 车 × 平均 × 全部曲线 | 58.0 MB | 118.7 MB | 4.2 s |
| 50 车 × 平均 × 1 条曲线 | 8.6 MB | 18.2 MB | 0.6 s |
| **200 车 × 平均 × 1 条曲线** | 34.3 MB | 72.8 MB | **2.5 s** |
| 200 车 × 平均 × 全部曲线 | 400（拦截） | — | — |

结论：**「200 台车」本身不是问题，「200 台车 × 7 条曲线」才是**。
所以准入检查卡的是**估算的采样点总数**（`MAX_COMPARE_NUMBERS`，默认 5000 万），
不是车辆数。按车辆数卡会同时误伤「200 车只画 1 条」和放过「50 车画 7 条」。

服务端序列化的内存放大系数实测 **7.95×**，5000 万个数字 ≈ 340 MB JSON ≈ 2.7 GB RSS。

#### 三个收敛手段（可叠加）

1. **`curves=`** —— 最有效。曲线名过滤**下推到 SQL**（`selectinload` 只加载命中的行），
   不是拉回来再在 Python 里筛。
2. **`exp_scope=avg_first`** —— 每个 segment 只取「平均」，没有平均则取第一次，
   用窗口函数在 SQL 里表达，省 ~2/3。
3. **`meta_only=1`** —— 只返回元信息（车辆 + 指标 + 曲线元信息），**完全不碰 `Curve.series`**。
   实测 200 车 / 稳态回转 = **811 KB / 0.1 s**（对照 357 MB，约 440×）。

#### 前端的两阶段加载

1. **菜单阶段**（`meta_only=1` + `exp_scope`）：车辆列表、指标表、曲线选择区立刻渲染
2. **点菜阶段**（`curves=` 只带勾选的）：只为用户选中的曲线取 series

勾选变化时**只补拉新勾选的**曲线（已加载过的直接用，取消勾选什么都不做）。
切口径（仅平均 ↔ 全部实验）会重新取数，并用「代际」计数器（`loadGen`）丢弃过期请求，
否则快速切工况时会把上一批的曲线点集并进这一批的骨架里。

#### 第二阶段走流式（NDJSON）

`/api/compare` 的致命处不在数据量，而在**它必须先把整个响应组装成一个 dict、
再 `json.dumps` 成一个几百 MB 的字符串**。实测（50 车 × 平均 × 7 条 1 万点曲线）：

| 路径 | 服务端 RSS 峰值增量 | 总耗时 | 首行到达 |
|---|---|---|---|
| `/api/compare` | **794.0 MB** | 6.60 s | —（要等 body 收完） |
| `/api/compare/stream` | **11.6 MB** | 3.71 s | **0.182 s** |

峰值内存差 **68×**，而且前端**第一行到手就能渲染表格和曲线选择区**。

所以前端第二阶段改走 `POST /api/compare/stream`，返回 `application/x-ndjson`，
一行一个 JSON 对象：

```
{"type":"meta",  ...}   车辆 + 指标 + 曲线元信息（不含 series）
{"type":"curve", ...}   一条曲线的 series，重复 N 次
{"type":"done",  ...}   统计 curves / points / ms
{"type":"error", ...}   生成中的异常（此时 HTTP 200 已经发出去了）
```

服务端按 `STREAM_BATCH_CURVES`（默认 24）条一批取 series，所以**峰值内存与
「选了多少条曲线」无关**。超限时返回 400 —— 那时还没开始流，是干净的 JSON 错误。

三个实现上的坑：

* **取 series 必须用 Core 查询**（`select(Curve.id, …, Curve.series)`），
  不能 `select(Curve)`。ORM 会把每条曲线对象塞进 Session 的 identity map 并一直持有。
* **流式响应写不了 `X-Server-Time-Ms`** —— 响应头在第一条数据生成之前就发出去了。
  服务端耗时放在 `done` 行的 `ms` 字段里。
* **gzip 不会把流缓冲住**（实测过）：`GZipMiddleware` 对 `StreamingResponse` 是边压边发的。

#### 流式下前端不能「收一条画一次」

`chartBlocks` 是手动维护的 `shallowRef`，不是 computed：computed 会在 `data` 任何
一处变化时给所有 block 生成新的 `lines` 数组，于是每个 `CurveChart` 都被触发重画。
现在 `applyCurveEvent` 只把数据挂上骨架并记进 `dirtyBlocks`，按 200ms 节流
`rebuildDirtyBlocks()`，**只给变过的曲线换新 `lines`**。

> `el-select` 的 multiple 模式会原地改 `v-model` 数组（引用不变），所以
> `watch(selectedCurves)` **必须加 `{ deep: true }`**，否则「勾了曲线但图不出来」。

### 2.11 中文参数 → 全部走 POST body

工况名、曲线名、品牌名都是中文。把中文放进 URL query 时，某些反向代理会把
百分号编码解码后原样转发给 uvicorn/h11，HTTP 解析器直接返回
`400 Invalid HTTP request received.`——请求根本到不了 FastAPI。

所以含中文的接口都提供 **POST + JSON body** 版本，前端只走 POST：

| 接口 | 前端用法 |
|---|---|
| `POST /api/indicators/all` | 所有筛选条件放 body |
| `POST /api/compare/stream` | `{segments, condition_id, dataset_type, exp, curves, exp_scope}` |

GET 版本仍然保留（方便 curl / Swagger 调试），但**前端不再使用**。
新增接口时记住这条：**只要参数可能是中文，就不要放 URL**。

### 2.12 前端渲染 → 降采样 / Float64Array / shallowRef

#### 曲线降采样用 LTTB

单条曲线可达 10 万点，一页叠 3~10 条就是几十万点。

先明确它省什么、不省什么：

| 环节 | 降采样有没有用 |
|---|---|
| 后端 SQL 查询 | ✗（曲线是整块 JSONB） |
| 网络传输 | ✗（降采样在浏览器里做，数据已经下载完了） |
| JSON 解析 | ✗（同上） |
| 构建点集 + ECharts 建索引 | ✓ 省一个数量级 |
| 渲染 / 内存 | ✓ |

要省传输只能靠后端的 `curves=` / `exp_scope=` / `meta_only=`。

**为什么用 LTTB 而不是等间隔抽样**：等间隔会丢掉尖峰，而峰值（超调量、
峰值响应时间）恰恰是试验曲线最要紧的信息。实测 10 万点抽到 1 万点：
LTTB 保住尖峰与首末点，耗时 2.4 ms；等间隔抽样尖峰直接丢失。

参数在 `CompareView.vue` 的常量 `DS_THRESHOLD`（默认 `10000`，`null` = 关闭）。
界面上刻意没有这个选项。

#### 大数组用交错 Float64Array

ECharts 最顺手的 `series.data = [[x, y], …]` 在 V8 里每个点要一个小数组。
实测 8 条线 × 10 万点：

| 表示 | 每点驻留 | `setOption` |
|---|---|---|
| `number[][]` | ~520 B/点 | 656 ms |
| 交错 `Float64Array` | ~40 B/点（**约 10×**） | 286 ms（2.3×） |

用 `Float64` 不用 `Float32`：后者只有 ~7 位有效数字，5 万点以上坐标会有偏差。

```ts
series: [{
  type: 'line',
  dimensions: ['x', 'y'],          // typed array 源必须显式给维度
  data: interleavedFloat64Array,   // buf[i*2] = x, buf[i*2+1] = y
}]
```

交错数组是临时的：ECharts 对 typed array 源会 `provider.clean()`，`setOption` 后即释放。
**不要缓存进 computed**，在 `render()` 里现构建、交给 ECharts 就丢。

#### `ref` → `shallowRef`

响应里有几万个 `number[]`。`ref` 会递归地把它们全包成 Proxy，建几万个代理要
几百毫秒到几秒。我们从不修改点集，所以 `data` 用 `shallowRef`，合并完曲线后
手动 `triggerRef()` 通知。`CurveChart` 里对 `props.lines` 也用浅 watch，不做 deep。

---

## 3. 数据模型

### 3.1 三层结构

```
配置层（只加数据，不改表结构）
  attr_defs       字段注册表
  dim_entries     维度字典（同一维度内可多层）
  tree_profiles   导航树层级配置
  dataset_types   数据格式注册

实例层
  vehicles            1──N vehicle_attr_values
                      1──N vehicle_modes
                      1──N test_orders
                      1──N datasets
  conditions          N──1 datasets

数据层
  datasets            1──N metric_values
                      1──N curves
```

### 3.2 表关系

```
attr_defs  1──N  vehicle_attr_values  N──1  vehicles
dim_entries (自引用 parent_id)  ────  vehicle_attr_values.value_dim_id

vehicles  1──N  vehicle_modes   ─┬─N  datasets
vehicles  1──N  test_orders     ─┘

conditions    1──N  datasets
dataset_types 1──N  datasets

datasets  1──N  metric_values
datasets  1──N  curves

tree_profiles    仅配置，无外键
```

### 3.3 各表字段

**attr_defs** — 字段注册表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| code | varchar(50) unique | 程序引用的稳定键，如 `brand` |
| name | varchar(100) | 展示名，如「品牌」 |
| value_type | varchar(20) | `text` / `number` / `bool` / `date` / `dim` |
| dim_code | varchar(50) | 仅 `dim` 类型，指向 `dim_entries.dim_code` |
| unit | varchar(50) | 单位（可选） |
| required | bool | 是否必填 |
| show_in_list | bool | 是否在列表展示 |
| options | JSON | 非维度字段的枚举候选 |
| sort_order | int | 展示顺序；同一 dim_code 内即层级顺序 |
| is_active | bool | 停用后不再生成属性，但历史值保留 |

**dim_entries** — 维度字典

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| dim_code | varchar(50) | `brand` / `platform` / … |
| name | varchar(100) | 节点名 |
| parent_id | FK → dim_entries | 同维度内的父节点 |
| path | varchar(500) | 物化路径，如 `/1/7/` |
| sort_order | int | 排序 |
| is_active | bool | |

唯一约束 `(dim_code, parent_id, name)`。

**tree_profiles** — 导航树配置

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| code | varchar(50) unique | 如 `default` |
| name | varchar(100) | 如「平台 → 品牌 → 车 → 模式」 |
| levels | JSON | 层级声明数组，见 2.3 |
| is_default | bool | 默认使用哪一套 |

**dataset_types** — 数据格式注册

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| code | varchar(50) unique | `handling_stability` |
| name | varchar(100) | 操稳数据 |
| description | text | |
| is_active | bool | |

**vehicles** — 车辆主表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(200) unique | 车名，**全局唯一** |
| created_at | timestamp | |

其余属性全部走 `vehicle_attr_values`。

**vehicle_attr_values** — 车辆属性值

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| vehicle_id | FK → vehicles | 级联删除 |
| attr_id | FK → attr_defs | |
| value_text | varchar(500) | text / date |
| value_num | float | number / bool |
| value_dim_id | FK → dim_entries | dim |
| updated_at | timestamp | |

唯一约束 `(vehicle_id, attr_id)`；索引 `(attr_id, value_dim_id)`、`(attr_id, value_num)`。

**vehicle_modes** — 车辆拥有的试验模式

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| vehicle_id | FK → vehicles | |
| mode | varchar(50) | 舒适 / 运动 / … |
| sort_order | int | |

唯一约束 `(vehicle_id, mode)`。

**test_orders** — 试验单号

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| vehicle_id | FK → vehicles | |
| discipline | varchar(20) | 操稳 / 平顺 |
| order_no | varchar(100) | 单号 |

唯一约束 `(vehicle_id, discipline, order_no)`。

**conditions** — 工况

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(100) unique | 角阶跃 / 角脉冲 / 稳态回转 |
| description | text | |
| category | varchar(100) | 分类，会写进该工况下指标的 `category` |

**datasets** — 统一数据信封

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| vehicle_id | FK → vehicles | |
| mode_id | FK → vehicle_modes，**可空** | 无模式的数据为 null |
| test_order_id | FK → test_orders，**可空** | 单号缺失为 null |
| condition_id | FK → conditions | |
| dataset_type_id | FK → dataset_types | 决定这条数据怎么解释 |
| name | varchar(300) | 数据集名（导入时 = 文件名去后缀） |
| exp_no | varchar(20) | `'1'` / `'2'` / `'平均'` |
| is_average | bool | |
| sort_order | int | 平均 = 999 |
| payload | JSON | 兜底容器，只放无法归入指标/曲线的结构 |
| created_at | timestamp | |

约束与索引：

- 唯一 `(vehicle_id, mode_id, condition_id, dataset_type_id, name, exp_no)` — 同一 slot 只存一条，重复导入即覆盖
- `(vehicle_id, condition_id, sort_order)` — 单车详情 / 重导
- `(condition_id, dataset_type_id, vehicle_id, mode_id)` — 对比取数主路径

**metric_values** — 标量指标

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| dataset_id | FK → datasets | 级联删除 |
| name | varchar(200) | 指标名（已去数字前缀） |
| unit | varchar(50) | 单位 |
| raw_key | varchar(300) | 原始 key，无损还原 |
| value | float | 数值 |
| text_value | varchar(500) | 文本值（备用） |
| category | varchar(100) | 指标分类 |

索引：`(dataset_id, name)`、`(dataset_id, raw_key)`、`(name, dataset_id)`。

**curves** — 曲线

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| dataset_id | FK → datasets | 级联删除 |
| group | varchar(20) | `cal`（计算曲线）/ `duiqi`（对齐曲线），新格式可新增 |
| name | varchar(200) | 曲线名，如「方向盘转角-时间」 |
| title | varchar(200) | 图标题 |
| xlabel / ylabel | varchar | 轴标签 |
| series | JSON | `[{x, y, curvename, curvetype}, …]` |
| point_count | int | 采样点数（冗余，避免解析 JSON） |
| sort_order | int | 曲线顺序 |

索引：`(dataset_id, group, sort_order)`。

### 3.4 跨库兼容

```python
JSONType = JSON().with_variant(JSONB, "postgresql")
```

PostgreSQL 走 JSONB（可索引、可压缩、TOAST），SQLite（本地开发）退化为普通 JSON。
同一套模型两边都能建表。

---

## 4. API 一览

### 4.1 配置

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 前端所需全部配置：字段、维度、树配置、数据格式、工况、模式建议 |
| GET | `/api/attrs` | 字段注册表（`?active_only=true`） |
| POST | `/api/attrs` | **新增车辆字段，不改表结构** |
| PUT | `/api/attrs/{id}` | 修改字段（名称 / 类型 / 枚举 / 停用） |
| POST | `/api/tree-profiles` | 新增导航树配置 |
| PUT | `/api/tree-profiles/{code}` | 修改层级顺序 / 来源 / 缺失策略 |

### 4.2 导航树与车辆

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tree` | 按配置投影出任意层级的树；`?profile=brand_first`；任意字段过滤 `?f_<code>=值` |
| GET | `/api/vehicles` | 车辆扁平列表（动态属性 + 模式 + 数据集数量） |
| GET | `/api/vehicles/{id}` | 车辆详情（属性 / 模式 / 单号） |
| POST | `/api/vehicles` | 新增车辆 |
| PUT | `/api/vehicles/{id}` | 修改车辆 |
| DELETE | `/api/vehicles/{id}` | 删除车辆 |

新增车辆的 body：

```json
{
  "name": "极光EV-01",
  "attrs": {"platform": "EPA1", "brand": "星河", "drive_type": "后驱", "vin": "..."},
  "modes": ["舒适", "运动"],
  "test_orders": [{"discipline": "操稳", "order_no": "CS-2024-001"}]
}
```

`attrs` 的 key 是 `attr_defs.code`，值按字段类型解析；维度字段可传名称（不存在会自动建节点）。

### 4.3 辅助

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/conditions` | 工况列表（`?vehicle_id=&mode_id=&dataset_type=`） |
| GET | `/api/curve-names` | 某工况的曲线名（`?condition_id=&dataset_type=`） |
| GET | `/api/indicator-names` | 某工况的指标名 |
| GET | `/api/health` | 健康检查 |

### 4.4 数据查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/segments/{vehicle_id}/conditions/{condition_id}/data` | 单个 segment × 工况的数据 |
| GET | `/api/compare` | 多 segment 对比（一次性返回） |
| GET | `/api/compare/stream` | 多 segment 对比（NDJSON 流式，GET 版） |
| **POST** | **`/api/compare/stream`** | **推荐**，曲线名等中文参数放 body |
| **POST** | **`/api/indicators/all`** | **推荐**，统计筛选放 body |
| GET | `/api/indicators/all` | 统计（GET 版，调试用） |
| GET | `/api/indicators/count` | 统计总行数 |

`segment` 写作 `vehicle_id:mode_id`，多个用逗号分隔：

```
segments=1:2,1:3,5        # 1 号车的舒适模式、运动模式；5 号车无模式
```

公共收敛参数：

| 参数 | 说明 |
|---|---|
| `dataset_type` | 数据格式 code，不传则全部格式 |
| `exp` | 只取指定实验号，逗号分隔（如 `exp=平均`） |
| `curves` | 只取指定曲线名，逗号分隔。**过滤下推到 SQL** |
| `exp_scope` | `avg_first` = 每个 segment 只取「平均」（无则第一次） |
| `meta_only` | `true` = 只返回元信息，不含 `series`（约 440× 更小） |

`/api/compare` 和 `/api/compare/stream` 都有**准入检查**：估算采样点总数超过
`MAX_COMPARE_NUMBERS`（默认 5000 万）返回 400，并在 `detail` 里给收敛建议。
`meta_only=1` 不受限制。

### 4.5 指标统计

**POST `/api/indicators/all`** 的 body：

```json
{
  "filters": {"brand": "星河", "energy_type": "纯电"},
  "condition_id": 3,
  "indicator": "侧向加速度稳态值",
  "average": true,
  "dataset_type": "handling_stability",
  "mode_id": null,
  "limit": 200,
  "offset": 0
}
```

`filters` 的 key 是任意 `attr_defs.code`。GET 版把同样内容写成
`?f_brand=星河&condition_id=3&...`。

返回：

```json
{
  "total": 1280,
  "offset": 0,
  "limit": 200,
  "rows": [
    {
      "vehicle_id": 1,
      "vehicle": "极光EV-01",
      "mode_id": 2,
      "mode": "舒适",
      "condition": "角阶跃",
      "dataset_id": 42,
      "exp_no": "平均",
      "is_average": true,
      "attrs": {"brand": {"code": "brand", "name": "品牌", "value": "星河", "...": "..."}},
      "indicators": {
        "侧向加速度稳态值": {"value": 0.54, "text_value": null, "unit": "g", "name": "侧向加速度稳态值"}
      }
    }
  ]
}
```

### 4.6 上传导入

`POST /api/upload`（`multipart/form-data`）

| 字段 | 说明 |
|---|---|
| `files` | 数据文件（`.json` / `.msgpack` / `.msg` / `.mpack`）+ 可选 CSV |
| `paths` | 与 `files` 顺序一致的相对路径 JSON 数组 |
| `mode` | `replace`（默认，先 drop_all）/ `append`（按 slot 覆盖） |
| `dry_run` | `true` = 只解析预览，不写库 |
| `exts` | 只认指定后缀 |
| `dataset_type` | 本次数据按哪种格式解析（`dataset_types.code`） |

返回 `summary`（将写入什么）+ `imported`（实际写入）+ `errors`（单文件失败不中断）
+ `skipped`。体积上限 `MAX_UPLOAD_MB`（默认 512），超了 413。

### 4.7 返回结构（对比）

```json
{
  "condition_id": 3,
  "condition_name": "角阶跃",
  "dataset_type": "handling_stability",
  "vehicles": [
    {
      "vehicle_id": 1,
      "vehicle_name": "极光EV-01",
      "mode_id": 2,
      "mode": "舒适",
      "attrs": {"brand": {"code": "brand", "name": "品牌", "value": "星河"}},
      "experiments": [
        {
          "dataset_id": 42,
          "exp_no": "平均",
          "is_average": true,
          "name": "角阶跃",
          "dataset_type": "handling_stability",
          "payload": null,
          "params": {
            "13-侧向加速度稳态值(g)": {
              "name": "侧向加速度稳态值", "unit": "g",
              "raw_key": "13-侧向加速度稳态值(g)",
              "value": 0.54, "text_value": null
            }
          },
          "curves": {
            "cal": {
              "方向盘转角-时间": {
                "name": "方向盘转角-时间",
                "title": "方向盘转角vs时间",
                "xlabel": "时间（s）", "ylabel": "方向盘转角（deg）",
                "point_count": 500,
                "series_names": ["原始数据", "滤波"],
                "series": [{"x": [], "y": [], "curvename": "原始数据"}]
              }
            },
            "duiqi": {}
          }
        }
      ]
    }
  ],
  "curve_meta": {
    "groups": {
      "cal": {
        "series_names": ["原始数据", "滤波"],
        "curves": {"方向盘转角-时间": {"title": "…", "xlabel": "…", "ylabel": "…", "point_count": 500, "series_names": ["原始数据", "滤波"]}}
      }
    }
  }
}
```

注意 `curves` 是 **`{group: {name: curve}}`**，group 由数据决定（`cal` / `duiqi` /
以后新增的分组），前端不写死。`meta_only=1` 时 `series` 恒为 `[]`。

---

## 5. 环境准备

### 5.1 依赖

```bash
pip install -r requirements.txt
```

核心：FastAPI + SQLAlchemy 2.0 + psycopg2。可选加速：`orjson`（大响应序列化快 2~4 倍）、
`numpy`（造数快 5~10 倍）、`msgpack`（读 msgpack 数据时才需要）、`python-multipart`（上传接口）。

### 5.2 数据库

项目**默认用 PostgreSQL**：

```
postgresql+psycopg2://postgres:123456@localhost:5432/vehicle_test
```

改连接串在 `.env`：

```ini
# 用 PG
DATABASE_URL=postgresql+psycopg2://postgres:123456@localhost:5432/vehicle_test

# 或者切回 SQLite（零依赖，适合纯本地试跑）
# SQLITE_PATH=./dev.db
```

优先级（高 → 低）：

1. **真实环境变量** `DATABASE_URL`（Docker / CI 用，覆盖一切）
2. `SQLITE_PATH=./dev.db` → SQLite 文件
3. `USE_SQLITE=1` → SQLite at `./vehicle_test.db`
4. `.env` 里的 `DATABASE_URL`，兜底内置 PG 默认值

自检当前配置：

```bash
python -m app.config
# DATABASE_URL = postgresql+psycopg2://postgres:123456@localhost:5432/vehicle_test
# IS_SQLITE    = False
```

忘了 PG 密码可以用 `reset.py`（临时改 `pg_hba.conf` 为 trust，改完还，不丢数据）：

```bash
python reset.py --password 123456     # Windows 需管理员权限
```

### 5.3 建表

```bash
python scripts/init_db.py            # 缺表就建，元数据幂等补齐
python scripts/init_db.py --drop     # 先删所有表再建（数据全丢）
```

建表会同时写入默认元数据：14 个车辆字段、3 套导航树配置、3 种数据格式。

---

## 6. 操作 A：生成演示数据

适用于：**手上还没有真实数据，想先把平台跑起来看效果。**

### 6.1 一条命令

```bash
python scripts/seed_data.py --preset tiny
```

这条命令会**清空全部数据 + 重建元数据**，并同时在 `data/` 下导出 JSON 文件。

输出大致：

```
Seed complete  (24.0s)
  Vehicles    : 11（模式组合：舒适/运动 / 舒适 / 无模式 …）
  Conditions  : 5
  Datasets    : 165
  Indicators  : 3399
  Curves      : 2310
  Points      : 1,650,000
  JSON files  : ./data
```

演示数据刻意做成**不同车辆模式数量不固定**：有的无模式、有的只有「舒适」、
有的「舒适 + 运动」；部分车辆带一个或多个操稳单号，部分没有单号。

### 6.2 规模预设

| preset | 车辆 | 工况 | 普通点数 | 稳态点数 | 用途 |
|---|---|---|---|---|---|
| `tiny` | 11 | 5 | 500 | 5000 | 快速验证 |
| `demo` | 200 | 20 | 1000 | 10000 | 默认 |
| `standard` | 200 | 20 | 3000 | 30000 | 性能测试 |
| `full` | 200 | 20 | 6000 | 100000 | 压力测试，**很慢** |

### 6.3 精细控制

```bash
# 自定义规模
python scripts/seed_data.py --vehicles 30 --conditions 10 --runs 3 --points 2000

# 只写库不导 JSON，快很多
python scripts/seed_data.py --preset demo --no-json

# 固定随机种子（复现同一批数据）
python scripts/seed_data.py --preset tiny --seed 42
```

| 参数 | 说明 |
|---|---|
| `--vehicles` | 车辆数（覆盖 preset） |
| `--conditions` | 工况数（覆盖 preset） |
| `--runs` | 每台车每模式每工况实验次数（**不含平均**） |
| `--points` | 普通工况采样点数 |
| `--steady-points` | 稳态回转的采样点数 |
| `--no-json` | 不导出 `data/*.json` |
| `--seed` | 随机种子 |

> 造数直接走 `importer.import_items`，和真实导入共用同一套解析/落库规则。

---

## 7. 操作 B：导入真实数据

适用于：**算法侧已经产出了数据，要灌进平台。**

### 7.1 目录约定

```
real_data/
├── 极光EV-01(CS-2024-001)/   ← 文件夹名 = 车名，括号里是操稳单号（可省略）
│   ├── 舒适/                  ← 中间目录 = 试验模式（可多层，可没有）
│   │   ├── 角阶跃.json
│   │   └── 角脉冲.json
│   └── 运动/
│       └── 角阶跃.json
├── 星河EV-01/                 ← 没有模式，工况文件直接放车目录下
│   ├── 角阶跃.json
│   └── 稳态回转.json
├── vehicles.csv               ← 可选，任意列的车辆属性
└── conditions.csv             ← 可选，工况分类
```

规则：

- **一台车一个文件夹，一个工况一个文件**
- 车名取文件夹名（去掉结尾括号里的单号），**全局唯一**
- 中间目录都是试验模式，按 `/` 拼接（`舒适/赛道` 会成为一个模式名）
- 模式数量完全由目录决定，不需要提前配置
- 只有数据文件是必需的；两个 CSV 都可选

#### 先看一份能跑的完整示例

```bash
python scripts/make_example_data.py --out ./example_data
python scripts/import_data.py --data-dir ./example_data --dry-run
```

产出（2 台车 × 2 工况 × 3 份文件，含两种写法）：

```
example_data/
├── vehicles.csv
├── conditions.csv
├── 示例车A/
│   ├── 角阶跃.json           写法 A：{"1": [...], "2": [...], "平均": [...]}
│   └── 角脉冲.json           写法 A：只有 "1" "2"
└── 示例车B/
    └── 角阶跃.json           写法 B：{"params":…, "curves_cal":…, "curves_duiqi":…}
```

`--dry-run` 输出：

```
  车辆 2 台 / 模式 0 种 / 工况 2 个 / 文件 3 份
  将写入：数据集 6 条 / 指标 42 行 / 曲线 36 条（10,800 个采样点）
  车辆元信息来自: ./example_data/vehicles.csv（3 台）
```

> 注意「车辆元信息 3 台」和「车辆 2 台」不一致是正常的：
> `vehicles` 表按**扫描到的文件夹**建，不是按 CSV 建。CSV 里多列几台没数据的车无妨；
> 漏了几台有数据的车也不会报错，只是那几台属性为空。
> 示例目录没有模式层；想验证模式，把 `角阶跃.json` 放进 `示例车A/舒适/` 即可。

### 7.2 文件格式：JSON 或 msgpack

**后缀决定怎么读，内容结构完全一致**——解出来都是同一个 dict，走同一套解析逻辑。

| 后缀 | 读取方式 | 需要额外装库 |
|---|---|---|
| `.json` | 标准 JSON（utf-8，可带 BOM） | 不需要 |
| `.msgpack` `.msg` `.mpack` | msgpack | `pip install msgpack` |

```bash
pip install msgpack
python scripts/import_data.py --data-dir ./real_data
python scripts/import_data.py --data-dir ./real_data --ext .msgpack
```

> **msgpack 的中文 key 坑**：老版本 dump 出来的中文 key 是 bytes，直接读会得到
> `b'\xe8\xa7\x92...'` 这种乱码 key。脚本已做自动解码（`_decode_msgpack_keys`），
> 递归把 bytes key/value 解成 str；**但 float 数组刻意不碰**——曲线点动辄几十万个，
> 逐个转换会显著拖慢导入。

### 7.3 数据内容结构

**写法 A（推荐，与算法侧 dict 一致）** — 顶层 key 是实验号：

```python
{
  "1":    [params1,    curves_cal1,    curves_duiqi1],
  "2":    [params2,    curves_cal2,    curves_duiqi2],
  "平均": [paramsAvg,  curves_calAvg,  curves_duiqiAvg],
}
```

其中三段的形状：

```python
params = {
  "13-侧向加速度稳态值(g)": 0.54,
  "车速(km/h)": 80.0,
  ...
}

curves_cal = {
  "方向盘转角-时间": {
    "curve": [                                   # 键名也可以是 curves / series
      {"x": [...], "y": [...], "curvetype": "line", "curvename": "原始数据"},
      {"x": [...], "y": [...], "curvetype": "line", "curvename": "滤波"},
    ],
    "title": "方向盘转角vs时间",
    "xlabel": "时间（s）",
    "ylabel": "方向盘转角（deg）",
  },
  ...
}

curves_duiqi = { ...同 curves_cal 结构... }
```

**写法 B（扁平）** — 顶层直接是 params：

```python
{
  "params": { ... },
  "curves_cal": { ... },
  "curves_duiqi": { ... },
}
```

> 与 `dataset_types` 对应：`handling_stability` 走上面这套解析；
> **认不出的结构**不会报错，整块进 `datasets.payload`（`generic` 用法）。

### 7.4 元信息（可选，但强烈建议）

先回答常见疑问：**数据文件（json/msgpack）里只有指标和曲线，没有任何车辆属性。**
算法侧只负责算结果，不负责描述车，所以平台 / 品牌 / 驱动形式这些只能另外给。

#### `vehicles.csv` — 车辆属性（任意列）

放在数据目录根部，文件名固定（或用 `--vehicles-csv` 指定）。表头中英文都认：

```csv
车名,平台,品牌,子品牌,驱动形式,VIN号,生产日期,能源形式,操稳单号,试验模式
示例车A,EPA1,星河,,后驱,LGWEF6E40PF000001,2024-03-01,纯电,CS-2024-001,舒适;运动
示例车B,EPA2,星河,星锐,四驱,LGWEF6E40PF000002,2025-01-15,纯电,CS-2024-002;CS-2024-018,舒适
示例车C,EPA2,云驰,,前驱,LGWEF6E40PF000003,2025-02-20,插电混动,,舒适;运动;赛道
```

列的处理规则：

| 列 | 说明 |
|---|---|
| 车名 / 车型 / 车辆名称 / 名称 / vehicle / name | 关联键，**必须和数据文件夹名一致** |
| 模式 / 试验模式 / 车辆模式 / modes / test_modes / mode | 多个用 `; , \| 、` 分隔 |
| 操稳单号 / 操稳单 / handling_order / test_order / 单号 | 可多个 |
| 平顺单号 / 平顺单 / ride_order | 可多个（平顺数据暂不设计） |
| 其它任意列 | 按 `attr_defs.code` 或 `attr_defs.name` 匹配 |

**匹配不到的列会自动注册成新字段**（`value_type` 按该列所有值推断 `text` / `number`），
所以 CSV 直接加一列「整备质量」，导入后它就是一个可筛选、可展示的车辆字段，
不需要改表、不需要改代码。

旧表头兼容：`驱动方式` → `drive_type`，`车架号` → `vin`，`年款` → `model_year`。

> 最稳的做法是先在数据目录里 `ls` 一遍文件夹名，直接复制到 CSV 的「车名」列。

#### `conditions.csv` — 工况分类

文件名固定（或用 `--conditions-csv` 指定）。

```csv
name,category,description
角阶跃,操纵性,方向盘阶跃输入下的瞬态响应
角脉冲,操纵性,方向盘脉冲输入下的频率响应
稳态回转,稳态,固定半径稳态圆周
```

| 列 | 别名 | 落库位置 |
|---|---|---|
| `name` | 工况 | `conditions.name`，**必须和数据文件名（去后缀）一致** |
| `category` | 分类 | `conditions.category`，并写进该工况下所有指标的 `category` |
| `description` | 说明 | `conditions.description` |

> 不给 `conditions.csv` 也能导——工况名从文件名拿，只是 `category` / `description` 为空。

### 7.5 指标 key 的写法

推荐 `序号-指标名(单位)`，但**序号可以省略**，三种都认：

| key | name | unit |
|---|---|---|
| `13-侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `13-侧向加速度稳态值` | 侧向加速度稳态值 | *(空)* |
| `侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `侧向加速度稳态值` | 侧向加速度稳态值 | *(空)* |

分隔符支持 `-` `.` `_` `、`；单位取最外层括号。
**序号不存库**（见 2.7），它只是被解析掉，`name` 和 `unit` 才是检索用的。

### 7.6 实验号识别

| 顶层 key | 识别为 |
|---|---|
| `1` / `2` / `3` / … | 第 N 次实验，`sort_order` = N-1 |
| `平均` / `avg` / `average` / `AVE` / `AVE.` | 平均实验，`is_average=true`，`sort_order=999` |

### 7.7 导入流程

```bash
# 第 1 步：先预览！确认解析结果对不对，不写库
python scripts/import_data.py --data-dir ./real_data --dry-run
```

输出样例：

```
扫描数据目录: ./real_data  （后缀 .json, .msgpack, .msg, .mpack）
  车辆 11 台 / 模式 2 种 / 工况 5 个 / 文件 27 份
  将写入：数据集 165 条 / 指标 3399 行 / 曲线 2310 条（1,650,000 个采样点）
  车辆元信息来自: ./real_data/vehicles.csv（11 台）

--dry-run：未写库。样例：
  极光EV-01 / 舒适 / 角阶跃 → 实验号 ['1', '2', '平均']
```

**重点核对这几个数字**：车辆数、模式数、数据集数、指标行数。
对不上说明结构没被正确识别，往下导就是错数据。

```bash
# 第 2 步：正式导入
python scripts/import_data.py --data-dir ./real_data
```

### 7.8 ⚠️ 清空 vs 追加

| 命令 | 行为 |
|---|---|
| `python scripts/import_data.py --data-dir ./data` | **先 `DROP ALL` 清空**，再导入 |
| `python scripts/import_data.py --data-dir ./data --append` | 保留其他数据，**同一 slot 按本次内容覆盖** |

**默认是清空。** 导入真实数据前想保住演示数据，就加 `--append`。

`--append` 的覆盖粒度是 **`(车, 模式, 工况, 数据格式, 文件名)`**，不是简单追加：

- 同一个 slot 再导一次，会先删掉它原有的数据集再写新的（`Curve` / `MetricValue`
  由外键级联删除），所以「重新上传同一份数据」不会撞唯一约束，也不会出现重复实验
- 层级（车辆 / 模式 / 单号 / 工况）在 append 时**复用已有的**，不会重复插
- CSV 没给的字段保持原值，不会被 `None` 覆盖

### 7.9 全部参数

| 参数 | 说明 |
|---|---|
| `--data-dir` | 数据目录（默认 `./data`） |
| `--vehicles-csv` | 车辆元信息 CSV（默认找 `<data-dir>/vehicles.csv`） |
| `--conditions-csv` | 工况分类 CSV |
| `--ext` | 只认指定后缀，逗号分隔 |
| `--append` | 按 slot 覆盖而不是清空 |
| `--dry-run` | 只预览，不写库 |

### 7.10 导完怎么验证

```bash
# 查各表数量
python -c "
from sqlalchemy import create_engine, text
from app.config import DATABASE_URL
e = create_engine(DATABASE_URL)
with e.connect() as c:
    for t in ('vehicles','vehicle_modes','test_orders','conditions',
              'datasets','metric_values','curves','attr_defs'):
        print(t, c.execute(text('select count(*) from '+t)).scalar())
"
```

再打开前端或 Swagger 抽查一个分析单元：

```
GET /api/segments/1/conditions/1/data?mode_id=2&exp=平均
GET /api/compare?segments=1:2,1:3&condition_id=1&meta_only=1
```

### 7.11 页面导入：前端「上传数据」页

`import_data.py` 只能读**服务器本机**的目录，而数据是算法侧在别的机器上产出的。
「上传数据」页（`/upload`）把这条路径变成「把文件夹拖进浏览器」，不用共享盘、
不用先手工拷到服务器再敲命令。

```bash
# 起前后端，然后打开 http://localhost:5173/upload
# 拖入 ./example_data 试试
```

流程：**选/拖文件夹 → 预览（不写库）→ 开始导入**（`replace` 会弹二次确认）。

规则与命令行**完全共用** `app/importer.py`，所以两边导进去的结果一定一致 ——
这正是不把解析逻辑写在 API 里的原因（否则「命令行导入」和「页面导入」会慢慢
长出两套行为：模式怎么识别、`raw_key` 怎么拆、CSV 用哪一列关联车名，一旦对不上很难发现）。

实现上有三个坑，都处理掉了：

| 坑 | 症状 | 处理 |
|---|---|---|
| 浏览器 multipart **只带 `filename`**，`webkitRelativePath` 不进请求体 | 服务端只收到一堆光秃秃的 `角阶跃.json`，不知道属于哪台车 | 前端把路径列表当普通 form 字段 `paths`（JSON 数组）一起发 |
| 根目录该不该剥，**要看全量路径才能判断** | 剥多了车名没了；剥少了车名变成 `data`，更糟的是两台车会撞成同一台 | 前端「原样发送」，由 `importer.strip_common_root` 统一判断（优先以 `vehicles.csv` 所在目录为根） |
| 先把所有文件读完再解析 | 曲线数据几百 MB，原始文本 + 解析结果同时驻留，峰值翻倍 | `importer.ItemBuilder` 逐个读、解析完立刻丢原始字节；前端直接把 `File` 塞 FormData 交给浏览器流式发 |

> 验证：`python scripts/verify_upload.py`（用**临时 SQLite 库**，不会碰 PG）
> + `bash frontend/scripts/run_e2e_upload.sh`（真实 Chromium 里选文件夹、预览、导入）。

---

## 8. 日常运行

### 8.1 一键启动

```bash
./start.sh          # Linux / macOS
start.bat           # Windows
```

脚本会自动判断：`.env` 里有 `DATABASE_URL` → 用 PG，**跳过自动初始化**
（避免往生产库灌 tiny 数据）；否则用 SQLite 且库不存在时自动建表灌数据。

### 8.2 手动启动

```bash
# 后端
python -m app.main
# 或
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev          # 开发服务器，proxy /api → :8000
npm run build        # 生产构建
```

访问：

- 前端：`http://localhost:5173`
- Swagger：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/health`

### 8.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GZIP` | `1` | GZip 中间件开关。本机调试嫌压得慢可设 `0` |
| `GZIP_LEVEL` | `1` | 压缩级别。**别调高**——level 9 压 2.5MB 要 ~0.9s，反而慢 10 倍 |
| `MAX_COMPARE_NUMBERS` | `50000000` | 单次对比允许的估算采样点总数，超了 400 |
| `STREAM_BATCH_CURVES` | `24` | 流式端点每批取多少条曲线的 series。**内存旋钮** |
| `MAX_UPLOAD_MB` | `512` | 单次上传体积上限 |
| `TIMING` | `1` | 耗时埋点开关 |
| `HOST` / `PORT` / `RELOAD` | `0.0.0.0` / `8000` / `1` | 仅 `python -m app.main` 生效 |

### 8.4 前端页面

页头只有三个菜单项，没有品牌文案和技术栈说明。

**页面 1（对比分析）**

- 左侧目录树按 `tree_profiles` 动态渲染，可搜索、可切换层级配置
- **叶子是 segment（车 × 模式）**，不是车；同一台车的舒适 / 运动可分别勾选
- 选工况 + 数据格式 → 两阶段加载：先出指标表和曲线选择区，曲线数据随后**流式**到达
- 「曲线」多选：默认全选；取消勾选不删数据，重新勾选时若已加载过则不再发请求
- 「同图取线」：平均优先 / 全部实验叠加
- 指标表格：行 = 指标，列 = segment × 实验
- 界面上**不显示**负载预估行、降采样开关、耗时数字（都在代码里 / Console）

**页面 2（指标统计）**

- 筛选条件由 `meta.attrs` **动态生成**：维度字段 + 带 `options` 的字段各一个下拉
  （新增「能源形式」字段，页面自动多一个筛选框，前端不用改代码）
- 工况用 **ID** 传参，避免中文进 URL
- 服务端真分页（`LIMIT/OFFSET` 在 SQL 里做）
- 可导出 CSV

**页面 3（上传数据）**

- 拖文件夹进来，或点「选择文件夹」
- 选择本次数据的数据格式（来自 `dataset_types`，可扩展）
- 实时显示：文件数、数据文件数、元信息 CSV、体积、识别出的车名文件夹
- 「预览（不写库）」→ 看解析摘要；「开始导入」→ `replace` 会弹二次确认
- 结果区：各表条数 + CSV 来源 + 车名 / 模式 / 工况列表 + 解析失败 / 被跳过的文件
- 详见 [7.11](#711-页面导入前端上传数据页)

---

## 9. 性能与耗时观测

### 9.1 为什么需要

页面上那个「加载完成 X.Xs」**不是数据库耗时**——它是 `performance.now()` 从点击到
数据赋值完成，中间混着网络传输、JSON 解析、Vue 响应式赋值和 ECharts 渲染。
要定位瓶颈必须拆开。

### 9.2 后端：打日志

每个请求往 **stderr** 打两行（`app/timing.py`）：

```
[timing] compare        db=0.023s  total=0.023s  segments=2      ← 业务层：纯 SQL
[timing] /api/compare   server=0.035s  total=0.035s              ← 中间件层：含路由+序列化
```

`TIMING=0` 可整体关闭（变 no-op，零开销）。

给新端点埋点：

```python
from app.timing import Timer

t = Timer("my_endpoint")
with t.stage("db"):
    data = crud.something(db)
t.finish(rows=len(data))
return json_response(data, t)      # 传 timer 则序列化也计入
```

### 9.3 后端：响应头

| 响应头 | 含义 |
|---|---|
| `X-Server-Time-Ms` | 服务端总耗时，毫秒（如 `34.7`） |
| `Server-Timing` | 标准格式：`total=0.035; server=0.035` |

> **跨域注意**：自定义响应头必须写进 CORS 的 `expose_headers`，否则前端读到
> `undefined`。已在 `app/main.py` 配置好。

> **流式响应没有 `X-Server-Time-Ms`** —— 响应头先于数据发出。耗时读 `done` 行的 `ms`。

### 9.4 前端：控制台日志

```
[timing] compare  服务端=23ms  网络=12ms  解析=8ms  渲染=145ms  合计=188ms
[timing] indicators  服务端=55ms  网络=3ms  解析=6ms  渲染=20ms  合计=84ms  行数=200
```

| 段 | 含义 | 测法 |
|---|---|---|
| 服务端 | 后端 SQL + 序列化 | 读 `X-Server-Time-Ms`（流式读 `done.ms`） |
| 网络 | 发出请求到收完响应体（含 gzip 解压） | axios 响应拦截器 |
| 解析 | `JSON.parse` 变成 JS 对象 | 同上 |
| 渲染 | Vue 赋值 + ECharts / el-table 画完 | `await nextTick()` 之后 |

### 9.5 实测参考（tiny 数据）

| 场景 | 服务端 | 网络 | 解析 | 渲染 |
|---|---|---|---|---|
| 3 车对比（445 KB, gzip） | 117 ms | 4 ms | 18 ms | 61 ms |
| 指标分页（393 KB） | 55 ms | 3 ms | 6 ms | 20 ms |
| 导航树（0.5 KB） | 100 ms | 2 ms | 3 ms | 8 ms |

小接口首次请求的 100 ms 是冷启动开销，后续会降到 2~6 ms。

### 9.6 规模实测

> 下列数字在重构前的表结构上测得（`experiments/indicators/curves`）。
> 重构只是把 `experiments` 改名 `datasets` 并加了数据格式外键，
> **曲线「一条一行」的存储、`curves=` 下推、流式分批的代码路径都没变**，
> 量级可直接参考。

**响应体积与耗时**（稳态回转，GZIP level 1）：

| 场景 | gzip | 原始 JSON | 耗时 |
|---|---|---|---|
| 50 车 × 全部实验 × 全部曲线 | 176.8 MB | 357.0 MB | 10.99 s |
| 50 车 × 平均 × 全部曲线 | 58.0 MB | 118.7 MB | 3.89 s |
| 50 车 × 平均 × 1 条曲线 | 8.6 MB | 18.2 MB | 0.63 s |
| 50 车 × 平均 × meta_only | **11 KB** | 205 KB | **0.03 s** |
| 200 车 × 平均 × 1 条曲线 | 34.3 MB | 72.8 MB | 2.52 s |
| 200 车 × 平均 × meta_only | 40 KB | 811 KB | 0.10 s |
| 200 车 × 平均 × 全部曲线 | — | 400 拦截 | 0.03 s |

**流式 vs 一次性返回**（50 车 × 平均 × 7 条）：

| 路径 | RSS 峰值增量 | 总耗时 | 首行到达 |
|---|---|---|---|
| `/api/compare` | 794.0 MB | 6.60 s | — |
| `/api/compare/stream` | **11.6 MB** | 3.71 s | **0.182 s** |

**前端渲染表示**（8 条线 × 10 万点）：

| 表示 | 每点驻留 | `setOption` |
|---|---|---|
| `number[][]` | ~520 B/点 | 656 ms |
| 交错 `Float64Array` | ~40 B/点 | 286 ms |

### 9.7 降采样耗时

| 输入点数 | 抽到 1 万点耗时 |
|---|---|
| 10,000 | 0.1 ms |
| 50,000 | 2.0 ms |
| 200,000 | 1.3 ms |
| 500,000 | 6.2 ms |

即使 50 万点也只要 6 ms，不值得为它做 worker 或节流，同步跑就好。

---

## 10. 常见问题排查

### 导入时提示「结构无法识别」/ 数据进了 payload

`parse_payload()` 认不出你的 dict 形状，此时数据不会报错，会整块进
`datasets.payload`。常见原因：

- 曲线组的键名不是 `curve` / `curves` / `series`
- 顶层实验号是浮点 `1.0` 而不是字符串 `'1'`
- params 和曲线被包在别的层级里

处理：用 `--dry-run` 看预览数字对不对；需要看原始结构时：

```bash
python -c "import msgpack; print(msgpack.unpack(open('x.msgpack','rb'), raw=False, strict_map_key=False))"
```

### 车名对不上 CSV

CSV 的「车名」必须和数据文件夹名**完全一致**（若文件夹带单号括号，写去掉括号后的车名）。
先用 `ls` 复制文件夹名，再填 CSV。

### 导航树少了一层 / 多了一层

层级完全由 `tree_profiles.levels` 决定：

```bash
# 看当前配置
curl http://localhost:8000/api/meta | python -m json.tool
```

- 某层整层消失：配置里 `missing: "skip"` 且所有车都没这个字段
- 出现「未分模式」节点：`test_mode` 层配置了 `missing: "label"`
- 想调整顺序：`PUT /api/tree-profiles/default`，改 `levels` 数组即可，不用重启

### 同一台车的模式勾错了

树的叶子是 segment，标签是「模式名」，但 `mode_id` 才是真正的键。
前端勾选传的是 `vehicle_id:mode_id`，不要再从车名反推模式。

### 指标名出现乱码 / `b'\xe8\xa7\x92...'`

msgpack 的 bytes key 没被正确解码。正常会自动处理，若仍异常说明编码不是 utf-8：

```bash
python -c "
import msgpack
d = msgpack.unpack(open('文件.msgpack','rb'), raw=False, strict_map_key=False)
print(type(list(d)[0]), repr(list(d)[0]))
"
```

### 导入后曲线画不出来

按顺序查：

1. **第二阶段还没回来** —— 图上有「曲线数据加载中…」遮罩
2. **`series` 是空数组** —— `meta_only=1` 的响应里 `series` 恒为空
3. **`curves=` 名字写错了** —— 过滤下推后名字对不上就一条都查不出来，且不报错。
   用 `GET /api/curve-names?condition_id=` 确认准确名字
4. **流里返回了 `error` 行** —— 生成中出错时 HTTP 200 已发出，错误作为一行数据返回

### 中文参数报 400 `Invalid HTTP request received.`

说明中文进了 URL query，被中间层解码后原样转发。改用对应的 **POST body** 接口：
`POST /api/indicators/all`、`POST /api/compare/stream`。

### 流式响应像是一次性到齐

1. **反向代理在缓冲** —— nginx 默认 `proxy_buffering on`，加 `proxy_buffering off;`
2. **curl 忘了 `-N`**
3. **数据本来就小** —— 3 台车秒级跑完，看不出渐进

### 请求返回 400「响应过大」

准入检查拦下的，按 `detail` 里的建议收敛：

```
① 加 curves= 只取要画的曲线（最有效）
② 加 exp_scope=avg_first 只取平均实验（省 ~2/3）
③ 先用 meta_only=1 拿元信息再按需取数据
④ 减少勾选的分析单元
⑤ 调大 MAX_COMPARE_NUMBERS
```

### 前端报错但后端日志正常

大概率是 CORS 或代理配置。确认 `frontend/vite.config.ts` 的 proxy 指向正确后端端口。

---

## 11. 扩展指南

| 场景 | 做法 | 要改代码吗 |
|---|---|---|
| 新增车辆字段 | `POST /api/attrs` 或 `attr_defs` 插一行 | 不用 |
| CSV 里出现新列 | 导入器自动注册成字段 | 不用 |
| 字段改枚举候选 | `PUT /api/attrs/{id}` 改 `options` | 不用 |
| 调整树层级 / 顺序 | `PUT /api/tree-profiles/{code}` 改 `levels` | 不用 |
| 新增一套树方案 | `POST /api/tree-profiles` | 不用 |
| 新增数据格式 | `dataset_types` 插一行；解析逻辑在 `importer.ItemBuilder` 加分支 | 只有解析器 |
| 新增曲线分组 | `curves.group` 用新取值，前端从 `curve_meta.groups` 自动发现 | 不用 |
| 新增指标名 | 导入时自动出现，`metric_values` 插行 | 不用 |
| 新增工况 | 数据文件名即工况名；分类写 `conditions.csv` | 不用 |
| 新增车辆模式 | 目录加一层，或 CSV「模式」列 | 不用 |
| 新增单号类型 | `test_orders.discipline` 加取值 | 不用 |
| 换降采样算法 | 改 `frontend/src/utils/downsample.ts` | 是 |
| 调整准入上限 | `MAX_COMPARE_NUMBERS` | 不用 |
| 调流式批量 | `STREAM_BATCH_CURVES` | 不用 |
| 新增收敛参数 | 在 `_load_datasets` 的 `select()` 上加条件，**别在 Python 里过滤** | 是 |

### 改代码前的六条硬规矩

1. **不要给车辆字段建列** —— 走 `attr_defs`，否则字段一多就要停机迁移
2. **不要把导航树层级写死** —— 只认 `tree_profiles.levels`，代码里不要出现
   「platform → brand → vehicle」这类假设
3. **不要把曲线合并回数据集 payload** —— 一条曲线一行是「只取勾选曲线」和
   `meta_only` 的前提
4. **不要给指标名建字段，不要存数字前缀** —— 两件事都被真实数据推翻过
5. **过滤必须下沉 SQL** —— 否则分页 `total` 会错，而且白付 I/O 与反序列化
6. **大数组别用 `[[x, y], …]`，流式里查重列用 Core 查询** —— 前者内存 10×，
   后者 ORM identity map 会把流式省下的内存又吃回去

---

## 附：文件清单

```
vehicle-test-db/
├── app/
│   ├── config.py        数据库 URL 解析（PG / SQLite 切换）
│   ├── database.py      engine / session / Base
│   ├── models.py        ORM 模型（12 张表，元数据驱动）
│   ├── metadata.py      默认元数据：字段 / 维度 / 树配置 / 数据格式（幂等写入）
│   ├── schemas.py       Pydantic v2 响应与请求体
│   ├── crud.py          所有查询：树投影 / 属性读取 / 对比 / 流式 / 统计
│   ├── importer.py      导入：解析（JSON/msgpack/CSV）+ 写库（CLI 与上传共用）
│   ├── timing.py        分段耗时埋点
│   └── main.py          FastAPI 应用 + 路由 + 准入检查 + 流式 + 上传
├── scripts/
│   ├── init_db.py       建表 + 写默认元数据
│   ├── seed_data.py     造演示数据（4 档预设，模式数量不固定）
│   ├── import_data.py   导入真实数据（JSON / msgpack）—— 薄壳，逻辑在 app/importer.py
│   ├── make_example_data.py  生成最小示例数据（对照导入格式用）
│   ├── verify_optimize.py    对真库验证 crud 改动（只读）
│   ├── verify_stream.py      验证流式端点：等价性 / RSS 峰值 / 首行延迟
│   ├── verify_upload.py      验证上传导入接口（临时 SQLite 库）
│   └── bench_compare.py      各收敛手段的 HTTP 体积与耗时基准
├── frontend/            Vue 3 + TS + Element Plus + ECharts
│   ├── src/
│   │   ├── api/         axios 封装 + 耗时采集 + streamCompare + uploadData
│   │   ├── types/       TS 类型（对齐元数据驱动结构）
│   │   ├── utils/       downsample.ts（LTTB 降采样）
│   │   ├── components/  VehicleTree（动态层级）/ CurveChart（交错 Float64Array）
│   │   └── views/       对比分析页 / 指标统计页 / 上传数据页
│   └── scripts/         e2e 与渲染等价性 / 内存验证脚本
├── example_data/        最小示例目录（vehicles.csv / conditions.csv / 2 台车）
├── reset.py             重置 PG 密码
├── start.sh / start.bat 一键启动
├── requirements.txt
├── DESIGN.md            本文件（详细设计）
└── README.md            精简版说明
```
