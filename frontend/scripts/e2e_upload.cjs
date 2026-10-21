/**
 * 上传数据页端到端验证（真实 Chromium + 真实前后端）。
 *
 * 重点验证：
 *   1. `/upload` 路由存在且页面能渲染（以前菜单指向了不存在的路由 → 白屏）
 *   2. 用文件夹选择器选中一整个目录后，**相对路径确实带上了车型文件夹**
 *      （`example_data/示例车A/角阶跃.json`），而不是一堆光秃秃的文件名
 *   3. 「预览」拿到的是**真实解析结果**（车型/工况/曲线条数）
 *   4. 「预览」真的**没写库**（点完预览后 /api/tree 还是空的）
 *   5. 「开始导入」+ 二次确认后，数据真的进了库（/api/tree 出现那两台车）
 *   6. 全程 0 个运行时 JS 错误
 *
 * ⚠️ 这个脚本会调 `mode=replace`（drop_all），所以后端**必须**用临时 SQLite 库
 * 起（见 run_e2e_upload.sh），绝不能指向真库。
 */
const { chromium } = require('playwright-core')
const fs = require('fs')

const BASE = process.env.UI_BASE || 'http://127.0.0.1:5175'
const OUT = process.env.SHOT_DIR || 'e2e-shots'
const DATA_DIR = process.env.DATA_DIR || 'F:/A_projects/vehicle-test-db/example_data'

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

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH,
    args: ['--no-proxy-server', '--disable-gpu'],
  })
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })

  page.on('pageerror', (e) => errors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })

  // ── 1. 页面能打开 ──
  console.log('\n[1] 打开 /upload')
  await page.goto(`${BASE}/upload`, { waitUntil: 'networkidle' })
  await sleep(400)
  ok(await page.locator('.dropzone').isVisible(), '拖拽区已渲染')
  const navText = await page.locator('.nav-menu').innerText()
  ok(navText.includes('上传数据'), '导航里有「上传数据」', JSON.stringify(navText.replace(/\n/g, ' ')))
  await page.screenshot({ path: `${OUT}/upload-1-empty.png` })

  // ── 2. 选文件夹 ──
  console.log('\n[2] 用文件夹选择器选 example_data')
  await page.setInputFiles('input[webkitdirectory]', DATA_DIR)
  await sleep(500)
  const stats = await page.locator('.stat-row').innerText()
  console.log('     统计行:', JSON.stringify(stats.replace(/\n/g, ' | ')))
  ok(/共\s*5\s*个文件/.test(stats), '识别到 5 个文件（3 数据 + 2 CSV）')
  ok(/数据文件\s*3/.test(stats), '其中数据文件 3 个')
  ok(/元信息 CSV\s*2/.test(stats), '元信息 CSV 2 份')
  ok(/车型文件夹\s*2\s*个/.test(stats), '车型文件夹 2 个')

  const rels = await page.locator('.file-list .frel').allInnerTexts()
  console.log('     相对路径:', JSON.stringify(rels))
  ok(
    rels.some((r) => r.replace(/\\/g, '/').endsWith('示例车A/角阶跃.json')),
    '相对路径带上了车型文件夹（不是光秃秃的文件名）',
  )
  ok(
    rels.some((r) => /vehicles\.csv$/i.test(r)),
    'vehicles.csv 也在列表里',
  )
  await page.screenshot({ path: `${OUT}/upload-2-picked.png` })

  // ── 3. 预览 ──
  console.log('\n[3] 预览（dry_run）')
  await page.getByRole('button', { name: '预览（不写库）' }).click()
  await page.waitForSelector('.grid', { timeout: 30000 })
  await sleep(300)
  const grid = await page.locator('.grid').innerText()
  console.log('     摘要:', JSON.stringify(grid.replace(/\n/g, ' ')))
  const nums = grid.match(/\d[\d,]*/g) || []
  ok(/预览结果/.test(await page.locator('.card').last().innerText()), '显示「预览结果」')
  ok(nums.includes('2'), '车型 2 台')
  ok(nums.includes('36'), '曲线 36 条')

  // 预览不该写库
  const treeAfterPreview = await page.evaluate(async () => {
    const r = await fetch('/api/tree')
    return r.json()
  })
  ok(Object.keys(treeAfterPreview).length === 0, '预览后库仍是空的（没写库）',
    JSON.stringify(Object.keys(treeAfterPreview)))
  await page.screenshot({ path: `${OUT}/upload-3-preview.png` })

  // ── 4. 导入 ──
  console.log('\n[4] 开始导入 + 二次确认')
  await page.getByRole('button', { name: '开始导入' }).click()
  await page.waitForSelector('.el-message-box', { timeout: 10000 })
  const boxText = await page.locator('.el-message-box__message').innerText()
  ok(/不可撤销/.test(boxText), '弹出了「不可撤销」的确认框')
  await page.screenshot({ path: `${OUT}/upload-4-confirm.png` })
  await page.locator('.el-message-box__btns .el-button--primary').click()

  // 等结果卡片变成「导入结果」
  await page.waitForFunction(
    () => {
      const t = document.body.innerText
      return t.includes('导入结果') && /已写入/.test(t)
    },
    { timeout: 120000 },
  )
  await sleep(300)
  const bodyText = await page.locator('body').innerText()
  ok(/导入结果/.test(bodyText), '显示「导入结果」')
  ok(/已写入/.test(bodyText), '标注「已写入」')
  ok(!/个文件解析失败/.test(bodyText), '没有解析失败的文件')

  // ── 5. 数据真的进库了 ──
  console.log('\n[5] 库里的数据')
  const tree = await page.evaluate(async () => {
    const r = await fetch('/api/tree')
    return r.json()
  })
  const allVehicles = Object.values(tree).flatMap((brands) =>
    Object.values(brands).flatMap((vs) => vs.map((v) => v.name)),
  )
  console.log('     车型:', JSON.stringify(allVehicles))
  ok(allVehicles.length === 2, '库里 2 台车', String(allVehicles.length))
  ok(allVehicles.includes('示例车A'), '示例车A 在库里')

  const veh = await page.evaluate(async () => {
    const r = await fetch('/api/vehicles')
    return r.json()
  })
  const a = veh.find((v) => v.name === '示例车A') || {}
  ok(a.platform === 'EPA1' && a.brand === '星河', 'CSV 元信息生效（EPA1 / 星河）',
    `${a.platform} / ${a.brand}`)

  await page.screenshot({ path: `${OUT}/upload-5-done.png`, fullPage: true })

  // ── 6. 运行时错误 ──
  console.log('\n[6] 运行时 JS 错误')
  ok(errors.length === 0, '0 个运行时错误', errors.slice(0, 3).join(' | '))

  await browser.close()
  console.log('\n' + '='.repeat(56))
  console.log(`断言: ${passed} 通过 / ${failed} 失败`)
  console.log(`运行时 JS 错误: ${errors.length}`)
  console.log('='.repeat(56))
  process.exit(failed ? 1 : 0)
}

main().catch((e) => {
  console.error('e2e 崩了:', e)
  process.exit(2)
})
