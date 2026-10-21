# -*- coding: utf-8 -*-
"""
生成一份「最小可用」的真实数据示例目录，用来对照 import_data.py 的目录约定。

产出 <out>/ 下::

    vehicles.csv                     车辆元信息（车型/平台/品牌/驱动方式/车架号/年款）
    conditions.csv                   工况元信息（name/category/description）
    README.md                        给看的人：这份示例在演示什么
    示例车A/角阶跃.cw                 msgpack，文件名就是工况名
    示例车A/角脉冲.json               JSON
    示例车B/20241021-001.cw           文件名无意义 → 靠内容里的指标名识别
    示例车B/20241021-002.json
    示例车C/稳态回转_2024-10-22.cw    文件名含工况名但带前后缀 → 取最长匹配

为什么角阶跃和角脉冲的指标不一样
--------------------------------
「按内容识别工况」靠的是指标名集合。如果两个工况的指标名完全相同，
任何算法都分不出来 —— 那时只能认怂、保留文件名。所以示例里刻意让
两种工况用各自的指标（阶跃看「阶跃时间」，脉冲看「脉冲宽度 / 谐振频率」），
这样「文件名随便起」才能真的认出来。

用法::

    python scripts/make_example_data.py --out ./example_data
"""
import argparse
import json
import math
import os
import shutil

# 每个工况一套指标：raw_key -> 标量。数字前缀可有可无，import_data 都能解析。
STEP_PARAMS = {
    "10-车速(km/h)": 80.0,
    "11-阶跃时间(s)": 0.15,
    "13-侧向加速度稳态值(g)": 0.5626,
    "14-侧向加速度响应时间(s)": 0.28,
    "15-横摆角速度超调量(%)": 20.9979,
    "16-横摆角速度响应时间(s)": 0.32,
    # 故意不带数字前缀，验证解析放宽
    "横摆角速度峰值(g)": 21.5,
}

PULSE_PARAMS = {
    "10-车速(km/h)": 80.0,
    "11-脉冲宽度(s)": 0.42,
    "12-方向盘转角峰值(deg)": 45.3,
    "13-横摆角速度峰值(deg/s)": 18.76,
    "14-横摆角速度峰值时间(s)": 0.21,
    "15-谐振频率(Hz)": 1.35,
    "16-相位滞后角(deg)": -12.4,
    # 这一项是给「按内容识别工况」留的指纹：库里脉冲族其它工况都没有它，
    # 于是这批数据能被唯一认成「角脉冲」而不是「角脉冲-右打」。
    "17-脉冲响应衰减率(%)": 62.4,
}

STEADY_PARAMS = {
    "10-车速(km/h)": 60.0,
    "11-转弯半径(m)": 40.0,
    "12-侧向加速度(g)": 0.45,
    "13-方向盘转角(deg)": 92.5,
    "14-不足转向度(deg/g)": 2.18,
    "15-侧倾梯度(deg/g)": 4.62,
    "16-转向半径比": 0.986,
    "17-前后轴侧偏角差(deg)": 0.73,
}

PARAMS_BY_CONDITION = {
    "角阶跃": STEP_PARAMS,
    "角脉冲": PULSE_PARAMS,
    "稳态回转": STEADY_PARAMS,
}


def _wave(n, amp, freq, noise=0.0):
    """造一条像试验曲线的数据：正弦 + 指数趋势。"""
    x = [round(i * 0.005, 3) for i in range(n)]
    y = []
    for i in range(n):
        t = x[i]
        v = amp * (1 - math.exp(-t / 0.4)) + amp * 0.25 * math.sin(freq * t)
        if noise:
            v += noise * math.sin(i * 7.13)
        y.append(round(v, 4))
    return x, y


def _params(condition, scale=1.0):
    """指标：raw_key -> 标量。按工况取各自那套，别混用。"""
    base = PARAMS_BY_CONDITION.get(condition, STEP_PARAMS)
    return {k: round(v * scale, 4) for k, v in base.items()}


def _curves_cal(n=200):
    """计算曲线组：一条曲线两个 series（原始数据 / 滤波）。"""
    out = {}
    specs = [
        ("方向盘转角-时间", "方向盘转角vs时间", "时间（s）", "方向盘转角（deg）", 5.0, 8.0),
        ("侧向加速度-时间", "侧向加速度vs时间", "时间（s）", "侧向加速度（m/s^2）", 6.0, 25.0),
        ("横摆角速度-时间", "横摆角速度vs时间", "时间（s）", "横摆角速度（deg/s）", 20.0, 25.0),
    ]
    for name, title, xlabel, ylabel, amp, freq in specs:
        x, y_raw = _wave(n, amp, freq, noise=0.35)
        _, y_filt = _wave(n, amp, freq)
        out[name] = {
            "curve": [
                {"x": x, "y": y_raw, "curvetype": "line", "curvename": "原始数据"},
                {"x": x, "y": y_filt, "curvetype": "line", "curvename": "滤波"},
            ],
            "title": title,
            "xlabel": xlabel,
            "ylabel": ylabel,
        }
    return out


def _curves_duiqi(n=200):
    """对齐曲线组：只有滤波 series。"""
    out = {}
    for name, c in _curves_cal(n).items():
        out[name] = {
            "curve": [s for s in c["curve"] if s["curvename"] == "滤波"],
            "title": c["title"],
            "xlabel": c["xlabel"],
            "ylabel": c["ylabel"],
        }
    return out


def write_any(path, obj):
    """按后缀落盘：.cw / .msgpack / .msg 写 msgpack，其余写 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    low = path.lower()
    if low.endswith((".cw", ".msgpack", ".msg", ".mpack")):
        import msgpack  # 延迟导入：没装 msgpack 也能生成 JSON 示例

        with open(path, "wb") as fh:
            fh.write(msgpack.packb(obj, use_bin_type=True))
        print("  写入 %s（msgpack）" % path)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        print("  写入 %s（JSON）" % path)


def payload_multi(condition, base=1.0):
    """写法 A：{实验号: [params, curves_cal, curves_duiqi]}，含多次实验 + 平均。"""
    return {
        "1": [_params(condition, base), _curves_cal(), _curves_duiqi()],
        "2": [_params(condition, base * 1.03), _curves_cal(), _curves_duiqi()],
        "平均": [_params(condition, base * 1.015), _curves_cal(), _curves_duiqi()],
    }


def payload_two_runs(condition, base=1.0):
    """只有 2 次实验、没有平均 —— 验证「实验次数不固定」。"""
    return {
        "1": [_params(condition, base), _curves_cal(), _curves_duiqi()],
        "2": [_params(condition, base * 1.02), _curves_cal(), _curves_duiqi()],
    }


def payload_flat(condition, base=1.0):
    """写法 B：{params, curves_cal, curves_duiqi}，扁平单实验。"""
    return {
        "params": _params(condition, base),
        "curves_cal": _curves_cal(),
        "curves_duiqi": _curves_duiqi(),
    }


README = """# 示例数据（import_data / 上传页面都能直接用）

```
example_data/
├── vehicles.csv                     车辆元信息（可选，没有就只剩车名）
├── conditions.csv                   工况分类（可选，name,category,description）
├── 示例车A/
│   ├── 角阶跃.cw                    msgpack，文件名就是工况名
│   └── 角脉冲.json                  JSON
├── 示例车B/
│   ├── 20241021-001.cw              文件名无意义 → 靠内容里的指标名认工况
│   └── 20241021-002.json
└── 示例车C/
    └── 稳态回转_2024-10-22.cw       文件名含工况名但带前后缀 → 最长匹配
```

## 目录约定

- **第一层目录 = 车名**。可以带单号：`极光EV-01(CS-2024-001)`。
- **中间层目录 = 试验模式**（可多层，按 `/` 拼）。没有中间层就是无模式。
- **文件名 = 工况**。但文件名不必严格等于工况名，见下。

## 文件名不必是工况名

判定顺序（在 `app/importer.py` 的 `ItemBuilder._resolve_conditions`）：

1. 文件名**完全等于**已知工况名 → 用它；
2. 文件名**包含**已知工况名（如 `稳态回转_2024-10-22`）→ 取最长匹配；
3. 以上都不行 → 拿内容里的**指标名集合**去比对，挑唯一最像的工况；
4. 还是定不下来 → 保留文件名当工况名，并在导入结果里列出候选。

第 3 步能成立的前提是：**不同工况的指标名组合不一样**。
示例里角阶跃看「阶跃时间」，角脉冲看「脉冲宽度 / 谐振频率」，
所以 `20241021-001.cw` 这种纯编号文件名也能认出来。

> 反过来，如果两个工况的指标名完全一样（只差数值），任何算法都分不出来。
> 这时请让文件名带上工况名，否则数据会被归到错误工况 —— 那比认不出更麻烦。

## 后缀

| 后缀 | 说明 |
|---|---|
| `.json` | 标准 JSON（utf-8，可带 BOM） |
| `.msgpack` `.msg` `.mpack` | msgpack，算法侧常见落盘格式 |
| `.cw` | 业务自定义后缀，内容同样是 msgpack |

后缀不认识时还会嗅探首字节，所以偶尔改了扩展名的 msgpack 也能读。
msgpack 需要 `pip install msgpack`。

## 内容格式

两种写法都支持：

```jsonc
// A：多次实验 + 平均
{"1": [params, curves_cal, curves_duiqi],
 "2": [...],
 "平均": [...]}

// B：扁平单实验
{"params": {...}, "curves_cal": {...}, "curves_duiqi": {...}}
```

`平均` / `avg` / `average` 会被识别为平均实验。

## 试跑

```bash
# 只看解析结果，不写库
python scripts/import_data.py --data-dir ./example_data --dry-run

# 正式导入（默认先清空）
python scripts/import_data.py --data-dir ./example_data
```
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./example_data")
    ap.add_argument("--keep", action="store_true",
                    help="保留目录里已有的其它文件（默认只清掉旧的示例车目录）")
    args = ap.parse_args()
    root = args.out

    print("生成示例数据 → %s" % os.path.abspath(root))
    os.makedirs(root, exist_ok=True)

    # 清掉上一次生成的示例车目录，避免新旧文件混在一起
    if not args.keep:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and name.startswith("示例车"):
                shutil.rmtree(p)

    # ── vehicles.csv ──
    csv_path = os.path.join(root, "vehicles.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("车型,平台,品牌,驱动方式,车架号,年款\n")
        fh.write("示例车A,EPA1,星河,后驱,LGWEF6E40PF000001,2024\n")
        fh.write("示例车B,EPA2,星河,四驱,LGWEF6E40PF000002,2025\n")
        fh.write("示例车C,EPA2,云驰,前驱,LGWEF6E40PF000003,2025\n")
    print("  写入 %s" % csv_path)

    # ── conditions.csv ──
    csv_path = os.path.join(root, "conditions.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("name,category,description\n")
        fh.write("角阶跃,操纵性,方向盘阶跃输入下的瞬态响应\n")
        fh.write("角脉冲,操纵性,方向盘脉冲输入下的频率响应\n")
        fh.write("稳态回转,稳态,定半径稳态回转\n")
    print("  写入 %s" % csv_path)

    # ── README ──
    readme_path = os.path.join(root, "README.md")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(README)
    print("  写入 %s" % readme_path)

    # ── 示例车A：文件名就是工况名 ──
    write_any(os.path.join(root, "示例车A", "角阶跃.cw"),
              payload_multi("角阶跃"))
    write_any(os.path.join(root, "示例车A", "角脉冲.json"),
              payload_two_runs("角脉冲"))

    # ── 示例车B：文件名纯编号 → 靠内容识别 ──
    write_any(os.path.join(root, "示例车B", "20241021-001.cw"),
              payload_multi("角阶跃", 1.05))
    write_any(os.path.join(root, "示例车B", "20241021-002.json"),
              payload_flat("角脉冲", 0.98))

    # ── 示例车C：文件名含工况名但带前后缀 → 最长匹配 ──
    write_any(os.path.join(root, "示例车C", "稳态回转_2024-10-22.cw"),
              payload_multi("稳态回转", 1.0))

    print("\n完成。试跑：")
    print("  python scripts/import_data.py --data-dir %s --dry-run" % root)


if __name__ == "__main__":
    main()
