<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { downsampleIfNeeded } from '@/utils/downsample'

export interface ChartLine {
  name: string
  x: number[]
  y: number[]
}

const props = withDefaults(
  defineProps<{
    title?: string | null
    xlabel?: string | null
    ylabel?: string | null
    lines: ChartLine[]
    /** 原始点数（用于标题提示与降采样判断） */
    pointCount?: number
    /**
     * 降采样阈值。超过这个点数就走 LTTB 降采样。
     * `0` / `null` = 关闭降采样（始终全分辨率）。
     */
    downsampleThreshold?: number | null
  }>(),
  { downsampleThreshold: 10000 },
)

const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

/** 降采样开关（threshold 为 0/null 时关） */
const dsEnabled = computed(
  () => props.downsampleThreshold != null && props.downsampleThreshold > 0,
)

/**
 * 每条线的待绘制数据 + 统计。
 *
 * ── 这里刻意**不构建 `[[x, y], …]` 点集** ──
 *
 * ECharts 最顺手的写法是 `series.data = [[x, y], …]`，但 V8 里每个两元素
 * 小数组要 ~56B 数组头 + 16B 元素 = 72B/点。实测（8 条线 × 10 万点）：
 *
 *     number[][]            ~520 B/点   ← 把 V8 老生代压到 300MB 直接 OOM
 *     交错 Float64Array      ~40 B/点   ← 同样数据正常跑完（约 10x）
 *
 * 所以这里只保留**扁平**的 x / y（降采样后也是扁平的），交错数组在
 * `render()` 里临时构建、交给 ECharts 后就丢掉 —— ECharts 对 typed array
 * 源会走 `provider.clean()`（把内部 `_data` 置空），不会长期持有它。
 *
 * 用 Float64 而不是 Float32：Float32 只有 ~7 位有效数字，5 万点以上时
 * 折线坐标就会出现肉眼可见的偏差（逐字节比对能测出来）。Float64 的内存
 * 仍然只有 number[][] 的 1/4.5，而渲染结果与原来**完全一致**。
 *
 * 注意降采样省的是构建点集 / ECharts 建索引的内存，**不省网络传输**
 * （数据已经到浏览器了）。传输侧的优化要靠后端的 exp / curves 参数。
 */
const prepared = computed(() => {
  const threshold = dsEnabled.value ? (props.downsampleThreshold as number) : 0
  const items = props.lines.map((l) => {
    const r = dsEnabled.value
      ? downsampleIfNeeded(l.x, l.y, threshold)
      : {
          x: l.x,
          y: l.y,
          downsampled: false,
          originalCount: Math.min(l.x.length, l.y.length),
          outputCount: Math.min(l.x.length, l.y.length),
        }
    return {
      line: l,
      x: r.x,
      y: r.y,
      count: r.outputCount,
      downsampled: r.downsampled,
      originalCount: r.originalCount,
    }
  })

  const anyDown = items.some((o) => o.downsampled)
  const original = items.length ? items[0].originalCount : 0
  const shown = items.length ? items[0].count : 0
  return { items, anyDown, original, shown }
})

/**
 * 把扁平的 x / y 交错成一条 Float64Array（ECharts 的 typed array 源，
 * row-major：`buf[i*2] = x[i]`、`buf[i*2+1] = y[i]`）。
 *
 * 这个数组是**一次性**的：setOption 之后 ECharts 已经把它拷进内部存储，
 * 并主动把 provider 里的引用置空，所以我们不留任何引用。
 */
function interleave(x: number[], y: number[], n: number): Float64Array {
  const buf = new Float64Array(n * 2)
  for (let i = 0; i < n; i++) {
    buf[i * 2] = x[i]
    buf[i * 2 + 1] = y[i]
  }
  return buf
}

/**
 * 类型断言集中点。
 *
 * ECharts 运行期**支持** `series.data` 是 typed array（`lib/data/Source.js`
 * 里 `createSourceFromSeriesDataOption()` 会据此走 `SOURCE_FORMAT_TYPED_ARRAY`，
 * 并用 `fillStorage` 直接灌进内部存储），但 `echarts` 自带的 .d.ts 只把
 * `series.data` 声明成普通数组 —— 是类型声明的缺口，不是用法错误。
 *
 * 把断言关在这一个函数里，别让 `as any` 散落到 option 里。
 */
function asSeriesData(buf: Float64Array): number[] {
  return buf as unknown as number[]
}

/** 标题副文案：说明当前是降采样展示还是全分辨率 */
const subtitle = computed(() => {
  const { anyDown, original, shown } = prepared.value
  if (!original) return ''
  if (anyDown) {
    return `${original.toLocaleString()} 点 → 降采样显示 ${shown.toLocaleString()} 点`
  }
  const full = props.pointCount || original
  return `${full.toLocaleString()} 点（全分辨率）`
})

function buildOption(): echarts.EChartsOption {
  return {
    // 10万点必须关动画，否则每帧都在插值
    animation: false,
    title: {
      text: props.title ?? '',
      subtext: subtitle.value,
      left: 'center',
      top: 4,
      textStyle: { fontSize: 13, fontWeight: 600 },
      subtextStyle: { fontSize: 10, color: '#909399' },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      textStyle: { fontSize: 12 },
      // 大数据量下 tooltip 自己也不该拖慢渲染
      confine: true,
    },
    legend: {
      top: 34,
      type: 'scroll',
      textStyle: { fontSize: 11 },
    },
    grid: { left: 60, right: 24, top: 72, bottom: 58 },
    xAxis: {
      type: 'value',
      name: props.xlabel ?? '',
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { fontSize: 11 },
      axisLabel: { fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      name: props.ylabel ?? '',
      nameLocation: 'middle',
      nameGap: 46,
      nameTextStyle: { fontSize: 11 },
      axisLabel: { fontSize: 11 },
      scale: true,
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 16, bottom: 14 },
    ],
    series: prepared.value.items.map((it) => ({
      name: it.line.name,
      type: 'line' as const,
      showSymbol: false,      // 关键点：不画点
      smooth: false,
      // typed array 源**必须**显式声明维度，否则 ECharts 直接断言失败
      dimensions: ['x', 'y'],
      // 降采样后点数已受控，progressive 只在仍然很大时才起作用
      progressive: 5000,
      progressiveThreshold: 3000,
      lineStyle: { width: 1.6 },
      data: asSeriesData(interleave(it.x, it.y, it.count)),
    })),
  }
}

function render() {
  if (!chart) return
  chart.setOption(buildOption(), true)
}

function onResize() {
  chart?.resize()
}

onMounted(() => {
  if (!host.value) return
  chart = echarts.init(host.value, undefined, { useDirtyRect: true })
  render()
  window.addEventListener('resize', onResize)
})

// 浅比较就够：父组件的 lines 由 computed 重建，引用一定变。
// 这里原本是 deep: true —— 那会**深度遍历每一条曲线的几万个点**，
// 10 万点工况下每次数据变动都要走一遍，纯浪费。
watch(() => props.lines, render)
watch(
  () => [props.title, props.xlabel, props.ylabel, props.pointCount, props.downsampleThreshold],
  render,
)

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="host" class="chart-host" />
</template>

<style scoped>
.chart-host {
  width: 100%;
  height: 340px;
}
</style>
