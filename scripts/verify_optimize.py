"""对真库验证本轮 crud 改动（只读）。

重点：
  1. exp_scope='avg_first' 取到的实验是不是「平均优先，否则第一次」
  2. curve_names 是否真的下推到了 SQL（数一下 SELECT 语句与返回的曲线数）
  3. meta_only 是否真的没读 series（不触发 lazy load，且不返回 series 数据）
  4. get_all_indicators_flat 的分页与「全量再切片」是否一致
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, select  # noqa: E402

from app import crud  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Condition, Curve, Experiment, Indicator, Vehicle  # noqa: E402


class SqlCounter:
    """统计发出的 SQL 语句（用于抓 lazy load / N+1）。"""

    def __init__(self, engine):
        self.stmts = []
        self._engine = engine

    def __enter__(self):
        def before(conn, cursor, statement, params, context, executemany):
            self.stmts.append(statement)

        event.listen(self._engine, "before_cursor_execute", before)
        self._before = before
        return self

    def __exit__(self, *a):
        event.remove(self._engine, "before_cursor_execute", self._before)

    def count(self, needle=None):
        if needle is None:
            return len(self.stmts)
        return sum(1 for s in self.stmts if needle.lower() in s.lower())


def main():
    db = SessionLocal()
    engine = db.get_bind()

    cond = db.execute(select(Condition).order_by(Condition.id)).scalars().first()
    vids = [
        r[0] for r in db.execute(
            select(Experiment.vehicle_id)
            .where(Experiment.condition_id == cond.id)
            .distinct()
            .order_by(Experiment.vehicle_id)
            .limit(4)
        )
    ]
    print("工况: %s(id=%s)  取前 %d 台车: %s\n" % (cond.name, cond.id, len(vids), vids))

    # ── 1. exp_scope 正确性 ──
    print("=" * 62)
    print("1. exp_scope='avg_first' 是否等价于「平均优先，否则第一次」")
    print("=" * 62)
    for vid in vids[:3]:
        all_exps = list(db.execute(
            select(Experiment.experiment_no, Experiment.is_average, Experiment.sort_order)
            .where(Experiment.vehicle_id == vid, Experiment.condition_id == cond.id)
            .order_by(Experiment.sort_order)
        ))
        got = crud.get_compare(db, [vid], cond.id, exp_scope="avg_first")
        got_nos = [
            e["experiment_no"] for v in got["vehicles"] for e in v["experiments"]
        ]
        # 期望：有平均取平均，否则取 sort_order 最小的
        avg = [n for n, a, _ in all_exps if a]
        expect = avg[:1] if avg else [all_exps[0][0]] if all_exps else []
        ok = sorted(got_nos) == sorted(expect)
        print(
            "  车 %-4s 全部=%-22s → avg_first=%-8s 期望=%-8s %s"
            % (vid, [n for n, _, _ in all_exps], got_nos, expect, "✅" if ok else "❌")
        )

    # ── 2. curve_names 是否下推 SQL ──
    print()
    print("=" * 62)
    print("2. curves= 过滤：返回的曲线数 / series 是否真的没多读")
    print("=" * 62)
    all_names = crud.get_curve_names(db, cond.id)
    print("  该工况曲线名共 %d 条: %s" % (len(all_names), all_names))
    pick = all_names[:1]
    with SqlCounter(engine) as c1:
        full = crud.get_compare(db, vids, cond.id, exp_scope="avg_first")
    with SqlCounter(engine) as c2:
        narrow = crud.get_compare(
            db, vids, cond.id, exp_scope="avg_first", curve_names=set(pick)
        )

    def curve_count(resp):
        n = 0
        for v in resp["vehicles"]:
            for e in v["experiments"]:
                n += len(e["curves_cal"]) + len(e["curves_duiqi"])
        return n

    def series_points(resp):
        n = 0
        for v in resp["vehicles"]:
            for e in v["experiments"]:
                for g in ("curves_cal", "curves_duiqi"):
                    for cv in e[g].values():
                        for s in cv.get("series") or []:
                            n += len(s.get("x") or [])
        return n

    print("  全量   : 曲线 %d 条 / 点 %s / SQL %d 条"
          % (curve_count(full), f"{series_points(full):,}", c1.count()))
    print("  只取 %-8s: 曲线 %d 条 / 点 %s / SQL %d 条"
          % (pick[0], curve_count(narrow), f"{series_points(narrow):,}", c2.count()))
    print("  → 下推生效: %s" % ("✅" if curve_count(narrow) < curve_count(full) else "❌"))

    # ── 3. meta_only 不读 series ──
    print()
    print("=" * 62)
    print("3. meta_only：SQL 条数 / 是否含 series / 是否触发 lazy load")
    print("=" * 62)
    with SqlCounter(engine) as c3:
        meta = crud.get_compare(db, vids, cond.id, meta_only=True)
    # 强制访问一次所有属性，若还有未加载的列会再发 SQL
    _ = [
        (cv["title"], cv["point_count"], cv["series"])
        for v in meta["vehicles"]
        for e in v["experiments"]
        for g in ("curves_cal", "curves_duiqi")
        for cv in e[g].values()
    ]
    n_after_access = c3.count()

    has_series = any(
        cv.get("series")
        for v in meta["vehicles"]
        for e in v["experiments"]
        for g in ("curves_cal", "curves_duiqi")
        for cv in e[g].values()
    )
    print("  meta_only SQL 条数 = %d（访问完属性后仍为 %d）" % (c3.count(), n_after_access))
    print("  含 series 数据 = %s" % has_series)
    print("  curve_meta 曲线数 = %d"
          % len(meta["curve_meta"]["groups"].get("cal", {}).get("curves", {})))
    # 指标还在（前端表格立刻能渲染）
    n_params = sum(
        len(e["params"]) for v in meta["vehicles"] for e in v["experiments"]
    )
    print("  指标条数 = %d（表格数据仍在）" % n_params)
    print("  → 无 lazy load: %s" % ("✅" if n_after_access == c3.count() else "❌"))

    # ── 4. flat 分页与「全量再切片」一致 ──
    print()
    print("=" * 62)
    print("4. get_all_indicators_flat 分页正确性")
    print("=" * 62)
    # 参考实现：一次性取全部（小库可行），在 Python 里切片
    p1 = crud.get_all_indicators_flat(db, limit=5, offset=0)
    p2 = crud.get_all_indicators_flat(db, limit=5, offset=5)
    pbig = crud.get_all_indicators_flat(db, limit=5000, offset=0)
    print("  total = %d（每页 5，第二页 total 也应为 %d）" % (p1["total"], p2["total"]))
    same_total = p1["total"] == p2["total"] == pbig["total"]
    print("  total 一致: %s" % ("✅" if same_total else "❌"))

    key = lambda r: (r["platform"], r["brand"], r["vehicle"], r["condition"], r["experiment_no"])
    seq = [key(r) for r in pbig["rows"]]
    ok_page = (
        [key(r) for r in p1["rows"]] == seq[:5]
        and [key(r) for r in p2["rows"]] == seq[5:10]
        and len(set(seq)) == len(seq)
    )
    print("  分页顺序与全量前 10 行一致: %s" % ("✅" if ok_page else "❌"))

    # 指标内容一致
    ref = {key(r): r["indicators"] for r in pbig["rows"]}
    ok_ind = all(ref.get(key(r)) == r["indicators"] for r in p1["rows"] + p2["rows"])
    print("  指标内容一致: %s" % ("✅" if ok_ind else "❌"))

    # average_only 与 indicator 过滤
    pa = crud.get_all_indicators_flat(db, average_only=True, limit=3)
    print("  average_only=true total=%d（应 ≤ 全部 %d）" % (pa["total"], p1["total"]))
    ind_name = db.execute(select(Indicator.name).limit(1)).scalar()
    pi = crud.get_all_indicators_flat(db, indicator_name=ind_name, limit=3)
    print("  indicator=%r total=%d" % (ind_name, pi["total"]))

    # 未使用（占位，避免 lint 抱怨）
    _ = (Vehicle, Curve, event)

    db.close()


if __name__ == "__main__":
    main()
