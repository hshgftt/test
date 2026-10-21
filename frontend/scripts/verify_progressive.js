/**
 * 排雷：progressive 渲染 + typed array 源会不会抛错？
 * 以及大 N 下 typed array 的实际 setOption 耗时。
 */
import * as echarts from 'echarts'

const base = {
  animation: false,
  grid: { left: 10, right: 10, top: 10, bottom: 10 },
  xAxis: { type: 'value' },
  yAxis: { type: 'value' },
}

function mkPairs(N) {
  const p = new Array(N)
  for (let i = 0; i < N; i++) p[i] = [i / 10, Math.sin(i / 40) * 10]
  return p
}
function mkTyped(N, Ctor = Float64Array) {
  const b = new Ctor(N * 2)
  for (let i = 0; i < N; i++) {
    b[i * 2] = i / 10
    b[i * 2 + 1] = Math.sin(i / 40) * 10
  }
  return b
}

function run(label, option) {
  const t0 = performance.now()
  try {
    const chart = echarts.init(null, null, {
      ssr: true,
      renderer: 'svg',
      width: 900,
      height: 400,
    })
    chart.setOption(option)
    const svg = chart.renderToSVGString()
    chart.dispose()
    const ms = performance.now() - t0
    console.log(`  ✅ ${label.padEnd(46)} ${ms.toFixed(0).padStart(6)} ms  svg=${svg.length}`)
    return { ok: true, ms, svg }
  } catch (e) {
    console.log(`  ❌ ${label.padEnd(46)} ${String(e && e.message)}`)
    return { ok: false }
  }
}

console.log('── progressive 组合测试（N=50000，阈值 3000） ──')
const N = 50000
const prog = { progressive: 5000, progressiveThreshold: 3000 }

run('number[][] + progressive', {
  ...base,
  series: [{ type: 'line', showSymbol: false, ...prog, data: mkPairs(N) }],
})
run('Float64 交错 + dimensions + progressive', {
  ...base,
  series: [
    {
      type: 'line',
      showSymbol: false,
      dimensions: ['x', 'y'],
      ...prog,
      data: mkTyped(N),
    },
  ],
})
run('Float64 交错 + dimensions（无 progressive）', {
  ...base,
  series: [
    {
      type: 'line',
      showSymbol: false,
      dimensions: ['x', 'y'],
      data: mkTyped(N),
    },
  ],
})

console.log('\n── setOption 耗时（单条线，不同 N） ──')
for (const n of [10000, 100000, 500000]) {
  const t0 = performance.now()
  const pairs = mkPairs(n)
  const t1 = performance.now()
  const typed = mkTyped(n)
  const t2 = performance.now()
  console.log(
    `  N=${String(n).padStart(6)}  构建 number[][]=${(t1 - t0).toFixed(1)}ms  构建 Float64 交错=${(t2 - t1).toFixed(1)}ms`,
  )

  const rA = run(`  N=${n} number[][] + progressive`, {
    ...base,
    series: [{ type: 'line', showSymbol: false, ...prog, data: pairs }],
  })
  const rB = run(`  N=${n} Float64 交错 + progressive`, {
    ...base,
    series: [
      {
        type: 'line',
        showSymbol: false,
        dimensions: ['x', 'y'],
        ...prog,
        data: typed,
      },
    ],
  })
  if (rA.ok && rB.ok) {
    console.log(`         → setOption 比值 A/B = ${(rA.ms / rB.ms).toFixed(2)}x`)
  }
}
