/**
 * 对比分析页端到端验证（真实 Chromium + 真实前后端）。
 *
 * 重点验证本轮改动：
 *   1. 两阶段加载 —— 菜单（meta_only）先出表格和曲线选择区，曲线随后到
 *   2. 第二阶段走的是**流式端点** `/api/compare/stream`（NDJSON）
 *   3. series.data 用交错 Float64Array 后，浏览器 canvas 里**确实画出了线**
 *      （SSR/SVG 已验证等价，但浏览器走的是 canvas，要单独确认）
 *   4. 切口径（仅平均 ↔ 全部实验）会重新取数
 *   5. 勾掉 / 重新勾选曲线不报错
 *   6. 全程 0 个运行时 JS 错误
 */
const { chromium } = require('playwright-core')

const BASE = process.env.UI_BASE || 'http://127.0.0.1:5174'
const OUT = process.env.SHOT_DIR || 'e2e-shots'
const fs = require('fs')

const errors = []
let passed = 0
let failed = 0

function ok(cond, label, extra) {
  if (cond) {
    passed++
    console.log(`  ✅ ${label}${extra ? '  ' + extra : ''}`)
  } else {
    failed++
    console.log(`  ❌ ${label}${extra ? '  ' + extra : ''}`)
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** 统计 canvas 里「系列线颜色」的像素数 —— 判断线到底画没画出来 */
const COUNT_LINE_PIXELS = `(() => {
  const cs = [...document.querySelectorAll('.chart-host canvas')]
  if (!cs.length) return { canvases: 0 }
  const c = cs[0]
  const ctx = c.getContext('2d')
  const { width: w, height: h } = c
  const d = ctx.getImageData(0, 0, w, h).data
  let line = 0, any = 0
  for (let i = 0; i < d.length; i += 4) {
    const r = d[i], g = d[i+1], b = d[i+2], a = d[i+3]
    if (a < 10) continue
    any++
    // ECharts 默认调色板第一色 #5470c6 (84,112,198)，带一点容差
    if (Math.abs(r-84) < 70 && Math.abs(g-112) < 70 && Math.abs(b-198) < 70) line++
  }
  return { canvases: cs.length, w, h, total: w*h, any, line }
})()`

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH,
    args: ['--no-proxy-server', '--disable-gpu'],
  })
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })

  /**
   * 注入 longtask 收集器。
   *
   * >50ms 的主线程任务就是用户能感觉到的「卡」—— ECharts 在流式过程中
   * 反复 setOption 很容易堆出几十个。这是衡量「卡不卡」的客观指标，
   * 比「感觉快」靠谱。
   */
  await page.addInitScript(() => {
    window.__longtasks = []
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          window.__longtasks.push(Math.round(e.duration))
        }
      }).observe({ entryTypes: ['longtask'] })
    } catch (err) {
      /* 浏览器不支持就留空数组 */
    }
  })

  /** 记录所有请求 URL —— 用来确认曲线数据确实走的是流式端点 */
  const reqs = []
  page.on('request', (r) => reqs.push(r.url()))

  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('[console] ' + m.text())
  })
  page.on('pageerror', (e) => errors.push('[pageerror] ' + e.message))

  console.log('── 1. 打开对比分析页 ──')
  await page.goto(BASE + '/compare', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.vehicle-tree', { timeout: 20000 })
  await page.waitForSelector('.tag.vehicle', { timeout: 20000 })
  ok(true, '车辆树已渲染')

  const wantCars = Number(process.env.CARS || 3)
  console.log(`\n── 2. 勾选 ${wantCars} 台车 ──`)
  const boxes = page.locator('.el-tree-node__content:has(.tag.vehicle) .el-checkbox__inner')
  const n = Math.min(wantCars, await boxes.count())
  for (let i = 0; i < n; i++) await boxes.nth(i).click()
  await sleep(400)
  const checkedTxt = await page.locator('.checked-info').innerText()
  ok(checkedTxt.includes(`${n}`), `已选车数显示正确`, checkedTxt.replace(/\s+/g, ' '))

  console.log('\n── 3. 切到「稳态回转」（点最多的工况）──')
  await page.locator('.ctrl-row').first().locator('.el-select').click()
  await sleep(400)
  await page.locator('.el-select-dropdown__item:has-text("稳态回转")').first().click()
  await sleep(300)
  const condTxt = await page.locator('.ctrl-row').first().locator('.el-select').innerText()
  ok(condTxt.includes('稳态回转'), '工况已切换', condTxt.trim())

  console.log('\n── 4. 点「加载对比」──')
  // 清空 longtask 计数：只统计**这一次**流式加载，后面的口径切换不算
  await page.evaluate(() => {
    window.__longtasks = []
  })
  const t0 = Date.now()
  await page.getByRole('button', { name: /加载对比/ }).click()

  // 阶段一：菜单（表格 + 曲线选择区）应先出现
  await page.waitForSelector('.table-card', { timeout: 60000 })
  const tMenu = Date.now() - t0
  ok(true, '指标表已出现（阶段一：菜单）', `${tMenu} ms`)
  await page.screenshot({ path: `${OUT}/01-menu.png`, fullPage: false })

  // 页头不应再有品牌标题与技术栈字样（按要求移除）
  const headerTxt = (await page.locator('.app-header').innerText()).replace(/\s+/g, ' ')
  ok(
    !headerTxt.includes('车辆操稳试验数据分析平台') && !headerTxt.includes('FastAPI'),
    '页头已去掉品牌文案',
    headerTxt.trim(),
  )

  // 阶段二：曲线数据
  await page.waitForSelector('.charts .chart-host', { timeout: 120000 })
  await page.waitForFunction(
    () => !document.querySelector('.charts .el-loading-mask'),
    null,
    { timeout: 120000 },
  )
  await sleep(1500) // 等 ECharts 画完
  const tAll = Date.now() - t0
  ok(true, '曲线数据加载完成（阶段二）', `累计 ${tAll} ms`)

  console.log('\n── 5. canvas 里真的画出了线吗 ──')
  const px = await page.evaluate(COUNT_LINE_PIXELS)
  console.log('     canvas 像素统计:', JSON.stringify(px))
  ok(px.canvases > 0, '图表 canvas 已创建', `${px.canvases} 个`)
  ok(px.line > 500, 'canvas 里存在系列线颜色的像素（线确实画出来了）', `line=${px.line}`)

  // 流式加载期间的长任务（>50ms 阻塞主线程 = 用户感知的卡顿）
  const lt = await page.evaluate(() => window.__longtasks || [])
  const ltTotal = lt.reduce((a, b) => a + b, 0)
  const ltMax = lt.length ? Math.max(...lt) : 0
  console.log(
    `     longtask(>50ms): ${lt.length} 个 / 合计 ${ltTotal} ms / 最长 ${ltMax} ms`,
  )
  ok(ltTotal < 3000, '流式加载期间长任务总时长 < 3s', `${lt.length} 个 / ${ltTotal} ms`)

  // 前端第二阶段必须走流式端点（而不是老的 /api/compare 一次性返回）
  const streamReqs = reqs.filter((u) => u.includes('/api/compare/stream'))
  ok(
    streamReqs.length > 0,
    '曲线数据走的是流式端点 /api/compare/stream',
    `${streamReqs.length} 个流式 / 共 ${reqs.filter((u) => u.includes('/api/compare')).length} 个对比请求`,
  )

  await page.screenshot({ path: `${OUT}/02-charts.png`, fullPage: false })

  console.log('\n── 6. 曲线卡片标题与图例 ──')
  const titles = await page.locator('.chart-card .chart-title').allInnerTexts()
  console.log('     卡片:', titles.map((t) => t.replace(/\s+/g, ' ')).join(' | '))
  ok(titles.length > 0, '至少渲染了 1 张曲线卡片')

  console.log('\n── 7. 切「同图取线 = 全部实验」应重新取数（图上线数翻倍）──')
  /** 第一张卡片标题里的「N 条线」 */
  async function firstCardLines() {
    const t = await page.locator('.chart-card .chart-title').first().innerText()
    const m = t.match(/(\d+)\s*条线/)
    return m ? Number(m[1]) : -1
  }
  const linesBefore = await firstCardLines()
  // 「同图取线」那组里的「全部实验」（另一组「指标实验」里也有同名按钮）
  await page
    .locator('.ctrl-row')
    .filter({ hasText: '同图取线' })
    .locator('.el-radio-button:has-text("全部实验")')
    .click()
  await sleep(1500)
  await page.waitForFunction(
    () => !document.querySelector('.charts .el-loading-mask'),
    null,
    { timeout: 180000 },
  )
  await sleep(1500)
  const linesAfter = await firstCardLines()
  ok(
    linesAfter > linesBefore,
    '切到「全部实验」后图上线数增加（说明确实重新取了全量）',
    `${linesBefore} → ${linesAfter} 条`,
  )
  await page.screenshot({ path: `${OUT}/03-all-exps.png`, fullPage: false })

  console.log('\n── 7b. 切「指标实验 = 全部实验」应增加表格列 ──')
  const colsBefore = await page.locator('.table-card thead th').count()
  await page
    .locator('.ctrl-row')
    .filter({ hasText: '指标实验' })
    .locator('.el-radio-button:has-text("全部实验")')
    .click()
  for (let i = 0; i < 90; i++) {
    const c = await page.locator('.table-card thead th').count()
    if (c !== colsBefore) break
    await sleep(1000)
  }
  const colsAfter = await page.locator('.table-card thead th').count()
  ok(colsAfter > colsBefore, '表格列数增加', `${colsBefore} → ${colsAfter}`)
  await page.waitForFunction(
    () => !document.querySelector('.charts .el-loading-mask'),
    null,
    { timeout: 180000 },
  )
  await sleep(1000)
  await page.screenshot({ path: `${OUT}/03b-all-cols.png`, fullPage: false })

  console.log('\n── 8. 勾掉全部曲线 / 只留 1 条 ──')
  const curveRow = page.locator('.ctrl-row').filter({ hasText: '全选' })
  await curveRow.locator('.el-checkbox').first().click()
  await sleep(1000)
  const emptyCards = await page.locator('.chart-card').count()
  ok(emptyCards === 0, '取消全选后不再渲染曲线卡片', `${emptyCards} 张`)

  // 只勾第一条曲线（数据已在内存里，不应再发请求）
  await curveRow.locator('.el-select').click()
  await sleep(500)
  // 页面上有多个 el-select 的下拉（工况 / 曲线），只有当前展开的那个是可见的
  const visItems = await page.locator('.el-select-dropdown__item:visible').allInnerTexts()
  console.log(
    `     下拉过滤: 共 ${await page.locator('.el-select-dropdown').count()} 个` +
      ` / 可见 ${await page.locator('.el-select-dropdown:visible').count()} 个` +
      ` / 可见项 ${visItems.length} 项`,
  )
  await page.locator('.el-select-dropdown__item:visible').first().click()
  await page.keyboard.press('Escape')
  await sleep(1500)
  const oneCard = await page.locator('.chart-card').count()
  ok(oneCard === 1, '只勾 1 条曲线时渲染 1 张卡片', `${oneCard} 张`)
  const px2 = await page.evaluate(COUNT_LINE_PIXELS)
  ok(px2.line > 500, '单曲线也有线被画出', `line=${px2.line}`)
  await page.screenshot({ path: `${OUT}/04-one-curve.png`, fullPage: false })

  console.log('\n── 9. 切回「仅平均」──')
  await page
    .locator('.ctrl-row')
    .filter({ hasText: '同图取线' })
    .locator('.el-radio-button:has-text("平均")')
    .click()
  await sleep(2500)
  await page.waitForFunction(
    () => !document.querySelector('.charts .el-loading-mask'),
    null,
    { timeout: 180000 },
  )
  await sleep(1200)
  const linesAvg = await firstCardLines()
  ok(linesAvg < linesAfter, '切回「仅平均」后线数回落', `${linesAfter} → ${linesAvg} 条`)
  await page.screenshot({ path: `${OUT}/05-back-to-avg.png`, fullPage: false })

  console.log('\n── 10. 指标统计页（分页接口本轮从「伪分页」改成了 SQL 侧真分页）──')
  await page.goto(BASE + '/stats', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.table-card .el-table__body tr', { timeout: 60000 })
  const sum1 = (await page.locator('.summary').innerText()).trim()
  console.log('     摘要:', sum1)
  const m1 = sum1.match(/(\d+)\s*行\s*\/\s*共\s*(\d+)\s*行/)
  ok(!!m1, '摘要格式正确', sum1)
  const pageRows = m1 ? Number(m1[1]) : 0
  const totalRows = m1 ? Number(m1[2]) : 0
  ok(pageRows === 100 && totalRows > 100, '第一页 100 行、总数更多', `${pageRows}/${totalRows}`)

  // 记下第一页第一行的车型，翻页后应该不同
  const firstCell1 = await page.locator('.table-card .el-table__body tr').first().innerText()

  // 翻到第 2 页
  await page.locator('.el-pager li:has-text("2")').first().click()
  await page.waitForFunction(
    (prev) => {
      const tr = document.querySelector('.table-card .el-table__body tr')
      return tr && tr.innerText !== prev
    },
    firstCell1,
    { timeout: 60000 },
  )
  const sum2 = (await page.locator('.summary').innerText()).trim()
  const m2 = sum2.match(/(\d+)\s*行\s*\/\s*共\s*(\d+)\s*行/)
  ok(m2 && Number(m2[2]) === totalRows, '翻页后 total 不变', sum2)
  const firstCell2 = await page.locator('.table-card .el-table__body tr').first().innerText()
  ok(firstCell1 !== firstCell2, '第 2 页内容与第 1 页不同')
  await page.screenshot({ path: `${OUT}/06-stats-page2.png`, fullPage: false })

  // 切「仅平均」口径：total 应显著变小（每台车每工况只留平均那一次）
  await page.locator('.el-radio-button:has-text("仅平均")').first().click()
  await sleep(2500)
  await page.waitForFunction(
    () => {
      const el = document.querySelector('.summary')
      return el && /行/.test(el.innerText)
    },
    null,
    { timeout: 60000 },
  )
  const sum3 = (await page.locator('.summary').innerText()).trim()
  const m3 = sum3.match(/(\d+)\s*行\s*\/\s*共\s*(\d+)\s*行/)
  ok(m3 && Number(m3[2]) < totalRows, '切「仅平均」后 total 变小', `${totalRows} → ${m3 && m3[2]}`)
  ok(m3 && Number(m3[2]) === Math.round(totalRows / 3), 'total 恰为全部实验的 1/3（每车每工况 3 次）')
  await page.screenshot({ path: `${OUT}/07-stats-avg.png`, fullPage: false })

  console.log('\n── 11. 控制台耗时埋点 ──')
  const logs = await page.evaluate(() => window.__timingLogs || [])
  void logs

  await browser.close()

  console.log('\n' + '='.repeat(56))
  console.log(`断言: ${passed} 通过 / ${failed} 失败`)
  console.log(`运行时 JS 错误: ${errors.length}`)
  errors.slice(0, 15).forEach((e) => console.log('   ' + e.slice(0, 300)))
  console.log(`截图目录: ${OUT}`)
  console.log('='.repeat(56))
  process.exit(failed || errors.length ? 1 : 0)
}

main().catch((e) => {
  console.error('脚本异常:', e)
  console.error('运行时错误累计:', errors.length)
  errors.slice(0, 15).forEach((x) => console.error('   ' + x.slice(0, 300)))
  process.exit(2)
})
