/**
 * 终局验证：只比对「画面几何」——所有 path 的 d 属性 + 所有文本节点。
 * 内部 class/id 计数器（zr3 / cls-27）是 zrender 的实现细节，与画面无关，忽略。
 */
import * as echarts from 'echarts'

const base = {
  animation: false,
  grid: { left: 10, right: 10, top: 10, bottom: 10 },
  xAxis: { type: 'value' },
  yAxis: { type: 'value' },
}
const prog = { progressive: 5000, progressiveThreshold: 3000 }

function renderSvg(option) {
  const chart = echarts.init(null, null, {
    ssr: true,
    renderer: 'svg',
    width: 900,
    height: 400,
  })
  chart.setOption(option)
  const svg = chart.renderToSVGString()
  chart.dispose()
  return svg
}

/** 抽取画面几何：path 的 d + 文本 + 填充/描边色 */
function geometry(svg) {
  const paths = (svg.match(/\sd="([^"]*)"/g) || []).map((s) => s.slice(4, -1))
  const texts = (svg.match(/>([^<>]+)<\/text>/g) || []).map((s) =>
    s.slice(1, -7),
  )
  const strokes = (svg.match(/stroke="(#[0-9A-Fa-f]{3,8})"/g) || []).map((s) =>
    s.slice(8, -1),
  )
  const fills = (svg.match(/\sfill="(#[0-9A-Fa-f]{3,8})"/g) || []).map((s) =>
    s.slice(7, -1),
  )
  return { paths, texts, strokes, fills }
}

function mkData(N, seed) {
  const pairs = new Array(N)
  const f64 = new Float64Array(N * 2)
  const f32 = new Float32Array(N * 2)
  let s = seed
  const rnd = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff)
  for (let i = 0; i < N; i++) {
    const x = i / 10 + rnd() * 0.0123456789
    const y = Math.sin(i / 40) * 10 + rnd() * 0.987654321
    pairs[i] = [x, y]
    f64[i * 2] = x
    f64[i * 2 + 1] = y
    f32[i * 2] = x
    f32[i * 2 + 1] = y
  }
  return { pairs, f64, f32 }
}

function cmp(label, ga, gb) {
  const keys = ['paths', 'texts', 'strokes', 'fills']
  const diffs = []
  for (const k of keys) {
    const A = ga[k]
    const B = gb[k]
    if (A.length !== B.length) {
      diffs.push(`${k}: 数量不同 ${A.length} vs ${B.length}`)
      continue
    }
    for (let i = 0; i < A.length; i++) {
      if (A[i] !== B[i]) {
        diffs.push(`${k}[${i}]: "${String(A[i]).slice(0, 60)}" vs "${String(B[i]).slice(0, 60)}"`)
        break
      }
    }
  }
  const ok = diffs.length === 0
  console.log(`  ${ok ? '✅ 画面完全一致' : '❌ 有差异'}  ${label}`)
  for (const d of diffs) console.log(`       ${d}`)
  return ok
}

let allOk = true
console.log('── 几何比对：number[][]  vs  交错 Float64  vs  交错 Float32 ──')
for (const N of [10, 137, 1000, 10000, 50000]) {
  const { pairs, f64, f32 } = mkData(N, 42)
  const gA = geometry(
    renderSvg({ ...base, series: [{ type: 'line', showSymbol: false, ...prog, data: pairs }] }),
  )
  const g64 = geometry(
    renderSvg({
      ...base,
      series: [{ type: 'line', showSymbol: false, dimensions: ['x', 'y'], ...prog, data: f64 }],
    }),
  )
  const g32 = geometry(
    renderSvg({
      ...base,
      series: [{ type: 'line', showSymbol: false, dimensions: ['x', 'y'], ...prog, data: f32 }],
    }),
  )
  console.log(`\n N=${N}`)
  allOk = cmp('number[][] vs Float64', gA, g64) && allOk
  allOk = cmp('number[][] vs Float32', gA, g32) && allOk
}

// 8 条线 + 尖峰
console.log('\n── 8 条线 + 尖峰，N=20000 ──')
const N = 20000
const lines = Array.from({ length: 8 }, (_, k) => {
  const p = new Array(N)
  const t = new Float64Array(N * 2)
  for (let i = 0; i < N; i++) {
    const y = Math.sin(i / 40 + k) * 10
    p[i] = [i / 10, y]
    t[i * 2] = i / 10
    t[i * 2 + 1] = y
  }
  p[Math.floor(N / 2)] = [N / 20, 55.5 + k]
  t[Math.floor(N / 2) * 2 + 1] = 55.5 + k
  return { p, t }
})
const gMA = geometry(
  renderSvg({
    ...base,
    legend: {},
    series: lines.map((l, k) => ({ type: 'line', name: 'L' + k, showSymbol: false, ...prog, data: l.p })),
  }),
)
const gMB = geometry(
  renderSvg({
    ...base,
    legend: {},
    series: lines.map((l, k) => ({
      type: 'line',
      name: 'L' + k,
      showSymbol: false,
      dimensions: ['x', 'y'],
      ...prog,
      data: l.t,
    })),
  }),
)
allOk = cmp('number[][] vs Float64（8 线）', gMA, gMB) && allOk

console.log(
  allOk
    ? '\n结论：交错 Float64Array + dimensions 与 number[][] 画面完全等价 ✅ 可安全替换'
    : '\n结论：存在差异 ❌',
)
