"""验证 /api/compare/stream（NDJSON 流式端点）。

四件事，全部对真库 + 真 HTTP：

  1. **等价性** —— 流式拼出来的数据和 /api/compare 的 JSON 逐字段一致
     （曲线 series 的 x/y 数组逐个数字比）。
  2. **服务端内存** —— 同一场景下两条路径的后端进程 RSS 峰值增量。
     这是流式存在的理由：老路径要把整个响应 dumps 成一个几百 MB 的
     字符串（实测放大 ~8x），新路径只持有「一批曲线」。
  3. **首行延迟** —— 流式收到第一行（meta）的时刻，对照老路径要等
     整个响应 body 收完。用户感知的「表格多久出来」就是它。
  4. **gzip 是否缓冲住流** —— 流式响应必须边生成边到达，如果
     GZipMiddleware 把数据攒在 zlib 缓冲里，这个测试会看到首行延迟
     接近总时长。

会临时起一个 uvicorn，跑完自己关掉。
"""
import gzip as _gzip
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Condition, Experiment  # noqa: E402

PORT = int(os.getenv("VERIFY_PORT", "8898"))
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 工具 ────────────────────────────────────────────────────────────────

def _h(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024)
    return "%.1f MB" % (n / 1024 / 1024)


class RssSampler:
    """后台线程轮询某个进程的 RSS 峰值。

    为什么要单独一个线程：请求是**阻塞**发出的，不采样的那段正好是
    内存峰值所在的区间。轮询间隔 50ms，对这个量级的请求够用。
    """

    def __init__(self, pid):
        self.pid = pid
        self.base = 0
        self.peak = 0
        self._stop = threading.Event()
        self._th = None

    def __enter__(self):
        import psutil

        p = psutil.Process(self.pid)
        self.base = p.memory_info().rss
        self.peak = self.base

        def loop():
            while not self._stop.wait(0.05):
                try:
                    r = p.memory_info().rss
                except Exception:
                    break
                if r > self.peak:
                    self.peak = r

        self._th = threading.Thread(target=loop, daemon=True)
        self._th.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2)

    @property
    def delta_mb(self):
        return (self.peak - self.base) / 1024.0 / 1024.0


def iter_ndjson(path, gzip_ok=True):
    """流式读 NDJSON，逐行 yield ``(obj, elapsed_seconds)``。

    手动做 gzip 流式解压 —— urllib 不会自动解压，而一次性 ``gzip.decompress``
    又要求先收完整个 body，那样就测不出「首行什么时候到」了。
    """
    req = urllib.request.Request(BASE + path)
    if gzip_ok:
        req.add_header("Accept-Encoding", "gzip")
    else:
        req.add_header("Accept-Encoding", "identity")

    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        enc = (r.headers.get("Content-Encoding") or "").lower()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else None
        buf = b""
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            if dec is not None:
                chunk = dec.decompress(chunk)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line), time.time() - t0
        if dec is not None:
            buf += dec.flush()
        if buf.strip():
            yield json.loads(buf), time.time() - t0


def fetch_json(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Accept-Encoding", "gzip")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = _gzip.decompress(raw)
    return json.loads(raw), time.time() - t0


# ── 1. 等价性 ───────────────────────────────────────────────────────────

def _collect_stream(path):
    """把流式响应拼回 /api/compare 的结构。"""
    meta = None
    curves = {}
    done = None
    first_ms = None
    for obj, el in iter_ndjson(path):
        t = obj.get("type")
        if t == "meta":
            meta = obj
            first_ms = el
        elif t == "curve":
            curves[(obj["experiment_id"], obj["group"], obj["curve"]["name"])] = obj["curve"]
        elif t == "done":
            done = obj
        elif t == "error":
            raise RuntimeError("流里返回 error 行: %s" % obj.get("detail"))
    assert meta is not None, "没收到 meta 行"
    # 把 series 挂回骨架（前端就是这么做的）
    for v in meta["vehicles"]:
        for e in v["experiments"]:
            for g, key in (("cal", "curves_cal"), ("duiqi", "curves_duiqi")):
                for name in list(e[key].keys()):
                    got = curves.get((e["experiment_id"], g, name))
                    if got is not None:
                        e[key][name] = got
    return meta, done, first_ms


def _first_diff(a, b, path="$"):
    """返回第一处不同（用于定位等价性失败在哪）。"""
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        return "%s 类型不同: %s vs %s" % (path, type(a).__name__, type(b).__name__)
    if isinstance(a, dict):
        for k in set(a) | set(b):
            if k not in a:
                return "%s 流式多出键 %r" % (path, k)
            if k not in b:
                return "%s 普通端点多出键 %r" % (path, k)
            d = _first_diff(a[k], b[k], "%s.%s" % (path, k))
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s 长度不同: %d vs %d" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    if a != b:
        return "%s 值不同: %r vs %r" % (path, a, b)
    return None


def check_equivalence(vids, cond_id, curves):
    print("=" * 66)
    print("1. 等价性：流式拼装结果 vs /api/compare 的 JSON")
    print("=" * 66)
    q = urllib.parse.urlencode({
        "vehicle_ids": ",".join(str(v) for v in vids),
        "condition_id": cond_id,
        "curves": ",".join(curves),
        "exp_scope": "avg_first",
    })
    plain, _ = fetch_json("/api/compare?" + q)
    streamed, done, first_ms = _collect_stream("/api/compare/stream?" + q)

    # curve_meta 里的 series_names 流式拿不到（骨架不含 series）—— 这是设计如此，
    # 比较前先把它抹平，否则会误报。
    for src in (plain, streamed):
        for g in (src.get("curve_meta") or {}).get("groups", {}).values():
            g["series_names"] = []
            for c in g["curves"].values():
                c["series_names"] = []
    # 流式 meta 行多带的字段不属于 /api/compare 的形状
    for k in ("type",):
        streamed.pop(k, None)

    diff = _first_diff(streamed, plain)
    n_cv = done["curves"]
    n_pt = done["points"]
    print("  普通端点: vehicles=%d" % len(plain["vehicles"]))
    print("  流式    : vehicles=%d  curves=%d  points=%s  首行=%.3fs"
          % (len(streamed["vehicles"]), n_cv, f"{n_pt:,}", first_ms))
    print("  → 逐字段一致: %s" % ("✅" if diff is None else "❌ " + str(diff)))
    return diff is None


# ── 2 & 3. 内存 / 首行延迟 ──────────────────────────────────────────────

def bench(vids, cond_id, curves, label):
    print()
    print("=" * 66)
    print("2/3. 内存峰值 + 首行延迟 —— %s" % label)
    print("=" * 66)
    q = urllib.parse.urlencode({
        "vehicle_ids": ",".join(str(v) for v in vids),
        "condition_id": cond_id,
        "curves": ",".join(curves),
        "exp_scope": "avg_first",
    })

    # 先各跑一次热身，把连接池 / 预编译 SQL 的开销排除掉
    fetch_json("/api/compare?" + q)
    for _ in iter_ndjson("/api/compare/stream?" + q):
        pass

    import psutil

    pid = _server_pid()
    with RssSampler(pid) as s:
        _, dt_plain = fetch_json("/api/compare?" + q)
    plain_mb = s.delta_mb

    with RssSampler(pid) as s:
        n_pt = 0
        for obj, _ in iter_ndjson("/api/compare/stream?" + q):
            if obj.get("type") == "done":
                n_pt = obj["points"]
    stream_mb = s.delta_mb

    print("  %-28s %10s %10s %10s" % ("路径", "RSS 峰值增量", "总耗时", "首行"))
    print("  %-28s %9.1f MB %9.2fs %10s"
          % ("① /api/compare", plain_mb, dt_plain, "—（要等 body 收完）"))

    # 单独再测一次流式的首行延迟（RSS 采样会干扰计时，分开跑）
    t0 = time.time()
    first = None
    last = None
    for obj, el in iter_ndjson("/api/compare/stream?" + q):
        if obj.get("type") == "meta" and first is None:
            first = el
        last = el
    print("  %-28s %9.1f MB %9.2fs %9.3fs"
          % ("② /api/compare/stream", stream_mb, last, first))
    print("  → 峰值内存 %.2f× 更低" % (plain_mb / stream_mb) if stream_mb > 0 else "")
    print("  → 首行比收完整 body 快 %.2fs（用户先看到表格）"
          % (dt_plain - first))
    return stream_mb, plain_mb


def _server_pid():
    """uvicorn 单进程（RELOAD=0），Popen 的 pid 就是服务进程。"""
    return _SRV.pid


# ── 4. gzip 是否缓冲住流 ────────────────────────────────────────────────

def check_gzip_streaming(vids, cond_id, curves):
    print()
    print("=" * 66)
    print("4. gzip 会不会把流式数据攒在缓冲里")
    print("=" * 66)
    q = urllib.parse.urlencode({
        "vehicle_ids": ",".join(str(v) for v in vids),
        "condition_id": cond_id,
        "curves": ",".join(curves),
        "exp_scope": "avg_first",
    })
    for gz in (True, False):
        req = urllib.request.Request(BASE + "/api/compare/stream?" + q)
        req.add_header("Accept-Encoding", "gzip" if gz else "identity")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=600) as r:
            enc = (r.headers.get("Content-Encoding") or "-").lower()
            marks = []
            dec = (zlib.decompressobj(16 + zlib.MAX_WBITS)
                   if enc == "gzip" else None)
            raw_n = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                raw_n += len(chunk)
                if len(marks) < 3:
                    marks.append(time.time() - t0)
            total = time.time() - t0
        print("  Accept-Encoding=%-8s → Content-Encoding=%-6s  "
              "前 3 块到达=%s  总耗时=%.2fs"
              % ("gzip" if gz else "identity", enc,
                 " ".join("%.3fs" % m for m in marks), total))
    print("  说明：若 gzip 那行的前几块时间明显晚于 identity，说明被缓冲住了。")


def main():
    db = SessionLocal()
    cond = db.execute(
        select(Condition).where(Condition.name == "稳态回转")
    ).scalars().first()
    if cond is None:
        cond = db.execute(select(Condition).order_by(Condition.id)).scalars().first()
    vids_all = [
        r[0] for r in db.execute(
            select(Experiment.vehicle_id)
            .where(Experiment.condition_id == cond.id)
            .distinct().order_by(Experiment.vehicle_id)
        )
    ]
    db.close()

    from app import crud
    from app.database import SessionLocal as SL

    d2 = SL()
    names = crud.get_curve_names(d2, cond.id)
    d2.close()

    print("工况: %s (id=%s)  可选车辆 %d 台  曲线 %d 条"
          % (cond.name, cond.id, len(vids_all), len(names)))
    print("曲线: %s\n" % names)

    env = dict(os.environ)
    env.update({
        "GZIP": "1", "GZIP_LEVEL": "1", "RELOAD": "0",
        "PORT": str(PORT), "PYTHONUNBUFFERED": "1",
    })
    log = open(os.path.join(ROOT, ".verify-stream-server.log"), "w")
    global _SRV
    _SRV = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    ok = False
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            log.flush()
            print("服务没起来：")
            print(open(os.path.join(ROOT, ".verify-stream-server.log")).read()[-3000:])
            return

        # 1. 等价性：小场景（3 台车 × 2 条曲线），要逐数字比
        ok = check_equivalence(vids_all[:3], cond.id, names[:2])

        # 2/3. 内存与首行：用「50 台车 × 全部曲线」这个有分量的场景
        bench(vids_all[:50], cond.id, names, "50 台车 × 平均 × 全部 %d 条曲线" % len(names))

        # 4. gzip
        check_gzip_streaming(vids_all[:20], cond.id, names[:3])

        print()
        print("=" * 66)
        print("等价性: %s" % ("✅ 通过" if ok else "❌ 失败"))
        print("=" * 66)
    finally:
        _SRV.terminate()
        try:
            _SRV.wait(timeout=10)
        except Exception:
            _SRV.kill()
        log.close()


if __name__ == "__main__":
    main()
