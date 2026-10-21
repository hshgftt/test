# 整车操稳试验数据平台 — 设计思路与操作手册

> 面向：接手这个项目的人（包括三个月后的你自己）。
> 读完应该能独立跑起来、导数据、改结构。

---

## 目录

1. [这个项目解决什么问题](#1-这个项目解决什么问题)
2. [设计思路：为什么是这样](#2-设计思路为什么是这样)
   - 2.1 指标用 EAV 行存储 / 2.2 `raw_key` 保留，数字前缀不存 / 2.3 曲线整块存 JSONB
   - 2.4 实验号是字符串，实验次数不固定 / 2.5 数据种类不写死 / 2.6 过滤下沉到 SQL
   - 2.7 曲线降采样放在前端，用 LTTB
   - **2.8 大数据量：按「实际负载」治理 + NDJSON 流式端点** ← 200 车场景必读
3. [数据模型](#3-数据模型)
4. [API 一览](#4-api-一览)
5. [环境准备](#5-环境准备)
6. [操作 A：生成演示数据](#6-操作-a生成演示数据)
7. [操作 B：导入已有真实数据](#7-操作-b导入已有真实数据)
   - 7.1~7.10 命令行导入（`scripts/import_data.py`）
   - **7.11 页面导入：前端「上传数据」页** ← 不用共享盘，拖文件夹即可
8. [日常运行](#8-日常运行)
9. [性能与耗时观测](#9-性能与耗时观测)
10. [常见问题排查](#10-常见问题排查)
11. [扩展指南](#11-扩展指南)

---

## 1. 这个项目解决什么问题

整车操稳试验（角阶跃、角脉冲、稳态回转等）会产生大量结构化结果：

- **指标**（标量）：每台车每个工况每次实验，约 10~30 个数值，如 `侧向加速度稳态值 = 0.54 g`
- **曲线**（向量）：每台车每个工况每次实验，约 7~10 条曲线，每条 500 ~ 10 万个采样点

规模目标是 **200 台车 × 20 个工况 × 多次实验**，全量曲线数据可达 **40 GB 量级**。

平台要做的事很朴素：

1. 把算法侧产出的 dict/文件**原样收进来**，不丢信息
2. 支持**多车对比**：同一工况下横向比指标、叠曲线
3. 支持**全量指标检索**：跨车、跨工况查某个指标
4. 每个指标名、每台车、每个工况都能**随时新增，不用改表结构**

---

## 2. 设计思路：为什么是这样

这一节解释几个关键取舍。改代码前先读这里，能避免踩坑。

### 2.1 指标用 EAV 行存储，不给每个指标名建字段

**需求原话是「不要为每个指标名配置字段」。**

指标名有几十上百个，而且算法侧随时可能加。如果建宽表：

```
experiments(id, vehicle_id, ..., 侧向加速度稳态值_g, 横摆角速度峰值, ...)
```

每加一个指标就要 `ALTER TABLE`，200 台车 × 20 工况的宽表迁移在生产上前提是停机。所以走 **EAV（Entity-Attribute-Value）**：

```
indicators(id, experiment_id, name, unit, raw_key, value, ...)
```

一行一个指标。新增指标 = `INSERT` 一行，**零 migration**。

代价是查询要 join，但配合 `ix_indicators_exp_name` 复合索引，取一台车一个工况的全部指标依然是毫秒级（实测 `<10ms`）。

### 2.2 `raw_key` 保留，数字前缀不存

算法侧给的 key 长这样：

```
'13-侧向加速度稳态值(g)'
 │        │          └─ unit
 │        └─ name
 └─ 数字前缀
```

我们**只拆出 name 和 unit 做检索与展示，`raw_key` 原样保留**，保证无损还原。

**数字前缀（seq）刻意不存**，原因有三，都是真实数据里验证过的：

| 问题 | 具体表现 |
|---|---|
| 可能省略 | 真实数据里会出现 `'侧向加速度稳态值(g)'`，没有前缀 |
| 跨工况含义不同 | 同一个「车速」：角阶跃里是 `10`，角脉冲里是 `100` |
| 不唯一 | `17-横摆角速度峰值响应时间` 和 `17-横摆角速度峰值迟滞` 同为 17 |

保留它只会误导排序（无前缀的会被排到最后）。**排序统一按 `name` 字典序**（中文用 `localeCompare('zh')`）。

> 这个结论是用户明确拍板的。历史上有过两版「按数字前缀排序」的方案，都被真实数据推翻了。不要再加回来。

### 2.3 曲线整块存 JSONB，不拆表

**关键前提：曲线只会整体取回来画图，不会按 y 值查询。**

所以拆成 `points(curve_id, x, y)` 是纯粹的浪费——一次画图要 join 几万行。改为一条曲线的完整 payload（`title` / `xlabel` / `ylabel` / `series`）整块存 JSONB：

```python
series = [
  {'x': [...], 'y': [...], 'curvetype': 'line', 'curvename': '原始数据'},
  {'x': [...], 'y': [...], 'curvetype': 'line', 'curvename': '滤波'},
]
```

- 读取是 **O(1) 整行取出**，不用 join，不用按点拼装
- PostgreSQL 的 JSONB 会自动 TOAST 压缩，存储比裸文本小
- 附带存一个 `point_count` 冗余列，避免为了知道点数去解析整个 JSON

### 2.4 实验号是字符串，实验次数不固定

**每台车每个工况的实验次数是不固定的**——可能 1 次、3 次，或者没有平均。

所以：

- `experiment_no` 存**字符串**：`'1'` / `'2'` / `'平均'`，让「平均」自然融入
- 配 `is_average` 布尔 + `sort_order` 整数（平均 = 999），保证排序稳定
- UI 上**只提供「仅平均 / 全部」两种口径**，不做「勾选第一次/第二次」的集合模型——次数一变 UI 就崩

### 2.5 数据种类不写死

曲线里的 `series` 有几种、叫什么名字（`原始数据` / `滤波` / 以后新增的任何种类），**完全由算法侧决定，前端不参与选择**。

一条曲线的全部 series 直接画出来：只有 1 条 → 图例是「车型」；多于 1 条 → 「车型 · 种类」。

> 历史坑：早期版本硬编码了「原始数据 / 滤波」两个选项，且做了「数据种类」选择层。用户明确指出「这层选择没意义」后整个删除。**不要再引入这类 UI。**

### 2.6 过滤下沉到 SQL

分页场景下，如果在前端过滤，`total` 会是错的（前端只看到当前页）。

所有过滤条件（平台、工况、指标名、仅平均）必须在 SQL 的 `WHERE` 里完成，`COUNT` 和 `SELECT` 用同一套条件。

### 2.7 曲线降采样放在前端，用 LTTB

单条曲线可达 10 万点（稳态回转），一页叠 3~10 条就是几十万点。

**先说清楚它省什么、不省什么**——这是最容易搞错的地方：

| 环节 | 降采样有没有用 |
|---|---|
| 后端 SQL 查询 | ✗ 无用（曲线是整块 JSONB，取多少就是多少） |
| 网络传输 | ✗ 无用（降采样在浏览器里做，数据已经下载完了） |
| JSON 解析 | ✗ 无用（同上） |
| 构建 `[x, y]` 点集 + ECharts 建索引 | ✓ 有用，省一个数量级 |
| 渲染 / 内存占用 | ✓ 有用 |

要省传输，只能靠后端的 `exp` / `curves` 参数收敛**取哪些线**；取回来之后一条 10 万点的线，前端降采样解决的是「画不动」。

**为什么是 LTTB（最大三角形三桶法）而不是等间隔抽样**

等间隔抽样（每 N 个取 1 个）会**丢掉尖峰**。试验曲线里峰值恰恰最关键——超调量、峰值响应时间都靠它。实测对照（10 万点抽到 1 万点，埋一个孤立尖峰）：

| 方法 | 结果 |
|---|---|
| LTTB | 尖峰保留 ✓，首末点保留 ✓，y 极值 `2.5` 与原始一致 ✓，耗时 2.4 ms |
| 等间隔抽样 | 尖峰丢失 ✗，y 极值塌到 `1.002` |

算法 O(n)，实测耗时：1 万点 0.1 ms / 5 万点 2.0 ms / 20 万点 1.3 ms / 50 万点 6.2 ms。相对「卡住主线程几百毫秒」的渲染，这个代价可以忽略。

**参数在哪**：`CompareView.vue` 里的常量 `DS_THRESHOLD`（默认 `10000`，`null` = 关闭），
传给 `CurveChart` 的 `downsampleThreshold` prop。

> **界面上没有这个选项**（明确要求去掉，别再加回来）—— 使用者不需要关心这个旋钮。
> 要调整就直接改那个常量。

> 注意别和 ECharts 自带的 `series.sampling` 搞混。那是**渲染层**采样，数据照样全传进 ECharts、照样建索引，解决的只是「线条像素聚合」。我们的降采样在**数据进入 ECharts 之前**做（`CurveChart.vue` 的 `prepared` computed），省的是构建点集和建索引这两块。两者不冲突，可叠加。

### 2.8 大数据量：按「实际负载」治理，而不是按车辆数

「200 台车选稳态回转会不会崩」—— 实测（7 条曲线 × 1 万点，每条 2 个 series）：

| 场景 | gzip | 原始 JSON | 耗时 |
|---|---|---|---|
| 50 车 × 全部实验 × 全部曲线 | 176.8 MB | 357 MB | 11.5 s |
| 50 车 × 平均 × 全部曲线 | 58.0 MB | 118.7 MB | 4.2 s |
| 50 车 × 平均 × 1 条曲线 | 8.6 MB | 18.2 MB | 0.6 s |
| **200 车 × 平均 × 1 条曲线** | 34.3 MB | 72.8 MB | **2.5 s** |
| 200 车 × 平均 × 全部曲线 | 400（拦截） | — | — |
| 200 车 × 全部实验 × 全部曲线 | 400（拦截） | — | — |

结论：**「200 台车」本身不是问题，「200 台车 × 7 条曲线」才是**。
所以准入检查卡的是**估算的采样点总数**（`MAX_COMPARE_NUMBERS`，默认 5000 万），不是车辆数。
按车辆数卡会同时误伤「200 车只画 1 条」（完全可行）和放过「50 车画 7 条」（357 MB）。

服务端序列化的内存放大系数实测 **7.95×**（ORM 对象 + dict + `json.dumps` 字符串 + gzip 缓冲），
5000 万个数字 ≈ 340 MB JSON ≈ 2.7 GB RSS。

#### 三个收敛手段（可叠加）

1. **`curves=`** —— 最有效。曲线名过滤**下推到 SQL**（`selectinload(Experiment.curves.and_(...))`），
   不是拉回来再在 Python 里筛。实测 7 条 → 1 条：76000 点 → 12000 点。
   **为什么必须下推**：以前是在 `_exp_payload` 里过滤，那时 `selectinload` 已经把每条曲线的
   JSONB 都读出来并反序列化成 Python dict 了，只画 1 条也要付全部 7 条的代价。
2. **`exp_scope=avg_first`** —— 每台车只取「平均」，没有平均则取第一次
   （与前端 `expsFor()` 的取线规则一致）。用窗口函数在 SQL 里表达，省 ~2/3。
3. **`meta_only=1`** —— 只返回元信息（车辆 + 指标 + 曲线元信息），**完全不碰 `Curve.series`**。
   实测 200 车 / 稳态回转 = **811 KB / 0.1 s**（对照 357 MB，约 440×）。
   这是「先看菜单再点菜」的基础。
   代价：`curve_meta` 里的 `series_names` 拿不到（数据种类存在 series 里），
   所以 `CurveMeta.series_names` 在 TS 里是可选的。

#### 前端的两阶段加载

`CompareView` 不再一次性把数据全拉下来：

1. **菜单阶段**（`meta_only=1` + `exp_scope`）：车辆列表、指标表、曲线选择区立刻渲染
   （实测 3 台车 **210 ms**，200 车约 0.1 s）。
2. **点菜阶段**（`curves=` 只带勾选的）：只为用户选中的曲线取 series。
   超过 4000 万点/批时**自动分批**，边到边画 —— 200 车 × 7 条会自动拆成 7 批，
   而不是一次性 240 MB 把服务端打爆。

配套的两个细节，别删：

* 勾选变化时**只补拉新勾选的**曲线（已加载过的直接用，取消勾选什么都不做）。
  用 `pendingCurves` 标记在途请求，挡住 watcher 与 `loadData` 的并发重复请求。
* 切口径（仅平均 ↔ 全部实验）会**重新取数**，并用一个「代际」计数器（`loadGen`）
  丢弃过期请求的结果 —— 否则快速切工况时，**上一批**的曲线点集会并进**这一批**的骨架里
  （vehicle_id / experiment_id 可能刚好对得上，数据会静默错乱）。

#### 第二阶段走流式（NDJSON），而不是一次性返回

`/api/compare` 的致命处不在数据量，而在**它必须先把整个响应组装成一个
dict、再 `json.dumps` 成一个几百 MB 的字符串**。实测 50 车 × 7 条 1 万点曲线
（`scripts/verify_stream.py` 可复现）：

| 路径 | 服务端 RSS 峰值增量 | 总耗时 | 首行到达 |
|---|---|---|---|
| `/api/compare` | **794.0 MB** | 6.60 s | —（要等 body 收完） |
| `/api/compare/stream` | **11.6 MB** | 3.71 s | **0.182 s** |

峰值内存差 **68×**，而且前端**第一行到手就能渲染表格和曲线选择区**。

所以前端第二阶段改走 `/api/compare/stream`，返回 `application/x-ndjson`，
一行一个 JSON 对象：

```
{"type":"meta",  ...}   车辆 + 指标 + 曲线元信息（不含 series）
{"type":"curve", ...}   一条曲线的 series，重复 N 次
{"type":"done",  ...}   统计 curves / points / ms
{"type":"error", ...}   生成中的异常（此时 HTTP 200 已经发出去了）
```

服务端按 `STREAM_BATCH_CURVES`（默认 24）条一批取 series，所以**峰值内存与
「选了多少条曲线」无关**。准入检查与 `/api/compare` 共用；超限时返回 400 ——
那时还没开始流，所以是干净的 JSON 错误，不是 `error` 行。

三个实现上的坑，改的时候别踩回去：

* **取 series 必须用 Core 查询**（`select(Curve.id, …, Curve.series)`），
  不能 `select(Curve)`。ORM 会把每条曲线对象塞进 Session 的 identity map
  并**一直持有**，几百条曲线就把省下来的内存又吃回去。骨架转成纯 dict 之后
  也要立刻 `expunge_all()`。
* **流式响应写不了 `X-Server-Time-Ms`** —— 响应头在第一条数据生成之前就发出去了。
  服务端耗时放在 `done` 行的 `ms` 字段里；`timing_middleware` 检测到
  `StreamingResponse` 就直接跳过，免得前端读到「服务端只花了 0.5ms」这种误导。
* **gzip 不会把流缓冲住**（实测过）：开 gzip 时前 3 个数据块在 0.104s 就到，
  identity 是 0.127s —— `GZipMiddleware` 对 `StreamingResponse` 是边压边发的。

#### 流式下前端不能「收一条画一次」

`chartBlocks` 从 computed 改成了手动维护的 `shallowRef`，原因是：

computed 会在 `data` 任何一处变化时给**所有** block 生成新的 `lines` 数组
（哪怕那条曲线的数据根本没变），于是每个 `CurveChart` 的浅 watch 都被触发。
50 车 × 7 条线 × 1 万点 ≈ 350 万个点，一次全量重画要重建所有 ECharts 索引 ——
收一条画一次，流式的收益全被吃光。

所以：`applyCurveEvent` 只把数据挂上骨架并记进 `dirtyBlocks`，按 200ms 节流调
`rebuildDirtyBlocks()`，**只给变过的曲线换新 `lines`**，其余保持同一个引用
→ 不重画。这条也是「`CurveChart` 里 `props.lines` 的 watch 不能 deep」的
另一面：浅比较正是这里的省力点。

⚠️ 顺带一个 Element Plus 的坑：`el-select` 的 multiple 模式会**原地**改
`v-model` 数组（push/splice），引用不变。以前 `chartBlocks` 是 computed
（遍历数组会追踪到内容变化），所以这个坑被掩盖了；改成手动维护后，
`watch(selectedCurves)` **必须加 `{ deep: true }`**，否则「勾了曲线但图不出来」。

#### 前端渲染：别用 `[[x, y], …]`

ECharts 最顺手的写法是 `series.data = [[x, y], …]`，但 V8 里每个两元素小数组要
~56 B 数组头 + 16 B 元素。实测 8 条线 × 10 万点：

| 表示 | 每点驻留 | `setOption` |
|---|---|---|
| `number[][]` | ~520 B/点 | 656 ms |
| 交错 `Float64Array` | ~40 B/点（**约 10×**） | 286 ms（2.3×） |

把 V8 老生代上限压到 300 MB 做对照：`number[][]` 直接 **OOM 崩溃**，
交错 `Float64Array` 正常跑完，且 `dispose()` 后内存完全归还。

用法（`CurveChart.vue`）：

```ts
series: [{
  type: 'line',
  dimensions: ['x', 'y'],          // typed array 源**必须**显式给维度，否则 ECharts 断言失败
  data: interleavedFloat64Array,   // row-major: buf[i*2] = x, buf[i*2+1] = y
}]
```

几个必须知道的点：

* **用 `Float64` 不用 `Float32`。** Float32 只有 ~7 位有效数字，5 万点以上时折线坐标
  就会出现偏差（逐字节比对能测出来）。Float64 的内存仍然只有 `number[][]` 的 1/4.5，
  而渲染结果与原写法**完全一致** —— 多种规模 × 多条线 × 埋尖峰，抽取 SVG 里所有
  path 的 `d` 属性与文本节点逐字节比对通过（`frontend/scripts/verify_equiv.js`）。
* **交错数组是临时的。** ECharts 对 typed array 源会走 `provider.clean()`
  （把内部 `_data` 置空），所以 `setOption` 之后它就被释放了，不会像 `number[][]`
  那样被 ECharts 长期持有。所以**不要**把它缓存进 computed —— 在 `render()` 里
  现构建、交给 ECharts 就丢。
* `progressive` 与 typed array 源兼容（实测无断言错误、渲染一致）。
  注意 `appendData` 只被 `chart.appendData()` API 和 `LinesSeries`（不是 `line`）用到，
  progressive 走的是分块 `each`，不会踩到 typed array 的 `appendData` 断言。
* ECharts 自带的 .d.ts 里 `series.data` 只声明了普通数组，这是**类型声明的缺口**；
  断言集中在 `asSeriesData()` 一个函数里，别把 `as any` 散到 option 各处。

#### `ref` → `shallowRef`，以及去掉 deep watch

响应里有几万个 `number[]`。`ref` 会递归地把它们全包成 Proxy —— 建几万个代理要
几百毫秒到几秒，而且 ECharts 读每个点都要穿过代理。我们从不修改点集，
所以 `data` 用 `shallowRef`，合并完曲线后手动 `triggerRef()` 通知。

同时去掉了 `CurveChart` 里对 `props.lines` 的 `deep: true` watch ——
那会**深度遍历每条曲线的几万个点**，纯浪费。`lines` 由 computed 重建，
引用一定会变，浅比较就够。

#### 分页也要真下推

`get_all_indicators_flat` 以前是「伪分页」：先把 (实验 × 指标) 的笛卡尔展开
（200 车 × 20 工况 × 30 指标 ≈ 35 万行）全读进内存、在 Python 里 groupby 完再切片 ——
**翻到第 2 页和第 1 页一样贵**，而且那份 list 本身就有几百 MB。

现在分两步：先在**实验级 key**（≈1.2 万行）上 `COUNT` / `ORDER BY … LIMIT OFFSET`，
再只为这一页的实验用一次 `IN` 查询取指标。指标名过滤用 `EXISTS` 子查询，
否则 `total` 会把没有该指标的实验也算进去。

---

## 3. 数据模型

### 3.1 表关系

```
platforms  1──N  brands  1──N  vehicles
                                   │
                                   │ 1
                                   N
                              experiments  N──1  conditions
                                   │ 1
                              ┌────┴────┐
                              N         N
                         indicators   curves
```

### 3.2 各表字段

**platforms** — 平台
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(100) unique | 平台名 |

**brands** — 品牌
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(100) | 品牌名 |
| platform_id | FK → platforms | 唯一约束 `(name, platform_id)` |

**vehicles** — 车型
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(200) | 车型名 |
| brand_id | FK → brands | 唯一约束 `(name, brand_id)` |
| drive_type | varchar(50) | 驱动方式 |
| vin | varchar(50) | 车架号 |
| model_year | int | 年款 |
| extra_attrs | JSONB | **扩展属性**（质量、电机、电池等） |

**conditions** — 工况
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| name | varchar(100) unique | 如「角阶跃」 |
| description | text | 描述 |
| category | varchar(100) | 分类：时域 / 频域 / 稳态 |

**experiments** — 实验（一次试验）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| vehicle_id | FK → vehicles | |
| condition_id | FK → conditions | |
| experiment_no | varchar(20) | `'1'` / `'2'` / `'平均'` |
| is_average | bool | 是否平均 |
| sort_order | int | 排序，平均 = 999 |

唯一约束 `(vehicle_id, condition_id, experiment_no)`；复合索引 `(vehicle_id, condition_id, sort_order)`。

**indicators** — 指标（EAV）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| experiment_id | FK → experiments | |
| name | varchar(200) **index** | 从 raw_key 拆出的指标名 |
| unit | varchar(50) | 从 raw_key 拆出的单位 |
| raw_key | varchar(300) | **原样保留，无损还原** |
| value | float | 数值 |
| text_value | varchar(500) | 文本值（备用） |
| category | varchar(100) | 指标分类 |

复合索引：`(experiment_id, name)`、`(experiment_id, raw_key)`。

**curves** — 曲线（JSONB）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| experiment_id | FK → experiments | |
| group | varchar(20) | `'cal'`（计算曲线）/ `'duiqi'`（对齐曲线） |
| name | varchar(200) | 曲线名，如「方向盘转角-时间」 |
| title | varchar(200) | 图标题 |
| xlabel / ylabel | varchar | 轴标签 |
| series | JSONB | `[{x, y, curvename, curvetype}, ...]` |
| point_count | int | 采样点数（冗余，避免解析 JSON） |
| sort_order | int | 曲线顺序 |

索引：`(experiment_id, group, sort_order)`。

### 3.3 跨库兼容

```python
JSONType = JSON().with_variant(JSONB, "postgresql")
```

PostgreSQL 走 JSONB（可索引、可压缩），其他后端（本地 SQLite 开发）退化为普通 JSON。表结构一套代码两端通用。

---

## 4. API 一览

| # | 接口 | 说明 |
|---|---|---|
| 1 | `GET /api/tree?platform=` | 平台 → 品牌 → 车型 目录树 |
| 2 | `GET /api/vehicles/{vid}/conditions/{cid}/data` | 单车 × 工况的指标 + 曲线 |
| 2b | `GET /api/compare?vehicle_ids=1,2,3&condition_id=1` | **多车对比**（页面1） |
| 2c | `GET /api/compare/stream?vehicle_ids=…&condition_id=…` | **多车对比（NDJSON 流式）** ← 前端实际走的 |
| 2d | `POST /api/upload` | **上传数据文件并导入**（multipart，含 dry-run 预览） |
| 3 | `GET /api/indicators/all` | 所有指标（嵌套或分页扁平） |
| — | `GET /api/indicators/count` | 分页总行数 |
| — | `GET /api/conditions` | 工况列表 |
| — | `GET /api/vehicles` | 车辆扁平列表 |
| — | `GET /api/vehicles/{id}/conditions` | 某车有数据的工况 |
| — | `GET /api/curve-names?condition_id=` | 某工况的曲线名列表 |
| — | `GET /api/health` | 健康检查 |

Swagger 文档：`http://localhost:8000/docs`

### 常用查询参数

`/api/compare` 和 `/api/vehicles/{vid}/conditions/{cid}/data`：

| 参数 | 说明 |
|---|---|
| `exp` | 只取指定实验号，逗号分隔（如 `exp=平均`） |
| `curves` | 只取指定曲线名，逗号分隔。**过滤下推到 SQL**，不是拉回来再筛 |
| `exp_scope` | `avg_first` = 每台车只取「平均」（无则第一次），省 ~2/3 |
| `meta_only` | `true` = 只返回车辆 + 指标 + 曲线元信息，不含 `series`（约 440× 更小） |

`/api/compare` 还有一道**准入检查**：估算的采样点总数超过 `MAX_COMPARE_NUMBERS`
（默认 5000 万）时返回 400，并在 `detail` 里给出收敛建议。
`meta_only=1` 不受此限制。详见 [2.8](#28-大数据量按实际负载治理而不是按车辆数)。

> 前端默认就是「两阶段」：先 `meta_only=1&exp_scope=avg_first` 拿菜单，
> 再按用户勾选的曲线取数据 —— 第二阶段走的是 **`/api/compare/stream`**
> （NDJSON 流式，逐行 `{"type":"curve", …}`），所以第一行 `meta` 到手就能
> 渲染，不用等全部曲线到齐。见 [2.8](#28-大数据量按实际负载治理而不是按车辆数)。
>
> 手工看流：`curl -N 'http://localhost:8000/api/compare/stream?vehicle_ids=1,2&condition_id=1&curves=侧倾角-时间'`
> —— `-N` 关掉 curl 自己的输出缓冲，否则看不到逐行到达。

`/api/indicators/all`：

| 参数 | 说明 |
|---|---|
| `platform` / `condition` / `indicator` | 过滤条件（下沉到 SQL） |
| `average` | `true` = 只统计「平均」实验 |
| `flat` | `true` = 分页扁平结构（**大数据量必用**） |
| `limit` / `offset` | 分页参数（在 SQL 里 `LIMIT/OFFSET`，不是切 Python 列表） |

### 返回结构（API 2 / 2b）

```json
{
  "condition_name": "角阶跃",
  "curve_meta": {
    "groups": {
      "cal": {
        "series_names": ["原始数据", "滤波"],
        "curves": {
          "方向盘转角-时间": {
            "title": "方向盘转角vs时间",
            "xlabel": "时间（s）",
            "ylabel": "方向盘转角（deg）",
            "point_count": 500,
            "series_names": ["原始数据", "滤波"]
          }
        }
      }
    }
  },
  "vehicles": [
    {
      "vehicle_name": "极光EV-01",
      "experiments": [
        {
          "experiment_id": 1,
          "experiment_no": "1",
          "is_average": false,
          "params": {
            "13-侧向加速度稳态值(g)": {
              "name": "侧向加速度稳态值",
              "unit": "g",
              "raw_key": "13-侧向加速度稳态值(g)",
              "value": 0.54
            }
          },
          "curves_cal": {
            "方向盘转角-时间": {
              "title": "...", "xlabel": "...", "ylabel": "...",
              "series": [{"x": [], "y": [], "curvename": "原始数据"}]
            }
          },
          "curves_duiqi": {}
        }
      ]
    }
  ]
}
```

---

## 5. 环境准备

### 5.1 依赖

```bash
pip install -r requirements.txt
```

核心：FastAPI + SQLAlchemy 2.0 + psycopg2。可选加速：`orjson`（大响应序列化快 2~4 倍）、`numpy`（造数快 5~10 倍）、`msgpack`（读 msgpack 数据时才需要）。

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
# Using        : PostgreSQL  →  ...
```

忘了 PG 密码可以用 `reset.py`（原理：临时改 `pg_hba.conf` 为 trust，改完还，不丢数据）：

```bash
python reset.py --password 123456     # Windows 需管理员权限
```

### 5.3 建表

```bash
python scripts/init_db.py
```

---

## 6. 操作 A：生成演示数据

适用于：**手上还没有真实数据，想先把平台跑起来看效果。**

### 6.1 一条命令

```bash
python scripts/seed_data.py --preset tiny
```

输出：

```
Seed complete  (24.0s)
  Platforms   : 4
  Brands      : 8
  Vehicles    : 11
  Conditions  : 5
  Experiments : 165
  Indicators  : 3399
  Curves      : 2310
  JSON files  : 55  →  ./data  (0.06 GB)
```

这条命令会**先清空所有表再重建**，并同时在 `data/` 下导出 55 个 JSON 文件。

### 6.2 规模预设

| preset | 车辆 | 工况 | 点数 | 用途 |
|---|---|---|---|---|
| `tiny` | 11 | 5 | 500 | 快速验证（~25 秒） |
| `demo` | 20 | 8 | 2000 | 默认，日常开发 |
| `standard` | 60 | 15 | 5000 | 性能测试 |
| `full` | 200 | 20 | 10000（稳态 10 万） | 压力测试，**很慢** |

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
| `--runs` | 每台车每工况实验次数（**不含平均**），默认 2 |
| `--points` | 普通工况采样点数 |
| `--steady-points` | 稳态回转的采样点数（一般远大于普通工况） |
| `--no-json` | 不导出 `data/*.json` |
| `--seed` | 随机种子 |

> 生成的 JSON 结构就是下面的「写法 A」，可以拿来当**导入格式的参考样例**。

---

## 7. 操作 B：导入已有真实数据

适用于：**算法侧已经产出了数据，要灌进平台。**

### 7.1 目录约定

```
real_data/
├── 极光EV-01/              ← 文件夹名 = 车型名
│   ├── 角阶跃.msgpack      ← 文件名 = 工况名
│   ├── 角脉冲.msgpack
│   └── 稳态回转.msgpack
├── 星河EV-01/
│   └── …
├── vehicles.csv            ← 可选，车辆元信息
└── conditions.csv          ← 可选，工况分类
```

**一台车一个文件夹，一个工况一个文件。**

**只有数据文件是必需的**；两个 CSV 都可选，但不给就没法准确定位平台/品牌。

#### 先看一份能跑的完整示例

不想自己拼格式的话，直接生成一份最小示例：

```bash
python scripts/make_example_data.py --out ./example_data
python scripts/import_data.py --data-dir ./example_data --dry-run
```

产出（2 台车 × 2 工况 × 3 份文件，含两种写法）：

```
example_data/
├── vehicles.csv                  ← 3 行（比实际车多，多出来的是没数据的车，会被忽略）
├── conditions.csv                ← 角阶跃 / 角脉冲
├── 示例车A/
│   ├── 角阶跃.json               写法 A：{"1": [...], "2": [...], "平均": [...]}
│   └── 角脉冲.json               写法 A：只有 "1" "2"，没有平均
└── 示例车B/
    └── 角阶跃.json               写法 B：{"params":…, "curves_cal":…, "curves_duiqi":…}
```

`vehicles.csv` / `conditions.csv` 的内容见 7.4 节，数据文件的完整片段见 7.3 节。

跑 `--dry-run` 应该看到：

```
车型 2 台 / 工况 2 个 / 车×工况文件 3 份
将写入：实验 6 次 / 指标 42 行 / 曲线 36 条
车辆元信息来自: ./example_data\vehicles.csv（3 台）
```

> **注意「车辆元信息来自 …（3 台）」和「车型 2 台」不一致是正常的**：
> CSV 里列了几台车不影响导入，只有**实际有数据文件**的车型才会入库
> （`vehicles` 表按扫描到的文件夹建，不是按 CSV 建）。CSV 只是给这些车型补属性。
>
> 反过来，**数据文件夹里的车型如果不在 CSV 里**，也能导进去 ——
> 品牌按车型名前缀猜，平台落「默认平台」。所以 CSV 漏了几行不会导致导入失败，
> 只会让那几台车的元信息不准。

### 7.2 文件格式：JSON 或 msgpack

**后缀决定怎么读，内容结构完全一致**——解出来都是同一个 dict，走同一套解析逻辑。

| 后缀 | 读取方式 | 需要额外装库 |
|---|---|---|
| `.json` | 标准 JSON（utf-8，可带 BOM） | 不需要 |
| `.msgpack` `.msg` `.mpack` | msgpack | `pip install msgpack` |

```bash
pip install msgpack                                     # 一次性
python scripts/import_data.py --data-dir ./real_data    # 后缀自动识别
python scripts/import_data.py --data-dir ./real_data --ext .msgpack   # 只读 msgpack
```

目录里 JSON 和 msgpack 混放也能一起导。

> **msgpack 的中文 key 坑**：msgpack 在 0.5→1.0 之间改过 str/bytes 语义，老版本 dump 出来的中文 key 是 bytes，直接读会得到 `b'\xe8\xa7\x92...'` 这种乱码 key，导致指标名全废。脚本里已做自动解码（`_decode_msgpack_keys`），会递归把 bytes key/value 解成 str；**但 float 数组刻意不碰**——曲线点动辄几十万个，逐个转换会显著拖慢导入。

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
    "curve": [                                   # ← 键名也可以是 curves / series
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

### 7.4 元信息（可选，但强烈建议）

先回答常见疑问：**数据文件（json/msgpack）里只有指标和曲线，没有任何车辆属性。**

`角阶跃.json` 里找不到「这台车是什么平台、什么品牌、什么驱动方式」——
因为算法侧只负责算结果，不负责描述车。这些属性只能另外给。

**没有元信息时**，平台会按车型名前缀猜品牌（`极光EV-01` → `极光`），其余落到「默认平台 / 默认品牌」。猜得不准，建议提供。

#### `vehicles.csv` — 车辆属性

放在数据目录根部，文件名固定（或用 `--vehicles-csv` 指定）。**表头中英文都认。**

```csv
车型,平台,品牌,驱动方式,车架号,年款
示例车A,EPA1,星河,后驱,LGWEF6E40PF000001,2024
示例车B,EPA2,星河,四驱,LGWEF6E40PF000002,2025
示例车C,EPA2,云驰,前驱,LGWEF6E40PF000003,2025
```

| 列 | 别名 | 落库位置 | 缺省 |
|---|---|---|---|
| 车型 | 车型名 / vehicle / name | `vehicles.name`，**必须和数据文件夹名一模一样** | — |
| 平台 | platform | `platforms.name`（新平台自动建） | `默认平台` |
| 品牌 | brand | `brands.name`（挂在平台下，同名品牌跨平台算两个） | `默认品牌` |
| 驱动方式 | 驱动 / drive_type | `vehicles.drive_type` | 空 |
| 车架号 | vin | `vehicles.vin` | 空 |
| 年款 | model_year | `vehicles.model_year`（只取前 4 位转 int） | 空 |

> **「车型」这一列是关联键**，写错一个字这台车就匹配不上 CSV，会退化成猜品牌。
> 最稳的做法是先在数据目录里 `ls` 一遍文件夹名，直接复制过来。

因为层级是 平台 → 品牌 → 车型，**同一个品牌名在不同平台下是两条记录**。
上表里 `示例车B` / `示例车C` 同属 EPA2 平台但品牌不同，会生成两个品牌。

#### `conditions.csv` — 工况分类

文件名固定（或用 `--conditions-csv` 指定）。

```csv
name,category,description
角阶跃,操纵性,方向盘阶跃输入下的瞬态响应
角脉冲,操纵性,方向盘脉冲输入下的频率响应
稳态回转,稳态,固定半径稳态圆周
```

| 列 | 别名 | 落库位置 | 缺省 |
|---|---|---|---|
| name | 工况 | `conditions.name`，**必须和数据文件名（去后缀）一致** | — |
| category | 分类 | `conditions.category` | 空 |
| description | 说明 | `conditions.description` | 空 |

`category` 会**同时写进该工况下所有指标的 `indicators.category`**，
所以它是前端「按分类筛指标」的依据，建议填（见 3.2 节）。

> 不给 `conditions.csv` 也能导 —— 工况名从文件名拿，
> 只是 `category` / `description` 为空，前端少一层分类筛选。


### 7.5 指标 key 的写法

推荐 `序号-指标名(单位)`，但**序号可以省略**，三种都认：

| key | name | unit |
|---|---|---|
| `13-侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `13-侧向加速度稳态值` | 侧向加速度稳态值 | *(空)* |
| `侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `侧向加速度稳态值` | 侧向加速度稳态值 | *(空)* |

分隔符支持 `-` `.` `_` `、`；单位取最外层括号。

> **序号不存库**（见 2.2 节）。它只是被解析掉，`name` 和 `unit` 才是检索用的。

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
  车型 11 台 / 工况 5 个 / 车×工况文件 55 份
  将写入：实验 165 次 / 指标 3399 行 / 曲线 2310 条
  车辆元信息来自: ./real_data/vehicles.csv（11 台）

--dry-run：未写库。样例：
  极光EV-01 / 角阶跃 → 实验号 ['1', '2', '平均']
```

**重点核对这三个数字**：车型数、实验次数、指标行数。对不上说明结构没被正确识别，往下导就是错数据。

```bash
# 第 2 步：正式导入
python scripts/import_data.py --data-dir ./real_data
```

### 7.8 ⚠️ 清空 vs 追加

| 命令 | 行为 |
|---|---|
| `python scripts/import_data.py --data-dir ./data` | **先 `DROP ALL` 清空**，再导入 |
| `python scripts/import_data.py --data-dir ./data --append` | 保留其他数据，**同一台车同一工况按本次内容覆盖** |

**默认是清空。** 导入真实数据前想保住演示数据，就加 `--append`；否则演示数据随时能用 `seed_data.py --preset tiny` 重建（~25 秒），丢了也不心疼。

`--append` 是**按「车 × 工况」覆盖**，不是简单追加：同一个 (车型, 工况) 再导一次，
会先删掉它原有的实验再写新的。两个原因：

1. `experiments` 上有 `uq_exp_vehicle_condition_no` 唯一约束，简单追加遇到同一
   实验号会直接 `IntegrityError`；
2. 「重新上传一次同一份数据」本来就是覆盖语义，不该报错。

层级（平台/品牌/车型/工况）在 append 时**复用已有的**，不会重复插；
车辆属性（驱动方式/车架号/年款）和工况分类/说明会以 CSV 为准补齐，
CSV 没给的字段保持原值（不会被 `None` 覆盖掉）。

### 7.9 全部参数

| 参数 | 说明 |
|---|---|
| `--data-dir` | 数据目录（默认 `./data`） |
| `--vehicles-csv` | 车辆元信息 CSV（默认找 `<data-dir>/vehicles.csv`） |
| `--conditions-csv` | 工况分类 CSV |
| `--ext` | 只认指定后缀，逗号分隔 |
| `--append` | 按「车 × 工况」覆盖而不是清空 |
| `--dry-run` | 只预览，不写库 |

### 7.10 导完怎么验证

```bash
# 查各表数量
python -c "
from sqlalchemy import create_engine, text
from app.config import DATABASE_URL
e = create_engine(DATABASE_URL)
with e.connect() as c:
    for t in ('vehicles','conditions','experiments','indicators','curves'):
        print(t, c.execute(text('select count(*) from '+t)).scalar())
"
```

再打开前端或 Swagger 抽查一台车的数据：
`http://localhost:8000/api/compare?vehicle_ids=1&condition_id=1&exp=平均`

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
长出两套行为：平均实验怎么识别、`raw_key` 怎么拆、CSV 用哪一列关联车型，
一旦对不上很难发现）。

实现上有三个坑，都在这页/这个接口里处理掉了：

| 坑 | 症状 | 处理 |
|---|---|---|
| 浏览器 multipart **只带 `filename`**，`webkitRelativePath` 不进请求体 | 服务端只收到一堆光秃秃的 `角阶跃.json`，不知道属于哪台车 | 前端把路径列表当普通 form 字段 `paths`（JSON 数组）一起发 |
| 根目录该不该剥，**要看全量路径才能判断** | 剥多了车型名没了；剥少了车型名变成 `data`，更糟的是 `data/示例车A/x.json` 和 `data/示例车B/x.json` 会撞成同一台车 → 实验号重复 → `IntegrityError` | 只在前端「原样发送」，由 `importer.strip_common_root` 统一判断（**优先以 `vehicles.csv` 所在目录为根**）。规则只有一处 |
| 先把所有文件读完再解析 | 曲线数据几百 MB，原始文本 + 解析结果同时驻留，峰值翻倍 | `importer.ItemBuilder` 逐个读、解析完立刻丢原始字节；前端直接把 `File` 塞 FormData 交给浏览器流式发 |

请求/响应（`multipart/form-data`）：

| 字段 | 说明 |
|---|---|
| `files` | 数据文件（`.json` / `.msgpack` / `.msg` / `.mpack`）+ 可选的 `vehicles.csv` / `conditions.csv` |
| `paths` | 与 `files` 顺序一致的相对路径 JSON 数组，如 `["极光EV-1/角阶跃.json","vehicles.csv"]` |
| `mode` | `replace`（默认，先 drop_all）/ `append`（按「车 × 工况」覆盖） |
| `dry_run` | `true` = 只解析预览，一个字都不写库 |

返回 `summary`（将写入什么）+ `imported`（实际写入，`dry_run` 时为 `null`）+
`errors`（**单份文件解析失败只进这里，不中断整次导入**）+ `skipped`（后缀不认识的）。
体积上限 `MAX_UPLOAD_MB`（默认 512，env 可调），超了返回 413。

> 验证：`python scripts/verify_upload.py`（36 项断言，用**临时 SQLite 库**，
> 不会碰 PG）+ `bash frontend/scripts/run_e2e_upload.sh`（20 项断言，
> 真实 Chromium 里真的选文件夹、预览、导入）。
> e2e 脚本会走 `mode=replace`，所以它**强制**后端跑在 SQLite 上，检测到 PG 会直接中止。


---

## 8. 日常运行

### 8.1 一键启动

```bash
./start.sh          # Linux / macOS
start.bat           # Windows
```

脚本会自动判断：`.env` 里有 `DATABASE_URL` → 用 PG，**跳过自动初始化**（避免往生产库灌 tiny 数据）；否则用 SQLite 且库不存在时自动建表灌数据。

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
| `MAX_COMPARE_NUMBERS` | `50000000` | 单次 `/api/compare` 允许的估算采样点总数，超了返回 400。见 [2.8](#28-大数据量按实际负载治理而不是按车辆数) |
| `STREAM_BATCH_CURVES` | `24` | 流式端点每批取多少条曲线的 series。这是**内存**旋钮（调大省 SQL 往返，调小降峰值），不是速度旋钮 |
| `MAX_UPLOAD_MB` | `512` | `POST /api/upload` 单次上传体积上限，超了返回 413 |
| `TIMING` | `1` | 耗时埋点开关，`0` 关闭 |
| `PORT` | `8000` | 后端端口 |

### 8.4 前端页面

页头只有三个菜单项，**没有品牌文案**（标题和「FastAPI + Vue3 + ECharts」这类
技术栈说明都去掉了 —— 使用者不需要看这些）。

**页面 1（对比分析）**

- 左侧目录树多选车辆（可搜索、全选、按平台选）
- 选工况 → 加载对比。**两阶段加载**：先出指标表和曲线选择区（约 0.2 s），
  曲线数据随后**流式**到达
- 曲线区只有两个选择：**工况 + 曲线组**（不写死数据种类）
- 「曲线」多选：默认全选；取消勾选不删数据，重新勾选时若已加载过则**不再发请求**
- 「同图取线」：平均优先 / 全部实验叠加。**切到「全部实验」会重新取数**（数据量翻 3 倍）
- 指标表格：行 = 指标，列 = 车 × 实验；口径只有「仅平均 / 全部」。
  切到「全部实验」同样会重新取数

> **界面上刻意不显示的几样东西**（都是明确要求去掉的，别再加回来）：
>
> - **负载预估行**（「N 台车 × M 条曲线 × P 点 ≈ X 个采样点」「已全部加载」
>   「将分 N 批加载」）。准入检查在服务端做，超限会直接返回 400 并把建议写在
>   `detail` 里，不需要在界面上预先吓唬用户。
> - **降采样开关**。降采样本身还在（LTTB，见 [2.7](#27-曲线降采样放在前端用-lttb)），
>   但参数变成了 `CompareView.vue` 里的常量 `DS_THRESHOLD`（默认 10000，`null` = 关闭），
>   要调就改这个常量。
> - **耗时数字**。只在 Console 打 `[timing]` 日志，不占界面（见 [9.4](#94-前端日志)）。

> **流式期间不边收边画**（`CompareView.vue` 的 `LIVE_RENDER = false`）。
> 一次 `setOption` 要把该图**所有**线的数据重建索引：25 车 × 7 曲线 × 1 万点
> = 350 万点，单张图约 350 ms。边收边画意味着这个代价要付好几遍 ——
> 实测长任务 6 个 / 合计 1607 ms，用户感觉就是「一直在卡」；改成每个流式
> 请求结束画一次后降到 3 个 / 863 ms。**卡的根因是同一个 setOption 付了多遍，
> 不是点数多。**

**页面 2（指标统计）**

- 平台 / 工况 / 指标名过滤 + 口径（仅平均 / 全部）
- 服务端分页（200 车 × 20 工况下约 35 万行，必须分页；
  `LIMIT/OFFSET` 在 SQL 里做，不是切 Python 列表）
- 可导出 CSV

**页面 3（上传数据）**

- 拖文件夹进来，或点「选择文件夹」（`webkitdirectory`）
- 实时显示：文件数、数据文件数、元信息 CSV、体积、识别出的车型文件夹
- 「预览（不写库）」→ 看解析摘要；「开始导入」→ `replace` 会弹二次确认
- 结果区：各表条数 + 元信息 CSV 来源 + 车型列表 + 解析失败/被跳过的文件
- 详见 [7.11](#711-页面导入前端上传数据页)

- 平台 / 工况 / 指标名过滤 + 口径（仅平均 / 全部）
- 服务端分页（200 车 × 20 工况下约 35 万行，必须分页；
  `LIMIT/OFFSET` 在 SQL 里做，不是切 Python 列表）
- 可导出 CSV

---

## 9. 性能与耗时观测

### 9.1 为什么需要

页面上那个「加载完成 X.Xs」**不是数据库耗时**——它是 `performance.now()` 从点击到数据赋值完成，中间混着网络传输、JSON 解析、Vue 响应式赋值和 ECharts 渲染。要定位瓶颈必须拆开。

### 9.2 后端：打日志

每个请求往 **stderr** 打两行（`app/timing.py`）：

```
[timing] compare        db=0.023s  total=0.023s  vehicles=2      ← 业务层：纯 SQL
[timing] /api/compare   server=0.035s  total=0.035s              ← 中间件层：含路由+序列化
```

`TIMING=0` 可整体关闭（变 no-op，零开销）。

已埋点的端点：`/api/compare`、`/api/vehicles/{vid}/conditions/{cid}/data`、`/api/indicators/all`、`/api/indicators/count`。

给新端点埋点的方式：

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

前端 axios 读 `X-Server-Time-Ms` 得到「服务端」那一段的精确值。

> **跨域注意**：浏览器默认只暴露 6 个 simple response header，自定义头**必须**写进 CORS 的 `expose_headers`，否则前端读到 `undefined`。已在 `app/main.py` 配置好。开发时走 Vite 代理是同源，测不出这个问题；一旦前端直连 `:8000` 就会哑。

### 9.4 前端：控制台日志

前端不再把耗时画到界面上，而是打到浏览器 Console：

```
[timing] compare  服务端=23ms  网络=12ms  解析=8ms  渲染=145ms  合计=188ms
[timing] indicators  服务端=55ms  网络=3ms  解析=6ms  渲染=20ms  合计=84ms  行数=200
```

四段的含义：

| 段 | 含义 | 测法 |
|---|---|---|
| 服务端 | 后端 SQL + 序列化 | 读响应头 `X-Server-Time-Ms` |
| 网络 | 发出请求到收完响应体（含 gzip 解压） | axios 响应拦截器 |
| 解析 | `JSON.parse` 把响应体变成 JS 对象 | 同上 |
| 渲染 | Vue 赋值 + ECharts / el-table 真正画完 | `await nextTick()` 之后测 |

**渲染必须等 `nextTick()` 之后测**，否则测的是「还没画完」的时间。

### 9.5 实测参考值（tiny 数据）

| 场景 | 服务端 | 网络 | 解析 | 渲染 |
|---|---|---|---|---|
| 3 车对比（445 KB, gzip） | 117 ms | 4 ms | 18 ms | 61 ms |
| 指标分页（393 KB） | 55 ms | 3 ms | 6 ms | 20 ms |
| 目录树（0.5 KB） | 100 ms | 2 ms | 3 ms | 8 ms |

**注意**：3 车对比的服务端 117 ms = 纯 SQL 63 ms + **序列化 54 ms**。序列化占了近一半——真要优化，换 `orjson` 比调 SQL 见效快（装了就自动生效，无需改代码）。

小接口首次请求的 100 ms 是冷启动开销，后续会降到 2~6 ms。

### 9.6 降采样耗时（实测）

LTTB 在浏览器主线程跑，纯计算，无需网络：

| 输入点数 | 抽到 1 万点耗时 |
|---|---|
| 10,000 | 0.1 ms |
| 50,000 | 2.0 ms |
| 200,000 | 1.3 ms |
| 500,000 | 6.2 ms |

**结论**：即使 50 万点也只要 6 ms，相对它省下的（构建 50 万个 `[x,y]` 数组 + ECharts 建索引 + 渲染，通常是几百毫秒到秒级）完全值得。**不值得为它做 worker 离线计算或节流**，同步跑就好。

对比参照：同样 10 万点、同样抽到 1 万点，**等间隔抽样 0 ms 但丢尖峰**，LTTB 2.4 ms 且保住尖峰。这 2.4 ms 是买「峰值不被削平」的钱。

### 9.7 规模实测（200 车 / 16 工况 / 7 条 1 万点曲线）

用 `python scripts/bench_compare.py` 复现（会自动起一个临时后端，跑完自己关掉）。

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

**内存放大系数 7.95×**：每车 7.15 MB JSON → 服务端 RSS 涨 56.8 MB，严格线性。
外推 180 车 = 1.29 GB JSON / 10.2 GB RSS —— 16 GB 本机必 OOM。这就是准入检查存在的理由。

**流式端点 vs 一次性返回**（`python scripts/verify_stream.py`，同一个
50 车 × 平均 × 全部 7 条曲线的场景）：

| 路径 | 服务端 RSS 峰值增量 | 总耗时 | 首行到达 |
|---|---|---|---|
| `/api/compare` | 794.0 MB | 6.60 s | —（要等 body 收完） |
| `/api/compare/stream` | **11.6 MB** | 3.71 s | **0.182 s** |

差 **68×**：老路径要把整个响应 `json.dumps` 成一个 118 MB 的字符串，再叠上
gzip 的输出缓冲；流式只持有「一批 24 条曲线」，序列化完就丢。

**gzip 不会把流缓冲住**（实测）：开 gzip 时前 3 个数据块在 0.104s 到达，
`identity` 是 0.127s —— `GZipMiddleware` 对 `StreamingResponse` 是边压边发的。
所以不需要为了流式关掉 gzip。

**前端渲染表示**（8 条线 × 10 万点，`frontend/scripts/verify_hold.js`）：

| 表示 | 每点驻留 | `setOption` |
|---|---|---|
| `number[][]` | ~520 B/点 | 656 ms |
| 交错 `Float64Array` | ~40 B/点 | 286 ms |

把 `--max-old-space-size=300` 加上做对照：`number[][]` **OOM 崩溃**，
交错 `Float64Array` 正常完成，`dispose()` 后内存回到基线。

**降采样 vs 不降采样**：降采样只省「构建点集 + 建索引 + 渲染」，
**不省网络与解析** —— 那两块只能靠 `curves=` / `exp_scope` / `meta_only` 收敛。

---

## 10. 常见问题排查

### 导入时提示「结构无法识别」

`parse_payload()` 认不出你的 dict 形状。常见原因：

- 曲线组的键名不是 `curve` / `curves` / `series`（三选一）
- 顶层实验号是浮点 `1.0` 而不是字符串 `'1'`
- params 和曲线被包在别的层级里

处理方式：用 `--dry-run` 看哪个文件被跳过，然后 `python -c "import msgpack; print(msgpack.unpack(open('x.msgpack','rb'), raw=False, strict_map_key=False))"` 打出结构，对照 7.3 节调整格式，或改 `parse_payload()`。

### 指标名出现乱码 / `b'\xe8\xa7\x92...'`

msgpack 的 bytes key 没被正确解码。正常情况下 `_decode_msgpack_keys()` 会自动处理，如果还出问题说明编码不是 utf-8。手动检查：

```bash
python -c "
import msgpack
d = msgpack.unpack(open('文件.msgpack','rb'), raw=False, strict_map_key=False)
print(type(list(d)[0]), repr(list(d)[0]))
"
```

### 导入后曲线画不出来 / 没有数据

检查 `curves.point_count` 是否为空。老数据可能缺这列：

```bash
python -c "
from scripts.import_data import recalc_curve_meta
class A: pass
recalc_curve_meta(A())
"
```

重新导入一遍也会自动重算。

### `DuplicateTable: 关系 "ix_indicators_name" 已经存在`

`Indicator.name` 的 `index=True` 已经自动生成 `ix_indicators_name`，不要在 `__table_args__` 里重复声明同名索引。

### 忘记 PostgreSQL 密码

```bash
python reset.py --password 新密码
```

Windows 需管理员权限。

### 前端报错但后端日志正常

大概率是 CORS 或代理配置。确认 `frontend/vite.config.ts` 的 proxy 指向正确的后端端口。

### 加了大指标后页面很卡

按收益从大到小：

- 用 `?curves=` 只取要画的曲线（**最有效**，7 条 → 1 条能省一个数量级）
- 用 `?exp_scope=avg_first` 只取平均实验（省 2/3）
- 页面 2 必须用 `flat=true` 分页
- 确认对比页的「降采样」开关是开的（默认开，阈值 10000）
- 看 Console 的 `[timing]` 日志确认瓶颈在服务端还是渲染

### 请求返回 400「响应过大」

准入检查拦下的。`detail` 里已经列出了收敛建议，按它做即可：

```
① 加 curves= 只取要画的曲线（最有效，1 条曲线能省一个数量级）
② 加 exp_scope=avg_first 只取平均实验（省 ~2/3）
③ 先用 meta_only=1 拿元信息再按需取数据
④ 减少车辆数
⑤ 调大环境变量 MAX_COMPARE_NUMBERS
```

注意**不是**按车辆数拦的：200 台车只画 1 条曲线完全没问题（实测 34 MB / 2.5 s），
而 50 台车画 7 条曲线是 357 MB。详见 [2.8](#28-大数据量按实际负载治理而不是按车辆数)。

前端正常情况下不会撞到这个 400 —— 它默认走两阶段加载（第二阶段是流式），
并且会按 4000 万点/批拆成多个请求。撞到基本说明手写请求时没收敛。

### 曲线图是空白的（有坐标轴没线）

按顺序查：

1. **第二阶段（点菜）还没回来** —— 图上有「曲线数据加载中…」遮罩。等它消失。
2. **该曲线的 `series` 是空数组** —— `meta_only=1` 的响应里 `series` 恒为空，
   如果只发了 meta 请求没发数据请求，就是这个现象。
3. **`curves=` 的名字写错了** —— 过滤下推到 SQL 后，名字对不上就一条都查不出来，
   而且不会报错。用 `GET /api/curve-names?condition_id=` 确认准确名字。
4. **流里返回了 `error` 行** —— 生成过程中出错时 HTTP 200 已经发出去了，
   服务端只能把错误当成一行数据发回来，前端会把它抛成异常并弹提示。
   在 Network 面板打开那条 `application/x-ndjson` 请求，看最后一行。

### 流式响应没有「边到边」到达，像是一次性到齐

`/api/compare/stream` 是按行推的，正常情况下第一行 `meta` 几十毫秒就到。
如果看起来是一次性到齐：

1. **中间有反向代理在缓冲** —— nginx 默认 `proxy_buffering on` 会把流攒起来。
   加 `proxy_buffering off;`（或让响应带 `X-Accel-Buffering: no`）。
2. **手工用 curl 看时忘了 `-N`** —— curl 自己的输出缓冲会让它看起来是一次性输出。
3. **数据本来就小** —— 3 台车 × 7 条曲线两秒内就跑完了，本来就「看不出」渐进。
   要看效果得用 50 台车以上的量级。

另外，**流式的服务端耗时读 `done` 行的 `ms` 字段**，不要读 `X-Server-Time-Ms` ——
流式响应压根不带这个头（响应头在第一条数据生成前就发出去了）。

### 降采样开了但曲线形状明显变了 / 峰值被削平

把「降采样」阈值调大（或关掉开关）对比看一次。

- 阈值调大后形状恢复 → 是阈值太小，抽点太狠。10000 点是「屏幕 1000 像素宽」的 10 倍，通常足够。
- 关了开关还是不对 → 问题不在降采样，看数据本身（`curves.series` 里 x 是否单调、y 是否缺段）。
- 开关关闭后会顺带解除 `ChartLine` 的抽点，但**内存压力会回来**，多车叠 10 万点线时可能卡住浏览器。

---

## 11. 扩展指南

| 场景 | 做法 |
|---|---|
| 新增指标名 | `INSERT indicators` 一行，**零 migration** |
| 新增曲线 | `INSERT curves` 一行，**零 migration** |
| 新增工况 | `INSERT conditions` 一行 + 在 seed 里加指标模板 |
| 新增车辆属性 | 常用的加列；临时的放 `extra_attrs` JSONB |
| 加分类层级 | `platforms` 加 `parent_id` 改自引用树，或加中间维度表 |
| 新增曲线组 | 加 `curves.group` 的新取值，前端从 `curve_meta.groups` 自动发现 |
| 新增数据格式 | 在 `import_data.py` 的 `load_data_file()` 加后缀分支 |
| 换降采样算法 | 改 `frontend/src/utils/downsample.ts`，`CurveChart.vue` 只认 `downsampleIfNeeded()` 的返回结构 |
| 调整准入上限 | 环境变量 `MAX_COMPARE_NUMBERS`（后端）/ `BUDGET_NUMBERS`（前端分批，`CompareView.vue`） |
| 调流式批量 / 重绘频率 | 环境变量 `STREAM_BATCH_CURVES`（后端每批几条曲线）/ `FLUSH_MS`（前端重绘节流，`CompareView.vue`） |
| 加新的收敛参数 | 在 `_load_experiments*` 的 `select()` 上加条件，**别在 Python 里过滤** |

### 改代码前的五条硬规矩

1. **不要给指标名建字段** —— 用户明确要求
2. **不要存数字前缀（seq）** —— 已被真实数据三次推翻
3. **过滤必须下沉 SQL** —— 否则分页 total 会错，而且白付 I/O 与反序列化
4. **大数组别用 `[[x, y], …]`** —— 用交错 typed array，且别让 computed 长期持有它
5. **流式端点里查重列要用 Core 查询** —— ORM 的 identity map 会把曲线对象一直攥着，
   把流式省下的内存又吃回去（实测 68× 的收益就是这么做出来的）

---

## 附：文件清单

```
vehicle-test-db/
├── app/
│   ├── config.py        数据库 URL 解析（PG / SQLite 切换）
│   ├── database.py      engine / session / Base
│   ├── models.py        ORM 模型（6 张表）
│   ├── schemas.py       Pydantic 响应模型
│   ├── crud.py          所有查询逻辑（负载估算 + 流式生成器 iter_compare_stream）
│   ├── importer.py      导入逻辑：解析（JSON/msgpack/CSV）+ 写库（CLI 与 /api/upload 共用）
│   ├── timing.py        分段耗时埋点
│   └── main.py          FastAPI 应用 + 路由 + 负载准入检查 + 流式端点 + 上传导入
├── scripts/
│   ├── init_db.py       建表
│   ├── seed_data.py     生成演示数据（4 档预设）
│   ├── import_data.py   导入真实数据（JSON / msgpack）—— 薄壳，逻辑在 app/importer.py
│   ├── make_example_data.py  生成最小示例数据（对照导入格式用）
│   ├── verify_optimize.py    对真库验证 crud 改动（只读）：口径/下推/懒加载/分页
│   ├── verify_stream.py      验证流式端点：等价性 / RSS 峰值 / 首行延迟 / gzip 是否缓冲
│   ├── verify_upload.py      验证上传导入接口（临时 SQLite 库，不碰 PG）
│   └── bench_compare.py      各收敛手段的 HTTP 体积与耗时基准
├── frontend/            Vue 3 + TS + Element Plus + ECharts
│   ├── src/
│   │   ├── api/         axios 封装 + 耗时采集 + streamCompare + uploadData
│   │   ├── types/       TS 类型
│   │   ├── utils/       downsample.ts（LTTB 降采样）
│   │   ├── components/  目录树 / 曲线图（交错 Float64Array 渲染）
│   │   └── views/       对比分析页 / 指标统计页 / 上传数据页
│   ├── scripts/
│   │   ├── verify_equiv.js     证明 typed array 与原写法渲染逐字节一致
│   │   ├── verify_hold.js      测内存驻留（含 --max-old-space-size 对照）
│   │   ├── verify_progressive.js  progressive 与 typed array 的兼容性 + setOption 耗时
│   │   ├── e2e_compare.cjs      对比分析页端到端（断言 canvas 里确实画出了线）
│   │   ├── e2e_upload.cjs       上传数据页端到端（真选文件夹 → 预览 → 导入 → 查库）
│   │   ├── run_e2e.sh           起前后端 → 跑对比页 e2e → 全关掉（一条命令）
│   │   └── run_e2e_upload.sh    同上，跑上传页 e2e（**强制临时 SQLite**，会 drop_all）
│   └── e2e-shots/       最近一次端到端验证的截图
├── .bak-optimize/       本轮改动前的 crud.py / main.py / 两个 .vue 备份（无 git，保险用）
├── reset.py             重置 PG 密码
├── start.sh / start.bat 一键启动
├── requirements.txt
└── README.md            精简版说明（本文件是详细版）
```
