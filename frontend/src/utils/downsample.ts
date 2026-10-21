/**
 * 曲线降采样（LTTB — Largest-Triangle-Three-Buckets）。
 *
 * 为什么用 LTTB 而不是等间隔抽样
 * ------------------------------
 * 等间隔抽样（每隔 N 个取 1 个）会**丢掉尖峰**。试验曲线里峰值恰恰是最
 * 关键的信息（超调量、峰值响应时间都靠它）。LTTB 按「三角形面积最大」
 * 选点，能保住曲线的视觉形状与极值，肉眼几乎看不出和原图的差别。
 *
 * 算法代价 O(n)，10 万点实测 10ms 量级，可以接受。
 *
 * 参考：Steinarsson, "Downsampling Time Series for Visual Representation"
 */

/** 降采样后的结果（含元信息，便于 UI 说明「当前展示 N / M 点」） */
export interface DownsampleResult {
  x: number[]
  y: number[]
  /** 是否真的降过（点数未超阈值时为 false，原样返回） */
  downsampled: boolean
  /** 原始点数 */
  originalCount: number
  /** 输出点数 */
  outputCount: number
}

/**
 * 保留第一个点、最后一个点，中间按 LTTB 选点。
 *
 * @param x        横坐标数组
 * @param y        纵坐标数组
 * @param threshold 目标点数（>=3 才有意义）
 */
export function lttb(x: number[], y: number[], threshold: number): { x: number[]; y: number[] } {
  const n = Math.min(x.length, y.length)
  if (threshold >= n || threshold < 3) {
    return { x: x.slice(0, n), y: y.slice(0, n) }
  }

  const sampledX: number[] = new Array(threshold)
  const sampledY: number[] = new Array(threshold)

  // 每个桶的宽度（除首尾两点外的可用槽位）
  const every = (n - 2) / (threshold - 2)

  // 第一个点固定保留
  sampledX[0] = x[0]
  sampledY[0] = y[0]

  let a = 0 // 上一个被选中的点

  for (let i = 0; i < threshold - 2; i++) {
    // ── 下一个桶的平均点（三角形的「右边」顶点）──
    let avgRangeStart = Math.floor((i + 1) * every) + 1
    let avgRangeEnd = Math.floor((i + 2) * every) + 1
    if (avgRangeEnd > n) avgRangeEnd = n

    let avgX = 0
    let avgY = 0
    const avgRangeLength = Math.max(avgRangeEnd - avgRangeStart, 1)
    for (; avgRangeStart < avgRangeEnd; avgRangeStart++) {
      avgX += x[avgRangeStart]
      avgY += y[avgRangeStart]
    }
    avgX /= avgRangeLength
    avgY /= avgRangeLength

    // ── 当前桶：选出与「上一点 + 下桶平均点」构成面积最大的那个 ──
    let rangeOffs = Math.floor(i * every) + 1
    const rangeTo = Math.floor((i + 1) * every) + 1

    const pointAX = x[a]
    const pointAY = y[a]

    let maxArea = -1
    let maxAreaIndex = rangeOffs

    for (; rangeOffs < rangeTo; rangeOffs++) {
      // 三角形面积（等价于叉积绝对值）
      const area = Math.abs(
        (pointAX - avgX) * (y[rangeOffs] - pointAY) -
          (pointAX - x[rangeOffs]) * (avgY - pointAY),
      )
      if (area > maxArea) {
        maxArea = area
        maxAreaIndex = rangeOffs
      }
    }

    sampledX[i + 1] = x[maxAreaIndex]
    sampledY[i + 1] = y[maxAreaIndex]
    a = maxAreaIndex
  }

  // 最后一个点固定保留
  sampledX[threshold - 1] = x[n - 1]
  sampledY[threshold - 1] = y[n - 1]

  return { x: sampledX, y: sampledY }
}

/**
 * 按阈值降采样：点数未超阈值时原样返回（不做无谓的拷贝与计算）。
 */
export function downsampleIfNeeded(
  x: number[],
  y: number[],
  threshold: number,
): DownsampleResult {
  const n = Math.min(x.length, y.length)
  if (!threshold || n <= threshold) {
    return {
      x,
      y,
      downsampled: false,
      originalCount: n,
      outputCount: n,
    }
  }
  const r = lttb(x, y, threshold)
  return {
    x: r.x,
    y: r.y,
    downsampled: true,
    originalCount: n,
    outputCount: r.x.length,
  }
}
