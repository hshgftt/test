<script setup lang="ts">
/**
 * 上传数据 —— 把算法侧产出的数据文件夹直接拖进浏览器导入。
 *
 * 为什么要有这个页面：`scripts/import_data.py` 只能读**服务器本机**的目录，
 * 而数据是在别的机器上产出的。走页面就不用共享盘、不用先手工拷到服务器
 * 再敲命令。解析与入库规则和命令行**完全共用**（`app/importer.py`），
 * 所以两边导进去的结果一定一致。
 *
 * 三个容易踩的点，都在这页处理掉了：
 *
 * 1. **相对路径要单独发**。multipart 只带 `filename`，`webkitRelativePath`
 *    不进请求体。不把路径一起发，服务端只能收到一堆光秃秃的 `角阶跃.json`，
 *    没法知道它属于哪台车。
 * 2. **根目录不要自己剥**。浏览器会把「被选中的那一层」也带上
 *    （`data/极光EV-1/角阶跃.json`），看着像多了一层。但用户如果直接把
 *    **一台车**的文件夹拖进来，那一层就是车型名，剥了车型就没了。
 *    判断该剥几层需要「看全量路径」——服务端已经有这套逻辑
 *    （`importer.strip_common_root`，优先以 vehicles.csv 的位置为准），
 *    前端原样发过去就行，**别在这儿再实现一遍**，否则两处规则迟早不一致。
 * 3. **别先把所有文件读完再发**。曲线数据几百 MB 很常见，一次性
 *    `Promise.all(readAsArrayBuffer)` 会把原始字节全驻留。这里直接把
 *    `File` 塞进 FormData 交给浏览器流式发。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getMeta, uploadData, type UploadResult } from '@/api'
import type { DatasetType } from '@/types'

interface Picked {
  file: File
  /** 相对路径，如 '极光EV-1/角阶跃.json' */
  rel: string
}

const READABLE_RE = /\.(json|msgpack|msg|mpack)$/i
const META_CSV = new Set([
  'vehicles.csv', 'vehicle.csv', '车辆信息.csv', '车型信息.csv', '车型.csv',
  'conditions.csv', 'condition.csv', '工况信息.csv', '工况.csv',
])

const picked = ref<Picked[]>([])
const mode = ref<'replace' | 'append'>('replace')
/** 本次数据按哪种格式解析。格式可扩展（操稳 / 平顺 / 通用…），来自配置 */
const datasetTypes = ref<DatasetType[]>([])
const datasetType = ref<string>('handling_stability')
const busy = ref(false)
const phase = ref<'' | 'preview' | 'import'>('')
const progress = ref(0)
const result = ref<UploadResult | null>(null)
const dragging = ref(false)
const folderInput = ref<HTMLInputElement | null>(null)

/** 列表最多画这么多行 —— 200 车 × 20 工况是 4000 份文件，全画出来浏览器会卡 */
const LIST_CAP = 100

function baseName(p: string) {
  return p.split('/').pop() || p
}

function isMetaCsv(p: string) {
  return META_CSV.has(baseName(p).toLowerCase())
}

const totalBytes = computed(() => picked.value.reduce((s, p) => s + p.file.size, 0))
const dataFiles = computed(() => picked.value.filter((p) => READABLE_RE.test(p.rel)))
const metaFiles = computed(() => picked.value.filter((p) => isMetaCsv(p.rel)))
const others = computed(
  () => picked.value.filter((p) => !READABLE_RE.test(p.rel) && !isMetaCsv(p.rel)),
)

/** 数据文件所在的那一级目录名 —— 就是车型名 */
const vehicleFolders = computed(() => {
  const set = new Set<string>()
  for (const p of dataFiles.value) {
    const parts = p.rel.split('/')
    if (parts.length >= 2) set.add(parts[parts.length - 2])
  }
  return [...set].sort((a, b) => a.localeCompare(b, 'zh'))
})

const shown = computed(() => picked.value.slice(0, LIST_CAP))

function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(2)} GB`
}

function fmtInt(n: number) {
  return n.toLocaleString('zh-CN')
}

function addFiles(list: Picked[]) {
  const seen = new Set(picked.value.map((p) => `${p.rel}|${p.file.size}`))
  let added = 0
  const merged = [...picked.value]
  for (const it of list) {
    const k = `${it.rel}|${it.file.size}`
    if (seen.has(k)) continue
    seen.add(k)
    merged.push(it)
    added++
  }
  merged.sort((a, b) => a.rel.localeCompare(b.rel, 'zh'))
  picked.value = merged
  result.value = null
  if (added === 0) ElMessage.info('这些文件已经在列表里了')
}

function onPickFolder(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  addFiles(
    files.map((f) => ({
      file: f,
      rel: ((f as any).webkitRelativePath as string) || f.name,
    })),
  )
  // 清空 value：否则再选同一个文件夹不会触发 change
  input.value = ''
}

/** 递归读拖进来的目录（webkitGetAsEntry） */
async function walkEntry(entry: any, prefix: string, out: Picked[]): Promise<void> {
  if (!entry) return
  if (entry.isFile) {
    const file: File = await new Promise((res, rej) => entry.file(res, rej))
    out.push({ file, rel: prefix + entry.name })
    return
  }
  if (!entry.isDirectory) return
  const reader = entry.createReader()
  for (;;) {
    const batch: any[] = await new Promise((res, rej) => reader.readEntries(res, rej))
    if (!batch.length) break
    for (const child of batch) {
      await walkEntry(child, `${prefix}${entry.name}/`, out)
    }
  }
}

async function onDrop(e: DragEvent) {
  e.preventDefault()
  dragging.value = false
  const items = e.dataTransfer?.items
  const out: Picked[] = []
  const entries: any[] = []
  if (items && items.length) {
    for (let i = 0; i < items.length; i++) {
      const en = (items[i] as any).webkitGetAsEntry?.()
      if (en) entries.push(en)
    }
  }
  if (entries.length) {
    for (const en of entries) await walkEntry(en, '', out)
  } else {
    for (const f of Array.from(e.dataTransfer?.files ?? [])) {
      out.push({ file: f, rel: ((f as any).webkitRelativePath as string) || f.name })
    }
  }
  if (!out.length) {
    ElMessage.warning('没读到文件。请拖文件夹，或点上面的「选择文件夹」')
    return
  }
  addFiles(out)
}

function clearAll() {
  picked.value = []
  result.value = null
  progress.value = 0
}

async function run(dryRun: boolean) {
  if (!picked.value.length) {
    ElMessage.warning('请先选择或拖入数据文件夹')
    return
  }
  if (!dataFiles.value.length) {
    ElMessage.warning('列表里没有可识别的数据文件（.json / .msgpack / .msg / .mpack）')
    return
  }
  if (!dryRun && mode.value === 'replace') {
    try {
      await ElMessageBox.confirm(
        '会先清空数据库里现有的全部数据，再写入本次上传的内容。' +
          '此操作不可撤销 —— 如果只是补几台车，请把「导入方式」改成「追加覆盖」。',
        '确认清空并导入？',
        {
          type: 'warning',
          confirmButtonText: '清空并导入',
          cancelButtonText: '取消',
          confirmButtonClass: 'el-button--danger',
        },
      )
    } catch {
      return
    }
  }

  busy.value = true
  phase.value = dryRun ? 'preview' : 'import'
  progress.value = 0
  const t0 = performance.now()
  try {
    const res = await uploadData(
      picked.value.map((p) => p.file),
      {
        paths: picked.value.map((p) => p.rel),
        mode: mode.value,
        dryRun,
        datasetType: datasetType.value || undefined,
      },
      (loaded, total) => {
        progress.value = total ? Math.round((loaded / total) * 100) : 0
      },
    )
    result.value = res
    const ms = Math.round(performance.now() - t0)
    if (dryRun) {
      ElMessage.success(`预览完成：${res.summary.files} 份文件，未写库（${ms} ms）`)
    } else {
      ElMessage.success(
        `导入完成：${res.summary.vehicles} 台车 / ${res.summary.curves} 条曲线` +
          `，用时 ${res.imported?.seconds ?? '?'}s`,
      )
    }
    // 耗时只在 Console 打，界面上不显示（和其他页面一致）
    console.log(
      `[timing] upload  ${dryRun ? 'preview' : 'import'}` +
        `  合计=${(ms / 1000).toFixed(2)}s` +
        `  文件=${res.received.files}  体积=${fmtBytes(res.received.bytes)}` +
        `  服务端入库=${res.imported?.seconds ?? '—'}s`,
    )
  } catch (e: any) {
    ElMessage.error(e?.message || '上传失败')
  } finally {
    busy.value = false
    phase.value = ''
    progress.value = 0
  }
}

onMounted(async () => {
  try {
    const meta = await getMeta()
    datasetTypes.value = meta.dataset_types
    if (datasetTypes.value.length) datasetType.value = datasetTypes.value[0].code
  } catch {
    /* 配置加载失败不挡上传：服务端会用默认格式 */
  }
})
</script>

<template>
  <div class="page">
    <!-- ── 选文件 ── -->
    <el-card shadow="never" class="card">
      <template #header>
        <div class="head">
          <span class="ttl">选择数据文件夹</span>
          <span class="hint">
            目录结构：<code>&lt;根&gt;/&lt;车型&gt;/[&lt;模式&gt;/]&lt;工况&gt;.json</code>
            ，可另放 <code>vehicles.csv</code> / <code>conditions.csv</code>
          </span>
        </div>
      </template>

      <input
        ref="folderInput"
        type="file"
        webkitdirectory
        directory
        multiple
        class="hidden-input"
        @change="onPickFolder"
      />

      <div
        class="dropzone"
        :class="{ over: dragging }"
        @click="folderInput?.click()"
        @dragover.prevent="dragging = true"
        @dragenter.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop="onDrop"
      >
        <div class="dz-main">
          {{ dragging ? '松手即导入列表' : '把数据文件夹拖到这里，或点击选择' }}
        </div>
        <div class="dz-sub">
          一台车一个文件夹，一个工况一个文件；支持 <b>.json</b> 与
          <b>.msgpack / .msg / .mpack</b>，两种后缀的内容结构一致。
        </div>
      </div>

      <div v-if="picked.length" class="stat-row">
        <el-tag type="info" size="small">共 {{ picked.length }} 个文件</el-tag>
        <el-tag type="success" size="small">数据文件 {{ dataFiles.length }}</el-tag>
        <el-tag v-if="metaFiles.length" type="warning" size="small">
          元信息 CSV {{ metaFiles.length }}（{{ metaFiles.map((m) => baseName(m.rel)).join('、') }}）
        </el-tag>
        <el-tag v-if="others.length" size="small">其他 {{ others.length }}</el-tag>
        <el-tag type="info" size="small">{{ fmtBytes(totalBytes) }}</el-tag>
        <span v-if="vehicleFolders.length" class="hint">
          车型文件夹 {{ vehicleFolders.length }} 个：{{ vehicleFolders.slice(0, 6).join('、') }}
          <template v-if="vehicleFolders.length > 6">…</template>
        </span>
        <el-button size="small" text type="danger" class="right" @click="clearAll">
          清空
        </el-button>
      </div>

      <div v-if="picked.length" class="file-list">
        <div v-for="p in shown" :key="p.rel" class="file-row">
          <span class="frel">{{ p.rel }}</span>
          <span class="fsize">{{ fmtBytes(p.file.size) }}</span>
        </div>
        <div v-if="picked.length > shown.length" class="file-row more">
          … 还有 {{ picked.length - shown.length }} 个未显示
        </div>
      </div>

      <el-alert
        v-if="picked.length && !metaFiles.length"
        type="info"
        show-icon
        :closable="false"
        class="tip"
        title="没有 vehicles.csv"
        description="数据文件里只有指标和曲线，没有任何车辆属性（平台/品牌/驱动方式算法侧不产出）。缺 CSV 时品牌会按车型名前缀猜、平台落到「默认平台」。"
      />
      <el-alert
        v-else-if="picked.length && metaFiles.length < 2"
        type="info"
        show-icon
        :closable="false"
        class="tip"
        title="只找到一份元信息 CSV"
        description="vehicles.csv 给车辆属性，conditions.csv 给工况分类/说明。少了哪个，对应字段就是空的（不影响导入）。"
      />
    </el-card>

    <!-- ── 导入选项 ── -->
    <el-card shadow="never" class="card">
      <div class="ctrl-row">
        <span class="ctrl-label">数据格式</span>
        <el-select
          v-model="datasetType"
          size="small"
          style="width: 150px"
          :disabled="busy"
        >
          <el-option
            v-for="t in datasetTypes"
            :key="t.code"
            :label="t.name"
            :value="t.code"
          />
        </el-select>
        <span class="hint">
          决定这批文件按哪种格式解析（格式注册表可扩展，不影响已有数据）
        </span>
      </div>

      <div class="ctrl-row second">
        <span class="ctrl-label">导入方式</span>
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="replace">清空后导入</el-radio-button>
          <el-radio-button value="append">追加覆盖</el-radio-button>
        </el-radio-group>
        <span class="hint">
          {{
            mode === 'replace'
              ? '先清空数据库里现有全部数据（不可撤销）'
              : '保留其他数据，同一台车同一工况按本次内容覆盖（重复上传同一份是幂等的）'
          }}
        </span>

        <el-button size="small" :loading="busy && phase === 'preview'" :disabled="busy" @click="run(true)">
          预览（不写库）
        </el-button>
        <el-button
          type="primary"
          size="small"
          :loading="busy && phase === 'import'"
          :disabled="busy || !picked.length"
          @click="run(false)"
        >
          开始导入
        </el-button>
      </div>

      <div v-if="busy" class="prog">
        <el-progress
          :percentage="progress"
          :indeterminate="progress >= 100"
          :duration="2"
          :stroke-width="14"
        />
        <span class="hint">
          {{
            progress < 100
              ? `正在上传 ${progress}%`
              : phase === 'import'
                ? '服务端正在解析并写库，请不要关闭页面…'
                : '服务端正在解析…'
          }}
        </span>
      </div>
    </el-card>

    <!-- ── 结果 ── -->
    <el-card v-if="result" shadow="never" class="card">
      <template #header>
        <div class="head">
          <span class="ttl">
            {{ result.dry_run ? '预览结果' : '导入结果' }}
          </span>
          <el-tag size="small" :type="result.dry_run ? 'info' : 'success'">
            {{ result.dry_run ? '未写库' : `已写入 · ${result.imported?.seconds ?? '?'}s` }}
          </el-tag>
          <span class="hint">
            收到 {{ result.received.files }} 个文件 / {{ fmtBytes(result.received.bytes) }}
          </span>
        </div>
      </template>

      <div class="grid">
        <div class="cell"><b>{{ fmtInt(result.summary.vehicles) }}</b><span>车型</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.conditions) }}</b><span>工况</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.modes) }}</b><span>试验模式</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.datasets) }}</b><span>数据集</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.indicators) }}</b><span>指标行</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.curves) }}</b><span>曲线</span></div>
        <div class="cell"><b>{{ fmtInt(result.summary.points) }}</b><span>采样点</span></div>
        <div class="cell" v-if="result.imported">
          <b>{{ fmtInt(result.imported.dataset_types) }} / {{ fmtInt(result.imported.modes) }}</b>
          <span>格式 / 模式</span>
        </div>
      </div>

      <div class="meta-line">
        <span class="ctrl-label">元信息</span>
        <el-tag v-if="result.vehicles_csv" size="small" type="warning">
          {{ result.vehicles_csv }}（{{ result.summary.meta_vehicles }} 台）
        </el-tag>
        <el-tag v-else size="small" type="info">无 vehicles.csv，品牌按车型名前缀推断</el-tag>
        <el-tag v-if="result.conditions_csv" size="small" type="warning">
          {{ result.conditions_csv }}
        </el-tag>
        <el-tag v-else size="small" type="info">无 conditions.csv，工况分类为空</el-tag>
        <el-tag size="small" effect="plain">数据格式 {{ result.dataset_type }}</el-tag>
        <el-tag v-if="result.curve_meta_fixed" size="small">
          补算 point_count {{ result.curve_meta_fixed }} 条
        </el-tag>
      </div>

      <div v-if="result.summary.vehicle_names.length" class="meta-line">
        <span class="ctrl-label">车型</span>
        <el-tag
          v-for="n in result.summary.vehicle_names.slice(0, 40)"
          :key="n"
          size="small"
          effect="plain"
        >
          {{ n }}
        </el-tag>
        <span v-if="result.summary.vehicle_names.length > 40" class="hint">
          … 等 {{ result.summary.vehicle_names.length }} 台
        </span>
      </div>

      <el-alert
        v-if="result.error_count"
        type="error"
        show-icon
        :closable="false"
        class="tip"
        :title="`${result.error_count} 个文件解析失败（已跳过，不影响其他文件）`"
      >
        <div v-for="e in result.errors.slice(0, 10)" :key="e.file" class="err-row">
          <span class="frel">{{ e.file }}</span>
          <span class="emsg">{{ e.error }}</span>
        </div>
        <div v-if="result.error_count > 10" class="hint">
          … 还有 {{ result.error_count - 10 }} 个
        </div>
      </el-alert>

      <el-alert
        v-if="result.skipped_count"
        type="warning"
        show-icon
        :closable="false"
        class="tip"
        :title="`${result.skipped_count} 个文件后缀不认识（已忽略）`"
      >
        <span class="hint">{{ result.skipped.slice(0, 10).join('、') }}</span>
      </el-alert>
    </el-card>
  </div>
</template>

<style scoped>
.page { max-width: 1100px; margin: 0 auto; }
.card { margin-bottom: 12px; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ttl { font-weight: 600; font-size: 14px; }
.hint { font-size: 11px; color: #999; }
.hint code {
  background: #f5f5f5;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 11px;
}
.hidden-input { display: none; }

.dropzone {
  border: 1px dashed #c8cdd6;
  border-radius: 6px;
  padding: 26px 16px;
  text-align: center;
  cursor: pointer;
  background: #fafbfc;
  transition: border-color 0.15s, background 0.15s;
}
.dropzone:hover { border-color: #409eff; background: #f5f9ff; }
.dropzone.over { border-color: #409eff; background: #ecf5ff; }
.dz-main { font-size: 14px; color: #555; }
.dz-sub { font-size: 11px; color: #999; margin-top: 6px; }

.stat-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.right { margin-left: auto; }

.file-list {
  margin-top: 8px;
  max-height: 190px;
  overflow: auto;
  border: 1px solid #eef0f3;
  border-radius: 4px;
  background: #fcfcfd;
}
.file-row {
  display: flex;
  gap: 10px;
  padding: 3px 10px;
  font-size: 11px;
  border-bottom: 1px solid #f5f6f8;
}
.file-row:last-child { border-bottom: none; }
.file-row .frel { flex: 1; color: #555; word-break: break-all; }
.file-row .fsize { color: #aaa; flex-shrink: 0; }
.file-row.more { color: #aaa; justify-content: center; }

.tip { margin-top: 10px; }

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
.ctrl-label { font-size: 12px; color: #888; flex-shrink: 0; }
.prog { margin-top: 12px; display: flex; align-items: center; gap: 12px; }
.prog :deep(.el-progress) { flex: 1; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
}
.cell {
  background: #f7f9fb;
  border-radius: 5px;
  padding: 8px 10px;
  text-align: center;
}
.cell b { display: block; font-size: 18px; color: #303133; }
.cell span { font-size: 11px; color: #999; }

.meta-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.err-row { display: flex; gap: 8px; font-size: 11px; margin-top: 4px; }
.err-row .frel { flex: 1; word-break: break-all; }
.err-row .emsg { color: #c45656; }
</style>
