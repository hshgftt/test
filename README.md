# 整车试验数据平台

**后端** FastAPI + SQLAlchemy 2.0 + PostgreSQL / SQLite ｜ **前端** Vue3 + TS + ECharts + Element Plus

> **📖 完整设计思路与操作手册见 [DESIGN.md](./DESIGN.md)**
> 本文件是精简版（快速开始 + 结构速查）。要了解「为什么这样设计」「怎么导真实数据」
> 「怎么排查问题」，看 DESIGN.md。

> **Python 3.8 可用**：后端全部用 `typing.List/Dict/Optional`，没有 `list[...]` / `X | Y` 这类
> 3.9+ 语法。已在 Python 3.8.16 上实测（fastapi 0.124 / uvicorn 0.33 / sqlalchemy 2.0.54 /
> pydantic 2.10 / numpy 1.24 / orjson）。

## 快速开始

```bash
# ── 后端 ──
pip install -r requirements.txt
cp .env.example .env          # 默认 SQLite；已有 .env 请按需改

# ① 想看演示数据（造数）
python scripts/init_db.py     # 建表 + 幂等写入默认元数据
python scripts/seed_data.py --preset tiny   # 生成数据 + 导出 data/*.json

# ② 想导入【真实数据】—— 见下文「导入真实数据」一节
python scripts/import_data.py --data-dir ./data --dry-run   # 先预览
python scripts/import_data.py --data-dir ./data             # 正式导入

# 启动（等价写法任选一种）
python -m app.main            # ★ 推荐，main.py 里已写好 __main__
uvicorn app.main:app --reload --port 8000
# Swagger: http://localhost:8000/docs

# 懒人版：首次运行且用 SQLite 时自动 init_db + seed_data 再启动
#   检测到 .env 用 PostgreSQL 时会【跳过】自动初始化，避免污染生产库
./start.sh                    # Linux / macOS
start.bat                     # Windows

# ── 前端 ──
cd frontend
npm install                   # 或 pnpm install
npm run dev                   # http://localhost:5173
```

> 所有命令都在**项目根目录**下执行（`python -m app.main` 依赖 `app` 包可被导入）。

## 核心概念

平台是**元数据驱动**的：车辆字段、导航树层级、数据格式都来自配置表，不是写死的列。

| 概念 | 表 | 说明 |
|---|---|---|
| 车辆字段注册表 | `attr_defs` | 每个字段一行。新增字段 = INSERT 一行，**不改表结构、不改代码** |
| 维度字典 | `dim_entries` | 同一维度内可多层（品牌→子品牌、平台→子平台），车辆只引用最深节点 |
| 导航树配置 | `tree_profiles` | 只声明「层从哪来、顺序如何」；树在运行时按车辆实际数据投影，不落库 |
| 数据格式注册 | `dataset_types` | 操稳 / 平顺（预留）/ 通用；新格式 = INSERT 一行 |
| 分析单元 segment | `vehicles` + `vehicle_modes` | `(车, 模式)`；树叶子是 segment，不同车模式数量可以不同 |
| 数据信封 | `datasets` | 一条数据一行，统一承载任意格式 |
| 指标 / 曲线 | `metric_values` / `curves` | 标量指标一行一条；曲线一条一行，`series` 整块 JSONB |

导航树层级可以任意增减、任意排序。默认配置是
`平台 → 子平台 → 品牌 → 子品牌 → 车 → 试验模式`，缺失字段的层会自动跳过；
另有 `brand_first`（品牌优先）和 `flat`（车 → 模式）两套配置，
也可以 `POST /api/tree-profiles` 新增。

## 导入真实数据

不用造数脚本，把你自己的试验数据按下面的目录结构放好即可：

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

* **一台车一个文件夹，一个工况一个文件**；
* 车名取文件夹名（去掉结尾括号里的单号），**全局唯一**；
* 中间目录都是试验模式，按 `/` 拼接；模式数量完全由目录决定，不需要提前配置；
* 一台车可以有多个操稳单号（CSV 里用 `;` `,` `|` `、` 分隔）。

```bash
# 先预览解析结果（不写库）
python scripts/import_data.py --data-dir ./real_data --dry-run

# 正式导入（默认清空现有数据；加 --append 则按 slot 覆盖）
python scripts/import_data.py --data-dir ./real_data
```

`--dry-run` 应输出「车辆 N 台 / 模式 M 种 / 工况 K 个 / 文件 J 份」和
「将写入：数据集 / 指标 / 曲线」。**重点核对这几个数字**，对不上说明结构没被正确识别。

> **不想敲命令？** 打开前端 → 页面「上传数据」→ 把文件夹拖进去 →
> 先「预览」再「开始导入」。解析和入库规则与命令行**完全共用**
> （`app/importer.py`），结果一致。详见 [DESIGN.md 7.11](./DESIGN.md)。

### 文件格式：JSON 或 msgpack

| 后缀 | 读取方式 |
|---|---|
| `.json` | 标准 JSON（utf-8，可带 BOM） |
| `.msgpack` `.msg` `.mpack` | msgpack（需 `pip install msgpack`） |

两种格式**内容结构完全一致**，走同一套解析逻辑。msgpack 里残留的 bytes key
会自动解成 str，不用手工清洗；目录里两种格式混放也能一起导。

### 数据的两种写法

**写法 A（推荐，与算法侧 dict 一致）** — 顶层 key 是实验号：

```python
{
  "1":    [params1,    curves_cal1,    curves_duiqi1],
  "2":    [params2,    curves_cal2,    curves_duiqi2],
  "平均": [paramsAvg,  curves_calAvg,  curves_duiqiAvg],
}
```

**写法 B（扁平）** — 顶层直接是 params：

```python
{"params": {...}, "curves_cal": {...}, "curves_duiqi": {...}}
```

曲线的 `curve` 里每一项是 `{x, y, curvetype, curvename}`；
`curvename` 缺失会自动补 `series0 / series1 …`。
实验号 `平均`（也接受 `avg` / `average` / `AVE` / `AVE.`）会自动识别成
`is_average=true` + `sort_order=999`。

> 认不出的结构不会报错，整块进 `datasets.payload`（`generic` 用法）。
> 以后新增数据格式，只需在 `dataset_types` 加一行，再补一个解析分支。

### 车辆 / 工况元信息（可选，强烈建议）

数据文件里只有指标和曲线，**没有车辆属性**（平台、品牌、驱动形式等只能另外给）。

`real_data/vehicles.csv`（表头中英文都认，**任意列**）：

```csv
车名,平台,品牌,子品牌,驱动形式,VIN号,生产日期,能源形式,操稳单号,试验模式
示例车A,EPA1,星河,,后驱,LGWEF6E40PF000001,2024-03-01,纯电,CS-2024-001,舒适;运动
示例车B,EPA2,星河,星锐,四驱,LGWEF6E40PF000002,2025-01-15,纯电,CS-2024-002;CS-2024-018,舒适
示例车C,EPA2,云驰,,前驱,LGWEF6E40PF000003,2025-02-20,插电混动,,舒适;运动;赛道
```

| 列 | 说明 |
|---|---|
| 车名 / 车型 / vehicle / name | **关联键**，必须和数据文件夹名一致 |
| 模式 / 试验模式 / modes / test_modes | 多个用 `; , \| 、` 分隔 |
| 操稳单号 / 平顺单号 / order | 可多个 |
| 其它任意列 | 按 `attr_defs.code` 或 `attr_defs.name` 匹配 |

**匹配不到的列会自动注册成新字段**（按列值推断 `text` / `number`）。
CSV 直接加一列「整备质量」，导入后就成为可筛选、可展示的车辆字段，
不需要改表、不需要改代码。

`real_data/conditions.csv`：

```csv
name,category,description
角阶跃,操纵性,方向盘阶跃输入下的瞬态响应
角脉冲,操纵性,方向盘脉冲输入下的频率响应
```

| 列 | 别名 | 落库位置 |
|---|---|---|
| `name` | 工况 | `conditions.name`，**必须和数据文件名（去后缀）一致** |
| `category` | 分类 | `conditions.category`，并写进该工况下所有指标的 `category` |
| `description` | 说明 | `conditions.description` |

注意事项：

* **`vehicles` 表按扫描到的文件夹建，不是按 CSV 建。** CSV 里多列几台没数据的车无妨；
  漏了几台有数据的车也不报错，只是那几台属性为空。
* **「车名」列是关联键**，写错一个字就匹配不上。最稳的做法是 `ls` 数据目录复制文件夹名。

### 先看一份能跑的完整示例

```bash
python scripts/make_example_data.py --out ./example_data
python scripts/import_data.py --data-dir ./example_data --dry-run
```

`example_data/` 含 2 台车 × 2 工况 × 3 份文件，两种写法都有；
`--dry-run` 应输出「车辆 2 台 / 工况 2 个 / 文件 3 份」。

### 指标 key 的写法

推荐 `序号-指标名(单位)`，但**序号可以省略**，四种都认：

| key | name | unit |
|---|---|---|
| `13-侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `13-侧向加速度稳态值` | 侧向加速度稳态值 | — |
| `侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `侧向加速度稳态值` | 侧向加速度稳态值 | — |

> 数字前缀**不入库**：它可能没有，跨工况含义也不一致（「车速」在角阶跃是 10、
> 在角脉冲是 100），且不唯一。前端统一按 `name` 排序，`raw_key` 无损保留原始 key。

### 数据库地址怎么改

配置集中在 `app/config.py`，从 `.env` 或真实环境变量读取，优先级从高到低：

| 优先级 | 变量 | 示例 | 说明 |
|---|---|---|---|
| 1 | `DATABASE_URL`（**真实环境变量**） | `postgresql+psycopg2://u:p@db:5432/vt` | Docker / CI / 生产用，覆盖一切 |
| 2 | `SQLITE_PATH` | `SQLITE_PATH=./dev.db` | 本地开发首选，相对/绝对路径都行 |
| 3 | `USE_SQLITE` | `USE_SQLITE=1` | 开关形式，默认落在 `./vehicle_test.db` |
| 4 | `DATABASE_URL`（来自 `.env`） | `postgresql+psycopg2://postgres:postgres@localhost:5432/vehicle_test` | 兜底 |

```bash
# 本地 SQLite（默认，.env 里已配好）
SQLITE_PATH=./dev.db

# 切回 PostgreSQL：注释掉 SQLITE_PATH / USE_SQLITE，写 DATABASE_URL
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/vehicle_test
```

随时自检当前连的是哪个库：

```bash
python -m app.config
# DATABASE_URL = sqlite:///./dev.db
# IS_SQLITE    = True
```

> 代码里 JSON 列用的是 `JSON().with_variant(JSONB, "postgresql")`——
> PostgreSQL 下走 JSONB（可建 GIN 索引、TOAST 压缩），SQLite 下退化为 JSON，
> 同一套模型两边都能建表。

### 启动参数

`HOST` / `PORT` / `RELOAD` 三个环境变量可覆盖（仅 `python -m app.main` 生效）：

```bash
PORT=8080 RELOAD=0 python -m app.main
```

其他常用变量：`GZIP`（默认 1）、`MAX_COMPARE_NUMBERS`（默认 5000 万）、
`STREAM_BATCH_CURVES`（默认 24）、`MAX_UPLOAD_MB`（默认 512）、`TIMING`（默认 1）。

## 数据库设计

```
配置层（只加数据，不改表结构）
  attr_defs       字段注册表
  dim_entries     维度字典（同一维度内可多层，parent_id 自引用）
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

### 表结构

| 表 | 关键字段 |
|---|---|
| **attr_defs** | `code`, `name`, `value_type`, `dim_code`, `unit`, `options`, `sort_order`, `is_active` |
| **dim_entries** | `dim_code`, `name`, `parent_id`, `path`, `sort_order`；唯一 `(dim_code, parent_id, name)` |
| **tree_profiles** | `code`, `name`, `levels`(JSON), `is_default` |
| **dataset_types** | `code`, `name`, `description`, `is_active` |
| **vehicles** | `id`, `name`(**全局唯一**), `created_at` |
| **vehicle_attr_values** | `vehicle_id`, `attr_id`, `value_text` / `value_num` / `value_dim_id` |
| **vehicle_modes** | `vehicle_id`, `mode`, `sort_order`；唯一 `(vehicle_id, mode)` |
| **test_orders** | `vehicle_id`, `discipline`(操稳/平顺), `order_no` |
| **conditions** | `name`(唯一), `category`, `description` |
| **datasets** | `vehicle_id`, `mode_id`(可空), `test_order_id`(可空), `condition_id`, `dataset_type_id`, `name`, `exp_no`, `is_average`, `sort_order`, `payload`(JSON) |
| **metric_values** | `dataset_id`, `name`, `unit`, `raw_key`, `value`, `text_value`, `category` |
| **curves** | `dataset_id`, `group`(`cal`/`duiqi`), `name`, `title`, `xlabel`, `ylabel`, `series`(JSON), `point_count`, `sort_order` |

`datasets` 的唯一约束 `(vehicle_id, mode_id, condition_id, dataset_type_id, name, exp_no)`
保证同一 slot 只存一条，重复导入即覆盖。

### 设计要点

**① 车辆字段：注册表 + EAV** — `attr_defs` 定义字段，`vehicle_attr_values` 存值。
新增字段 = INSERT 一行，零 migration。维度字段存 `value_dim_id`（指向 `dim_entries`），
非维度字段存 `value_text` / `value_num`，索引齐全，按字段筛选仍然走 SQL。

**② 导航树：运行时投影** — `tree_profiles.levels` 只声明层级（`source=attr/vehicle/test_mode`
+ `key` + `missing` 策略）。树节点按车辆实际数据生成，不落库。改层级顺序只改 JSON，
不改代码；子品牌、子平台这类可选层缺失时会自动跳过。

**③ 分析单元 segment** — 树叶子是 `(vehicle_id, mode_id)`，mode 可空。
同一台车的舒适 / 运动是两个独立叶子，对比接口也按 segment 取数。

**④ 数据格式：统一信封** — `datasets` 一条数据一行，`dataset_type_id` 决定怎么解释；
无法归入指标/曲线的结构进 `payload`。新增格式不改表结构。

**⑤ 指标 EAV 行存储** — 不为每个指标名建字段。原始 key 拆成 `name` / `unit`，
同时保留 `raw_key` 无损还原。新指标 = INSERT 一行，零 migration。

**⑥ 曲线一条一行 + JSONB** — 前端「曲线查询只会全拿出来绘制，不会查到 y 这种」，
因此 `series`（含 x/y/curvetype/curvename）整块存 JSONB；
但**一条曲线一行**而不是整个数据集一行，这样 `curves=` 过滤、`meta_only`、
流式分批都能下推到 SQL，不会为了取一条曲线反序列化几十 MB。

**⑦ 实验号用字符串** — `exp_no` 存 `'1'/'2'/'平均'`，配合 `is_average` +
`sort_order`（平均 = 999）保证排序稳定：数字实验在前，平均在最后。

## API

| 方法 | 接口 | 说明 |
|---|---|---|
| GET | `/api/meta` | 前端所需全部配置：字段、维度、树配置、数据格式、工况、模式建议 |
| GET / POST | `/api/attrs` | 字段注册表；**新增车辆字段不改表结构**（修改走 `PUT /api/attrs/{id}`） |
| POST / PUT | `/api/tree-profiles` | 新增 / 修改导航树层级配置 |
| GET | `/api/tree?profile=&f_<code>=` | 按配置投影出任意层级的导航树 |
| GET / POST / PUT / DELETE | `/api/vehicles…` | 车辆列表 / 详情 / 新增 / 修改 / 删除 |
| GET | `/api/segments/{vid}/conditions/{cid}/data` | 单个 segment × 工况的指标 + 曲线 |
| GET | `/api/compare?segments=…` | 多 segment 对比（一次性返回） |
| GET | `/api/compare/stream?segments=…` | 多 segment 对比（NDJSON 流式，GET 版） |
| **POST** | **`/api/compare/stream`** | **推荐**，曲线名等中文参数放 body |
| GET | `/api/indicators/all` | 指标统计（GET 版，调试用） |
| **POST** | **`/api/indicators/all`** | **推荐**，统计筛选放 body |
| GET | `/api/indicators/count` | 统计总行数 |
| POST | `/api/upload` | 上传数据文件并导入（multipart，支持 dry-run 预览） |
| GET | `/api/conditions` `/api/curve-names` `/api/indicator-names` | 辅助查询 |
| GET | `/api/health` | 健康检查 |

**segment 写作 `vehicle_id:mode_id`**，多个用逗号分隔：

```
segments=1:2,1:3,5        # 1 号车的舒适模式、运动模式；5 号车无模式
```

公共收敛参数：`dataset_type`、`exp`（如 `exp=平均`）、`curves`（过滤下推 SQL）、
`exp_scope=avg_first`、`meta_only=1`。

> **为什么推荐 POST？** 曲线名、品牌名都是中文。GET 把中文放在 query 里，
> 经反向代理百分号编码后可能触发 uvicorn 的 `400 Invalid HTTP request`。
> 前端对含中文的接口一律走 POST + JSON body，GET 版只用于调试。

统计接口 POST body：

```json
{
  "filters": {"brand": "星河", "energy_type": "纯电"},
  "condition_id": 3,
  "indicator": "侧向加速度稳态值",
  "average": true,
  "dataset_type": "handling_stability",
  "limit": 200,
  "offset": 0
}
```

`filters` 的 key 是任意 `attr_defs.code`。

## 前端

```
frontend/src/
├── api/          axios / fetch 封装（含 NDJSON 流式读取）
├── types/        TS 类型（对齐后端响应）
├── components/
│   ├── VehicleTree.vue   导航树：按 tree_profiles 动态渲染，叶子是 segment
│   └── CurveChart.vue    ECharts 折线（多车叠加、缩放、tooltip）
└── views/
    ├── CompareView.vue   页面1：对比画图 + 指标表格
    ├── StatsView.vue     页面2：全部指标统计表格（可导出 CSV）
    └── UploadView.vue    页面3：拖文件夹上传导入（支持预览）
```

**页面1（对比分析）**
- 左侧目录树按 `tree_profiles` 动态渲染，可搜索、可切换层级配置
- **叶子是 segment（车 × 模式）**，同一台车的舒适 / 运动可分别勾选
- 选工况 + 数据格式 → 两阶段加载：先出指标表和曲线选择区，曲线数据随后**流式**到达
- 指标表格：行 = 指标，列 = segment × 实验
- 界面上**不显示**负载预估行、降采样开关、耗时数字（都在代码里 / Console）

**页面2（指标统计）**
- 按工况 + 任意车辆字段（动态来自 `meta.attrs`）+ 实验号过滤
- 表格：车 | 模式 | 各动态属性 | 实验 | 各指标列（左侧列固定、横向滚动）
- 支持导出 CSV

**页面3（上传数据）**
- 拖文件夹进来或点「选择文件夹」，识别车目录 + 模式目录 + 元信息 CSV
- 「预览（不写库）」看解析摘要 →「开始导入」（清空模式会二次确认）
- 结果区显示各表条数、车辆/模式列表、解析失败/被跳过的文件

## 生成的数据

`scripts/seed_data.py` 用二阶系统模型生成物理自洽的信号：
方向盘阶跃/脉冲/正弦输入 → 侧向加速度、横摆角速度、侧倾角、侧倾角速度、前后轴侧偏角，
**指标由对应曲线计算得出**（响应时间、超调量、稳态增益等），因此指标与曲线互相吻合。

演示数据刻意做成**不同车辆模式数量不固定**：有的无模式、有的只有「舒适」、
有的「舒适 + 运动」；部分车辆带一个或多个操稳单号，部分没有单号。

## 规模化

### 预设

```bash
python scripts/seed_data.py --preset tiny       #  11 车 ×  5 工况   500点 / 稳态    5000 点    ~0.1 GB
python scripts/seed_data.py --preset demo       # 200 车 × 20 工况  1000点 / 稳态   10000 点    ~5 GB
python scripts/seed_data.py --preset standard   # 200 车 × 20 工况  3000点 / 稳态   30000 点    ~15 GB
python scripts/seed_data.py --preset full       # 200 车 × 20 工况  6000点 / 稳态  100000 点    ~40 GB
```

也可以逐个覆盖：

```bash
python scripts/seed_data.py --vehicles 200 --conditions 20 \
       --points 5000 --steady-points 100000 --runs 2
python scripts/seed_data.py --preset full --no-json   # 只写库不导 JSON，快很多
python scripts/seed_data.py --preset tiny --seed 42   # 固定随机种子，可复现
```

> 造数直接走 `importer.import_items`，和真实导入共用同一套解析/落库规则，
> 不会出现「造数能查、导入查不到」的两套行为。

### 体积怎么算

每条曲线 = 7 张图 × (原始 + 滤波) × 2 数组，每个数字 JSON 约 7 字节：

```
单次实验体积 ≈ 点数 × 21(数组) × 2(x,y) × 7 字节
```

| 点数 | 单次实验 | 200车 × 20工况 × 3次实验 |
|---|---|---|
| 1 000 | 0.29 MB | ~3 GB |
| 5 000 | 1.5 MB | ~15 GB |
| 10 000 | 2.9 MB | ~30 GB |
| 100 000 | 29 MB | ~290 GB（仅稳态回转 1 个工况 ≈ 12 GB） |

**结论**：200 车 × 20 工况 × 全部 5000~10000 点 + 稳态回转 10 万点 ≈ **40 GB 量级**。
本地开发用 `demo` / `standard`；上生产用 PostgreSQL（JSONB + TOAST 压缩）；
真要 40 GB 全量，把长曲线改成二进制 float32 存储（体积和解析时间都能再降 3~5 倍）。

### 读路径怎么保证不崩

| 手段 | 说明 |
|---|---|
| `curves=` **过滤下推 SQL** | 只拉要画的曲线，不是拉回来再筛；7 条 → 1 条能省一个数量级 |
| `meta_only=1` | 只取元信息，约 440× 更小；指标表和曲线选择区立刻出来 |
| `exp_scope=avg_first` | 每台车只取「平均」（无则第一次），省 ~2/3 |
| **NDJSON 流式** | 先发 `meta` 行，再按 `STREAM_BATCH_CURVES`（24）条一批逐行发 series；峰值内存从「整个响应」降到「一条曲线」 |
| **两阶段加载** | 页面 1 先拿菜单（车辆 + 指标 + 曲线名），用户勾完再取数据 |
| 准入检查 | `/api/compare` 估算采样点总数，超过 `MAX_COMPARE_NUMBERS`（5000 万）返回 400 + 收敛建议。按点数卡，不是按车辆数 |

实测（50 车 × 平均 × 7 条 1 万点曲线，`python scripts/verify_stream.py`）：
服务端 RSS 峰值增量 **794 MB → 11.6 MB（68×）**，首行 **0.182 s**
（一次性端点要等 6.6 s 把 body 收完）。

### 前端曲线降采样（LTTB）

超过阈值的曲线在**进入 ECharts 之前**抽点，默认开、阈值 **10000 点**。

* 算法是 **LTTB**（最大三角形三桶法），不是等间隔抽样 —— 等间隔会把尖峰抽没，
  而峰值（超调量、峰值响应时间）恰恰是试验曲线最要紧的信息；
* 代码在 `frontend/src/utils/downsample.ts`，耗时 O(n)，50 万点约 6 ms；
* `CurveChart` 的 `downsampleThreshold` prop 控制，`0` / `null` = 关闭；
* **它不省网络传输**（降采样发生在浏览器里，数据已经下载完了），省的是构建点集 +
  ECharts 建索引 + 渲染内存。要省传输只能靠 `exp` / `curves` 参数少取线。

### 前端渲染：大数组不要用 `[[x, y], …]`

ECharts 最顺手的 `series.data = [[x, y], …]` 在 V8 里每个点要一个小数组
（~72 B/点）。实测 8 条线 × 10 万点：`number[][]` 驻留 ~520 B/点，
换成交错 `Float64Array` 后 ~40 B/点（**约 10×**），`setOption` 也快 2.3×。

用法是 `dimensions: ['x','y']` + row-major 交错数组（`buf[i*2]=x, buf[i*2+1]=y`）。
用 `Float64` 而不是 `Float32`：后者只有 ~7 位有效数字，5 万点以上坐标就会有偏差。

### 耗时埋点：慢在哪一段

页面上那个「加载完成 X.Xs」**不是数据库耗时** —— 它是 `performance.now()` 从点击到
数据赋值完成，里面混着网络传输、`JSON.parse`、Vue 响应式赋值和 ECharts 渲染。
要定位瓶颈必须把链路拆开。

**后端（`app/timing.py`）** 每请求往 stderr 打两行，并把结果塞进响应头：

| 响应头 | 含义 |
|---|---|
| `X-Server-Time-Ms` | 服务端总耗时，毫秒（如 `34.7`） |
| `Server-Timing` | 标准格式，`total=0.035; server=0.035` |

`TIMING=0` 可整体关闭。**流式请求没有 `X-Server-Time-Ms`** —— 响应头先于数据发出，
服务端耗时放在最后一行的 `ms` 字段，前端自动读取。

**前端**耗时打到浏览器 Console 里（不占界面），分「服务端 / 网络 / 解析 / 渲染 / 合计」：

```
[timing] compare  服务端=23ms  网络=12ms  解析=8ms  渲染=145ms  合计=188ms
```

看 Console 就能判断瓶颈在数据库还是在渲染。

> 更细的设计说明与完整操作手册见 **[DESIGN.md](./DESIGN.md)**。

## 扩展指南

| 场景 | 做法 |
|---|---|
| 新增车辆字段 | `POST /api/attrs` 插一行；或直接在 `vehicles.csv` 加一列（自动注册） |
| 新增一层导航树 | 改 `tree_profiles.levels`（`POST/PUT /api/tree-profiles`），零代码 |
| 调整层级顺序 / 数量 | 同上；默认配置已有「平台优先」「品牌优先」「平铺」三套 |
| 某层级缺失时怎么办 | 该层 `missing: "skip"`（跳过）或 `missing: "label"`（占位） |
| 新增试验模式 | 直接建模式文件夹，或在 CSV 的「试验模式」列写；不需要配置 |
| 一台车多个单号 | CSV 里用 `;` / `,` / `\|` / `、` 分隔，自动拆成多行 |
| 新增工况 | 放一个新的 JSON 文件即可；分类/描述在 `conditions.csv` 补 |
| 新增指标 | 数据文件里加一个 key；INSERT `metric_values`，零 migration |
| 新增曲线 | 数据文件里加一条 `{curve: [...], title, xlabel, ylabel}`，零 migration |
| 新增数据格式 | `dataset_types` 加一行 + `importer` 补一个解析分支；其它格式自动进 `payload` |
| 新增维度节点 | 导入时传名称自动建；`dim_entries` 支持父子多层 |
