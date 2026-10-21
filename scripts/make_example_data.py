# -*- coding: utf-8 -*-
"""
生成一份「最小可用」的真实数据示例目录，用来对照 import_data.py 的目录约定。

产出 <out>/ 下：
    vehicles.csv                 车辆元信息（车型/平台/品牌/驱动方式/车架号/年款）
    conditions.csv               工况元信息（name/category/description）
    示例车A/角阶跃.json           写法 A：{实验号: [params, curves_cal, curves_duiqi]}
    示例车A/角脉冲.json
    示例车B/角阶跃.json           写法 B：{params, curves_cal, curves_duiqi}（扁平单实验）

用法：
    python scripts/make_example_data.py --out ./example_data
"""
import argparse
import json
import math
import os


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


def _params(expr_scale=1.0):
    """指标：raw_key -> 标量。数字前缀可有可无，import_data 都能解析。"""
    return {
        "10-车速(km/h)": round(80.0 * expr_scale, 1),
        "11-阶跃时间(s)": round(0.15 * expr_scale, 3),
        "13-侧向加速度稳态值(g)": round(0.5626 * expr_scale, 4),
        "14-侧向加速度响应时间(s)": round(0.28 * expr_scale, 3),
        "15-横摆角速度超调量(%)": round(20.9979 * expr_scale, 4),
        "16-横摆角速度响应时间(s)": round(0.32 * expr_scale, 3),
        # 故意不带数字前缀，验证解析放宽
        "横摆角速度峰值(g)": round(21.5 * expr_scale, 3),
    }


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


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    print("  写入 %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./example_data")
    args = ap.parse_args()
    root = args.out

    print("生成示例数据 → %s" % os.path.abspath(root))

    # ── vehicles.csv ──
    csv_path = os.path.join(root, "vehicles.csv")
    os.makedirs(root, exist_ok=True)
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
    print("  写入 %s" % csv_path)

    # ── 写法 A：{实验号: [params, curves_cal, curves_duiqi]}，含多次实验 + 平均 ──
    payload_a = {
        "1": [_params(1.00), _curves_cal(), _curves_duiqi()],
        "2": [_params(1.03), _curves_cal(), _curves_duiqi()],
        "平均": [_params(1.015), _curves_cal(), _curves_duiqi()],
    }
    write_json(os.path.join(root, "示例车A", "角阶跃.json"), payload_a)

    # 只有 2 次实验、没有平均 —— 验证「实验次数不固定」
    payload_a2 = {
        "1": [_params(0.95), _curves_cal(), _curves_duiqi()],
        "2": [_params(0.97), _curves_cal(), _curves_duiqi()],
    }
    write_json(os.path.join(root, "示例车A", "角脉冲.json"), payload_a2)

    # ── 写法 B：{params, curves_cal, curves_duiqi}，扁平单实验 ──
    payload_b = {
        "params": _params(1.05),
        "curves_cal": _curves_cal(),
        "curves_duiqi": _curves_duiqi(),
    }
    write_json(os.path.join(root, "示例车B", "角阶跃.json"), payload_b)

    print("\n完成。试跑：")
    print("  python scripts/import_data.py --data-dir %s --dry-run" % root)


if __name__ == "__main__":
    main()
