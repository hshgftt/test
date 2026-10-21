/**
 * 最后一问：setOption 之后，ECharts 是否还持有我们传进去的 data？
 *
 * 做法：每个变体在**独立进程**里跑，setOption 后把本地引用置空，强制 GC，
 * 再读 heapUsed + arrayBuffers。若 ECharts 释放了（provider.clean → _data = null），
 * 内存应显著回落。
 *
 * 用法: node --expose-gc scripts/verify_hold.js <pairs|f64>
 */
import * as echarts from 'echarts'

const variant = process.argv[2] || 'pairs'
const N = Number(process.env.N || 100000)
const LINES = Number(process.env.LINES || 8)
const PROG = process.env.PROG !== '0'
const SSR = process.env.SSR !== '0'

const base = {
  animation: false,
  grid: { left: 10, right: 10, top: 10, bottom: 10 },
  xAxis: { type: 'value' },
  yAxis: { type: 'value' },
}

function memMB() {
  const m = process.memoryUsage()
  return (m.heapUsed + m.arrayBuffers) / 1024 / 1024
}

function gc2() {
  if (global.gc) {
    global.gc()
    global.gc()
  }
}

// 预生成坐标（两种变体共用，保证公平）
const xs = new Float64Array(N)
const ys = new Float64Array(N)
for (let i = 0; i < N; i++) {
  xs[i] = i / 10
  ys[i] = Math.sin(i / 40) * 10
}
ys[N >> 1] = 55.5

gc2()
const before = memMB()

const chart = echarts.init(null, null, {
  ssr: true,
  renderer: 'svg',
  width: 900,
  height: 400,
})

let seriesData = []
if (variant === 'pairs') {
  for (let l = 0; l < LINES; l++) {
    const p = new Array(N)
    for (let i = 0; i < N; i++) p[i] = [xs[i], ys[i]]
    seriesData.push(p)
  }
} else {
  for (let l = 0; l < LINES; l++) {
    const b = new Float64Array(N * 2)
    for (let i = 0; i < N; i++) {
      b[i * 2] = xs[i]
      b[i * 2 + 1] = ys[i]
    }
    seriesData.push(b)
  }
}
gc2()
const afterBuild = memMB()

chart.setOption({
  ...base,
  series: seriesData.map((d, k) => ({
    type: 'line',
    name: 'L' + k,
    showSymbol: false,
    dimensions: variant === 'pairs' ? undefined : ['x', 'y'],
    progressive: PROG ? 5000 : 0,
    progressiveThreshold: PROG ? 3000 : 0,
    data: d,
  })),
})

// 关键：把我们的引用全部丢掉，看 ECharts 是否还拽着
seriesData = null
gc2()
const afterDrop = memMB()

let svgLen = 0
if (SSR) {
  svgLen = chart.renderToSVGString().length
  gc2()
}
const afterRender = memMB()

console.log(
  `变体=${variant}  N=${N} lines=${LINES} progressive=${PROG ? 'on' : 'off'} ssr=${SSR ? 'on' : 'off'}`,
)
console.log(`  baseline            ${before.toFixed(1)} MB`)
console.log(`  构建数据后          ${afterBuild.toFixed(1)} MB  (+${(afterBuild - before).toFixed(1)})`)
console.log(`  setOption+丢引用+GC ${afterDrop.toFixed(1)} MB  (+${(afterDrop - before).toFixed(1)})`)
console.log(`  renderToSVGString   ${afterRender.toFixed(1)} MB  (+${(afterRender - before).toFixed(1)})  svg=${svgLen}`)
console.log(
  `  → 每点驻留 ${(((afterDrop - before) * 1024 * 1024) / (N * LINES)).toFixed(1)} B/点`,
)
chart.dispose()
gc2()
const afterDispose = memMB()
console.log(
  `  dispose+GC          ${afterDispose.toFixed(1)} MB  (+${(afterDispose - before).toFixed(1)})`,
)
