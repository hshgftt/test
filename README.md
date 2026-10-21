# 车辆操稳试验数据分析平台

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
python scripts/init_db.py     # 建表
python scripts/seed_data.py   # 生成数据 + 导出 data/*.json（默认 demo 预设）

# ② 想导入【真实数据】—— 见下文「导入真实数据」一节
python scripts/import_data.py --data-dir ./data --dry-run   # 先预览
python scripts/import_data.py --data-dir ./data             # 正式导入

# 启动（等价写法任选一种）
python -m app.main            # ★ 推荐，main.py 里已写好 __main__
python app/main.py
uvicorn app.main:app --reload --port 8000
python -m uvicorn app.main:app --reload --port 8000
# Swagger: http://localhost:8000/docs

# 懒人版：首次运行会自动 init_db + seed_data 再启动
#   注意：检测到 .env 用 PostgreSQL 时会【跳过】自动初始化，避免污染生产库
./start.sh                    # Linux / macOS
start.bat                     # Windows

# ── 前端 ──
cd frontend
pnpm install
pnpm dev                      # http://localhost:5173
```

> 所有命令都在**项目根目录**下执行（`python -m app.main` 依赖 `app` 包可被导入）。

## 导入真实数据

不用造数脚本，把你自己的试验数据按下面的目录结构放好即可：

```
data/
├── 极光EV-1/
│   ├── 角阶跃.json
│   ├── 角脉冲.json
│   └── 稳态回转.json
└── 星河EV-01/
    └── …
```

**一台车一个文件夹，一个工况一个文件。** 然后：

```bash
# 先预览解析结果（不写库）
python scripts/import_data.py --data-dir ./data --dry-run

# 正式导入（默认会清空现有数据；加 --append 则按「车 × 工况」覆盖）
python scripts/import_data.py --data-dir ./data
```

> **不想敲命令？** 打开前端 → 顶部菜单「📤 上传数据」→ 把文件夹拖进去 →
> 先「预览」再「开始导入」。解析和入库规则与命令行**完全共用**
> （`app/importer.py`），结果一致。详见 [DESIGN.md 7.11](./DESIGN.md)。

### 文件格式：JSON 或 msgpack

**算法侧常见的 msgpack 落盘，直接指过去就行**，不用先转 JSON：

| 后缀 | 读取方式 |
|---|---|
| `.json` | 标准 JSON（utf-8，可带 BOM） |
| `.msgpack` `.msg` `.mpack` | msgpack（需 `pip install msgpack`） |

```bash
pip install msgpack                                    # 一次性
python scripts/import_data.py --data-dir ./real_data   # 后缀自动识别
python scripts/import_data.py --data-dir ./real_data --ext .msgpack   # 只读 msgpack
```

两种格式**内容结构完全一致**（解出来都是同一个 dict，走同一套解析逻辑）。
脚本会把 msgpack 里可能残留的 bytes key（老版本 dump 出来的中文 key）
自动解成 str，不用手工清洗。目录里 JSON 和 msgpack 混放也能一起导。

### 数据的两种写法都支持

**写法 A（推荐，与算法侧 dict 一致）** — 顶层 key 是实验号：

```json
{
  "1":    [params1,    curves_cal1,    curves_duiqi1],
  "2":    [params2,    curves_cal2,    curves_duiqi2],
  "平均": [paramsAvg,  curves_calAvg,  curves_duiqiAvg]
}
```

**写法 B（扁平）** — 顶层直接是 params：

```json
{ "params": {...}, "curves_cal": {...}, "curves_duiqi": {...} }
```

曲线的 `curve` 里每一项是 `{x, y, curvetype, curvename}`；
`curvename` 缺失会自动补 `series0 / series1 …`，不影响展示。
实验号除了 `1/2/3…`，`平均`（也接受 `avg` / `average`）会被自动识别成
`is_average=true` + `sort_order=999`。

### 车辆 / 工况元信息（可选，但建议提供）

**数据文件里只有指标和曲线，没有车辆属性。** `角阶跃.json` 里找不到「这台车什么平台、
什么品牌、什么驱动方式」—— 算法侧只算结果，不描述车。这些属性只能另外给。

在 `data/` 下放两个 CSV，比自动推断准确得多：

`data/vehicles.csv`（表头中英文都认）：

```csv
车型,平台,品牌,驱动方式,车架号,年款
示例车A,EPA1,星河,后驱,LGWEF6E40PF000001,2024
示例车B,EPA2,星河,四驱,LGWEF6E40PF000002,2025
```

| 列 | 别名 | 落库位置 |
|---|---|---|
| 车型 | vehicle / name | `vehicles.name`，**必须和数据文件夹名一致** |
| 平台 | platform | `platforms.name`（新平台自动建） |
| 品牌 | brand | `brands.name`（挂在平台下） |
| 驱动方式 | drive_type | `vehicles.drive_type` |
| 车架号 | vin | `vehicles.vin` |
| 年款 | model_year | `vehicles.model_year` |

`data/conditions.csv`：

```csv
name,category,description
角阶跃,操纵性,方向盘阶跃输入下的瞬态响应
角脉冲,操纵性,方向盘脉冲输入下的频率响应
```

| 列 | 别名 | 落库位置 |
|---|---|---|
| name | 工况 | `conditions.name`，**必须和数据文件名（去后缀）一致** |
| category | 分类 | `conditions.category`，并写进该工况下所有指标的 `category` |
| description | 说明 | `conditions.description` |

注意事项：

* **CSV 里的车型数和实际入库的车型数可以不一致。** `vehicles` 表按**扫描到的文件夹**建，
  不是按 CSV 建。CSV 里多列几台没数据的车无所谓；漏了几台有数据的车也不会报错，
  只是那几台退化成「猜品牌 + 默认平台」。
* **「车型」列是关联键**，写错一个字就匹配不上。最稳的做法是 `ls data/` 复制文件夹名。
* 因为层级是 平台 → 品牌 → 车型，**同名品牌在不同平台下算两条记录**。

没有 `vehicles.csv` 时会按车型名前缀推断品牌（`极光EV-01` → `极光`），
其余落到「默认平台 / 默认品牌」。

### 先看一份能跑的完整示例

不想自己拼格式，直接生成一份最小示例（2 台车 × 2 工况，两种写法都有）：

```bash
python scripts/make_example_data.py --out ./example_data
python scripts/import_data.py --data-dir ./example_data --dry-run
```

产出结构：

```
example_data/
├── vehicles.csv                  ← 3 行车辆属性
├── conditions.csv                ← 角阶跃 / 角脉冲
├── 示例车A/
│   ├── 角阶跃.json               写法 A：{"1": […], "2": […], "平均": […]}
│   └── 角脉冲.json               写法 A：只有 "1" "2"，没有平均
└── 示例车B/
    └── 角阶跃.json               写法 B：{"params":…, "curves_cal":…, "curves_duiqi":…}
```

`--dry-run` 应输出：

```
车型 2 台 / 工况 2 个 / 车×工况文件 3 份
将写入：实验 6 次 / 指标 42 行 / 曲线 36 条
车辆元信息来自: ./example_data\vehicles.csv（3 台）
```

（CSV 里 3 台、实际入库 2 台是正常的，见上面注意事项。）

### 指标 key 的写法

指标 key 推荐 `序号-指标名(单位)`，但**序号可以省略**，三种都认：

| key | name | unit |
|---|---|---|
| `13-侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `侧向加速度稳态值(g)` | 侧向加速度稳态值 | g |
| `侧向加速度稳态值` | 侧向加速度稳态值 | — |

> 数字前缀（如 13）**不入库**。它可能没有，跨工况含义也不一致
> （「车速」在角阶跃是 10、在角脉冲是 100），且不唯一。
> 前端统一按 `name` 排序。


### 数据库地址怎么改

配置集中在 `app/config.py`，从 `.env` 或真实环境变量读取，优先级从高到低：

| 优先级 | 变量 | 示例 | 说明 |
|---|---|---|---|
| 1 | `DATABASE_URL`（**真实环境变量**） | `DATABASE_URL=postgresql+psycopg2://u:p@db:5432/vt` | Docker / CI / 生产用，覆盖一切 |
| 2 | `SQLITE_PATH` | `SQLITE_PATH=./dev.db` | 本地开发首选，相对/绝对路径都行 |
| 3 | `USE_SQLITE` | `USE_SQLITE=1` | 开关形式，默认落在 `./vehicle_test.db` |
| 4 | `DATABASE_URL`（来自 `.env`） | `DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/vehicle_test` | 兜底 |

```bash
# 本地 SQLite（默认，.env 里已配好）
SQLITE_PATH=./dev.db

# 切回 PostgreSQL：注释掉 SQLITE_PATH / USE_SQLITE，写 DATABASE_URL
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/vehicle_test

# 不装 psycopg2 也能连 PG：pip install psycopg[binary]
```

SQLite 连接串的斜杠规则：`sqlite:///./dev.db`（相对）、`sqlite:////abs/path/db`（绝对路径，4 个斜杠）。
直接写 `SQLITE_PATH=/abs/path/db` 就不用管斜杠，`config.py` 会自动拼。

随时自检当前连的是哪个库：

```bash
python -m app.config
# DATABASE_URL = sqlite:///./dev.db
# IS_SQLITE    = True
```

> 代码里 JSON 列用的是 `JSON().with_variant(JSONB, "postgresql")`——
> PostgreSQL 下走 JSONB（可建 GIN 索引），SQLite 下自动退化成 JSON，同一套模型两边都能建表。

### 启动参数

`HOST` / `PORT` / `RELOAD` 三个环境变量可覆盖（仅 `python -m app.main` / `python app/main.py` 生效）：

```bash
PORT=8080 RELOAD=0 python -m app.main
```

## 数据结构

一次实验（run）包含三部分：

```python
params        # 指标字典，key 形如 '13-侧向加速度稳态值(g)'
curves_cal    # 计算曲线（含 '原始数据' + '滤波' 两个 series）
curves_duiqi  # 对齐后曲线（仅 '滤波' series）
```

每个工况的多次实验（次数不固定）加一个 `平均` 键：

```json
{
  "1":    [params1, curves_cal1, curves_duiqi1],
  "2":    [params2, curves_cal2, curves_duiqi2],
  "平均": [paramsAvg, curves_calAvg, curves_duiqiAvg]
}
```

### data/ 目录

```
data/
├── 极光EV-1/
│   ├── 角阶跃.json
│   ├── 角脉冲.json
│   ├── 稳态圆周.json
│   ├── 转向回正.json
│   └── 蛇行.json
└── …（每台车一个文件夹）
```

每台车一个文件夹，每个工况一个 JSON（11 台车 × 5 工况 = 55 个文件）。

## 数据库设计

```
platforms  1──N  brands  1──N  vehicles
                                     │ 1:N
                                experiments  N:1  conditions
                                     │ 1:N
                                ┌────┴────┐
                           indicators   curves
```

### 表结构

| 表 | 关键字段 |
|---|---|
| **platforms** | `id`, `name` |
| **brands** | `id`, `name`, `platform_id` |
| **vehicles** | `id`, `name`, `brand_id`, `drive_type`, `vin`, `model_year`, `extra_attrs`(JSONB) |
| **conditions** | `id`, `name`, `description`, `category` |
| **experiments** | `id`, `vehicle_id`, `condition_id`, `experiment_no`(**字符串**，支持 `'平均'`), `is_average`, `sort_order` |
| **indicators** | `id`, `experiment_id`, `name`, `unit`, `raw_key`, `value`, `text_value`, `category` |
| **curves** | `id`, `experiment_id`, `group`(`cal`/`duiqi`), `name`, `title`, `xlabel`, `ylabel`, `series`(JSONB), `point_count`, `sort_order` |

### 设计要点

**① 指标 EAV 行存储** — 不为每个指标名建字段。原始 key `'13-侧向加速度稳态值(g)'` 拆成
`name=侧向加速度稳态值` / `unit=g`，同时保留 `raw_key` 无损还原。
新指标 = INSERT 一行，零 migration。

> **数字前缀（13）刻意不入库。** 原因：
> ① 真实数据里可能没有编号（`'侧向加速度稳态值(g)'`）；
> ② 它跨工况含义不一致 ——「车速」在角阶跃是 10、在角脉冲是 100，
> 表达的是「这是什么指标」而非「排第几」；
> ③ 它不唯一（`17-横摆角速度峰值响应时间(s)` 与 `17-横摆角速度峰值迟滞(s)` 同为 17）。
> 所以**排序一律按 `name`**，`raw_key` 是唯一标识。

**② 曲线 JSONB 整行存储** — 前端"曲线查询只会全拿出来绘制，不会查到 y 这种"，
因此 `series`（含 x/y/curvetype/curvename）整块存 JSONB，一次读取拿全，不拆表。

**③ 实验号用字符串** — `experiment_no` 存 `'1'/'2'/'平均'`，配合 `is_average` +
`sort_order`（平均 = 999）保证排序稳定：数字实验在前，平均在最后。

**④ 车辆属性 固定列 + JSONB** — 常用属性走固定列，扩展属性放 `extra_attrs`。

## API

| # | 接口 | 说明 |
|---|---|---|
| 1 | `GET /api/tree?platform=` | 平台→品牌→车型 目录树 |
| 2 | `GET /api/vehicles/{vid}/conditions/{cid}/data` | 单车 × 工况的指标 + 曲线 |
| 2b | `GET /api/compare?vehicle_ids=1,2,3&condition_id=1` | **多车对比**（页面1） |
| 2c | `GET /api/compare/stream?vehicle_ids=…` | **多车对比（NDJSON 流式）** ← 前端实际走的 |
| 2d | `POST /api/upload` | **上传数据文件并导入**（multipart，支持 dry-run 预览） |
| 3 | `GET /api/indicators/all?platform=&condition=&indicator=` | 所有指标按层级嵌套（页面2） |
| — | `GET /api/conditions` | 工况列表 |
| — | `GET /api/vehicles` | 车辆扁平列表（含平台/品牌/驱动方式） |
| — | `GET /api/vehicles/{id}/conditions` | 某车有数据的工况 |
| — | `GET /api/health` | 健康检查 |

### 返回结构（API 2 / 2b）

```json
{
  "vehicle_name": "极光EV-1",
  "condition_name": "角阶跃",
  "experiments": [
    {
      "experiment_no": "1",
      "is_average": false,
      "params": {
        "13-侧向加速度稳态值(g)": {
          "name": "侧向加速度稳态值",
          "unit": "g", "raw_key": "13-侧向加速度稳态值(g)", "value": 0.54
        }
      },
      "curves_cal": {
        "方向盘转角-时间": {
          "title": "方向盘转角vs时间",
          "xlabel": "时间（s）",
          "ylabel": "方向盘转角（deg）",
          "series": [
            {"x": [...], "y": [...], "curvetype": "line", "curvename": "原始数据"},
            {"x": [...], "y": [...], "curvetype": "line", "curvename": "滤波"}
          ]
        }
      },
      "curves_duiqi": { ... }
    }
  ]
}
```

## 前端

```
frontend/src/
├── api/          axios 封装
├── types/        TS 类型（对齐后端响应）
├── components/
│   ├── VehicleTree.vue   目录树：搜索 + 全选/清空 + 选本平台
│   └── CurveChart.vue    ECharts 折线（多车叠加、缩放、tooltip）
└── views/
    ├── CompareView.vue   页面1：对比画图 + 指标表格
    ├── StatsView.vue     页面2：全部指标统计表格（可导出 CSV）
    └── UploadView.vue    页面3：拖文件夹上传导入（支持预览）
```

**页面1（对比分析）**
- 左侧目录树多选车辆（可搜索、全选、按平台选）
- 选工况 → 两阶段加载：先出指标表和曲线选择区，曲线数据随后**流式**到达
- 7 条曲线各一张 ECharts 图，多台车叠加；指标表格行=指标、列=车×实验
- 界面上**不显示**负载预估行、降采样开关、耗时数字（都在代码里/Console）

**页面2（指标统计）**
- 按工况 + 平台 + 实验号过滤
- 表格：平台 | 品牌 | 车型 | 实验 | 各指标列（左侧列固定、横向滚动）
- 支持导出 CSV

**页面3（上传数据）**
- 拖文件夹进来或点「选择文件夹」，识别车型文件夹 + 元信息 CSV
- 「预览（不写库）」看解析摘要 →「开始导入」（清空模式会二次确认）
- 结果区显示各表条数、车型列表、解析失败/被跳过的文件

## 生成的数据

`scripts/seed_data.py` 用二阶系统模型生成物理自洽的信号：
方向盘阶跃/脉冲/正弦输入 → 侧向加速度、横摆角速度、侧倾角、侧倾角速度、前后轴侧偏角，
**指标由对应曲线计算得出**（响应时间、超调量、稳态增益等），因此指标与曲线互相吻合。

## 规模化（200 台车 × 20 工况 × 10 万点）

改规模**主要在 `scripts/seed_data.py`**，但只调参数是不够的——数据量上来后
读路径、序列化、前端渲染都得跟着改，所以这次一并动了 5 个地方：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `scripts/seed_data.py` | 重写：20 个工况（按信号族分类）、每工况独立点数、车辆程序化生成、分批 commit、内存及时释放、numpy 快路径 | 原来 11 车 / 5 工况 / 100 点是写死的 |
| `app/main.py` | 曲线接口去掉 `response_model`（pydantic 会逐个校验 float，10 万点直接卡死）；改用直接 JSON 序列化；加 GZip | 350 MB 响应不能再走校验 |
| `app/crud.py` | 支持 `exp` / `curves` 过滤；`get_all_indicators_flat` 分页；compare 改为「车一次查、实验一次查」 | 避免 N+1，避免一次返回 35 万个指标 |
| `app/models.py` | `curves.point_count` 冗余字段 | 不解析 JSON 就知道点数 |
| 前端 | `animation:false`、`showSymbol:false`、`progressive` 渐进渲染；超 10000 点走 LTTB 降采样（见下节）；页面 2 改分页 | 10 万点直接渲染会卡主线程 |

**第二轮（把「会崩」变成「可控」）**：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `app/crud.py` | `curves=` 过滤**下推到 SQL**；新增 `exp_scope=avg_first`（窗口函数）；新增 `meta_only`（`load_only` 不读 series）；`get_all_indicators_flat` 改真分页 | 过滤在 Python 里做等于白付 I/O 与反序列化；伪分页翻页和第 1 页一样贵 |
| `app/main.py` | `/api/compare` 加**负载准入检查**（`MAX_COMPARE_NUMBERS`，按估算采样点数而非车辆数） | 200 车只画 1 条曲线可行，50 车画 7 条才会崩 —— 按车辆数卡是错的尺度 |
| `frontend/src/views/CompareView.vue` | **两阶段加载**（先菜单后点菜）+ 按 4000 万点/批自动分批 + `ref`→`shallowRef` + 代际守卫丢弃过期请求 | 指标表与曲线选择区 0.2 s 出来，用户勾完再取数据 |
| `frontend/src/components/CurveChart.vue` | `number[][]` → 交错 `Float64Array` + `dimensions`；去掉 `props.lines` 的 deep watch | 每点驻留 ~520 B → ~40 B（约 10×），`setOption` 快 2.3× |

**第三轮（流式端点：峰值内存从「整个响应」降到「一条曲线」）**：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `app/crud.py` | 新增 `iter_compare_stream()`：先发 `meta` 行，再按 `STREAM_BATCH_CURVES`（24）条一批取 series 逐行发；取 series 用 **Core 查询**而不是 ORM | ORM 的 identity map 会把曲线对象一直攥着，把流式省下的内存又吃回去 |
| `app/main.py` | 新增 `GET /api/compare/stream`（`application/x-ndjson`）；生成器内自建 session；`timing_middleware` 对流式响应跳过 `X-Server-Time-Ms` | 响应头在第一条数据生成前就发出去了，写进去会误导 |
| `frontend/src/api/index.ts` | 新增 `streamCompare()`：`fetch` + `getReader()` 逐行解析 NDJSON，支持 `AbortSignal` | 不用把几百 MB 的字符串整体 `JSON.parse` 一遍 |
| `frontend/src/views/CompareView.vue` | 第二阶段改流式；`chartBlocks` 从 computed 改成手动维护 + `dirtyBlocks` 只重建变过的曲线；200 ms 节流；`watch(selectedCurves)` 加 `deep: true` | 收一条画一条会把 350 万点的图反复全量重画，流式的收益全被吃光 |

实测（50 车 × 平均 × 7 条 1 万点曲线，`python scripts/verify_stream.py`）：
服务端 RSS 峰值增量 **794 MB → 11.6 MB（68×）**，首行 **0.182 s**
（老端点要等 6.6 s 把 body 收完）。

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
```

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
SQLite 单文件能扛，但建议：

* 本地开发用 `demo` / `standard` 预设；
* 上生产用 PostgreSQL（JSONB + TOAST 压缩）；
* 真要 40 GB 全量，把长曲线改成二进制 float32 存储（体积和解析时间都能再降 3~5 倍）。

### 前端曲线降采样（LTTB）

超过阈值的曲线在**进入 ECharts 之前**抽点，默认开、阈值 **10000 点**，对比分析页可调。

* 算法是 **LTTB**（最大三角形三桶法），不是等间隔抽样 —— 等间隔会把尖峰抽没，
  而峰值（超调量、峰值响应时间）恰恰是试验曲线最要紧的信息。实测 10 万点抽到 1 万点：
  LTTB 保住孤立尖峰与首末点，等间隔抽样尖峰直接丢失；
* 代码在 `frontend/src/utils/downsample.ts`，耗时 O(n)，50 万点约 6 ms；
* `CurveChart` 的 `downsampleThreshold` prop 控制，`0` / `null` = 关闭；
* **它不省网络传输**（降采样发生在浏览器里，数据已经下载完了），省的是构建点集 +
  ECharts 建索引 + 渲染内存。要省传输只能靠 `exp` / `curves` 参数少取线；
* 别和 ECharts 自带的 `series.sampling` 搞混，那是渲染层采样，数据照样全进 ECharts。

### 前端怎么保证不卡（传输侧）

**默认走两阶段加载**，而不是一次性拉全量：

1. **菜单阶段** —— `?meta_only=1&exp_scope=avg_first`：只拿车辆 + 指标 + 曲线元信息，
   不含 series。200 车 / 稳态回转实测 **811 KB / 0.1 s**（对照全量 357 MB），
   所以指标表和曲线选择区立刻出来；
2. **点菜阶段** —— `?curves=` 只带用户勾选的曲线，走**流式端点**
   `/api/compare/stream`（NDJSON 逐行返回）。第一行 `meta` 到手就能渲染，
   曲线边到边画；超过 4000 万点/批时拆成多个请求（200 车 × 7 条会拆成 7 个）。

手工调接口时的收敛手段，按收益排序：

* `?curves=方向盘转角-时间,...` —— **最有效**，只拉要画的曲线（过滤下推到 SQL，
  不是拉回来再筛）。7 条 → 1 条能省一个数量级；
* `?exp_scope=avg_first` —— 每台车只取「平均」（无则第一次），省 ~2/3；
* `?meta_only=1` —— 只要元信息，约 440× 更小；
* GZip 中间件默认开（数字 JSON 压 4~6 倍），本机嫌慢可 `GZIP=0`；
* ECharts：`animation:false` + `showSymbol:false` + `progressive:5000`；
* 页面 2 用 `/api/indicators/all?flat=true&limit=100&offset=0` 分页
  （`LIMIT/OFFSET` 在 SQL 里做，不是切 Python 列表）。

**准入检查**：`/api/compare` 会估算采样点总数，超过 `MAX_COMPARE_NUMBERS`
（默认 5000 万）直接返回 400 并给出收敛建议。它**不是**按车辆数卡的 ——
200 车只画 1 条曲线完全没问题（实测 34 MB / 2.5 s），而 50 车画 7 条是 357 MB。

### 前端渲染：大数组不要用 `[[x, y], …]`

ECharts 最顺手的 `series.data = [[x, y], …]` 在 V8 里每个点要一个小数组
（~72 B/点）。实测 8 条线 × 10 万点：`number[][]` 驻留 ~520 B/点，
换成交错 `Float64Array` 后 ~40 B/点（**约 10×**），`setOption` 也快 2.3×。
把 V8 老生代压到 300 MB 时前者直接 OOM、后者正常跑完。

用法是 `dimensions: ['x','y']` + row-major 交错数组（`buf[i*2]=x, buf[i*2+1]=y`）。
用 `Float64` 而不是 `Float32`：后者只有 ~7 位有效数字，5 万点以上坐标就会有偏差。
细节与验证方法见 `DESIGN.md` 的 2.8 节。

### 耗时埋点：慢在哪一段

页面上那个「加载完成 X.Xs」**不是数据库耗时** —— 它是 `performance.now()` 从点击到
数据赋值完成，里面混着网络传输、`JSON.parse`、Vue 响应式赋值和 ECharts 渲染。
要定位瓶颈必须把链路拆开，项目为此做了两层埋点。

**后端（`app/timing.py`）**：

每个请求往 stderr 打两行，分段计时：

```
[timing] compare        db=0.023s  total=0.023s  vehicles=2     ← 业务层：纯 SQL
[timing] /api/compare   server=0.035s  total=0.035s             ← 中间件层：含路由+序列化
```

同时把结果塞进响应头（前端可直接读）：

| 响应头 | 含义 |
|---|---|
| `X-Server-Time-Ms` | 服务端总耗时，毫秒（如 `34.7`） |
| `Server-Timing` | 标准格式，`total=0.035; server=0.035` |

`TIMING=0` 可整体关闭（变成 no-op）。跨域直连 `:8000` 时已在 CORS
`expose_headers` 里声明，浏览器不会屏蔽这两个头。

> **流式请求**（`/api/compare/stream`）**没有** `X-Server-Time-Ms` —— 响应头先于
> 数据发出，写进去只会是「路由返回」的 0.x ms。服务端耗时改放在最后一行的
> `ms` 字段里，前端已自动读它填进「服务端」那一格。

**前端**：耗时打到浏览器 Console 里（不占界面）：

```
[timing] compare  服务端=23ms  网络=12ms  解析=8ms  渲染=145ms  合计=188ms
```

* **服务端** — 读 `X-Server-Time-Ms`，后端自报的 SQL + 序列化耗时；
* **网络** — 发请求到浏览器收完响应体（含 gzip 解压）；
* **解析** — `JSON.parse` 把响应体变成 JS 对象；
* **渲染** — Vue 赋值 + ECharts / el-table 真正画完（`await nextTick()` 之后）；
* **合计** — 从点击到画面完成的端到端时间。

看 Console 就能判断瓶颈在数据库还是在渲染。注意 gzip 的压缩耗时发生在中间件外层，
会计入「网络」段而非「服务端」段。

> 更细的设计说明与完整操作手册见 **[DESIGN.md](./DESIGN.md)**。

## 扩展指南

| 场景 | 做法 |
|---|---|
| 新增指标名 | INSERT indicators 行，零 migration |
| 新增曲线 | INSERT curves 行，零 migration |
| 新增工况 | INSERT conditions 行 + 在 seed 里加指标模板 |
| 新增车辆属性 | 常用加列；临时放 `extra_attrs` JSONB |
| 加分类层级 | platform 加 `parent_id` 改自引用树，或加中间维度表 |
