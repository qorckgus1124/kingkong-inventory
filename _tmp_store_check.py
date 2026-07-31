# -*- coding: utf-8 -*-
"""임시 검증 (실행 후 삭제): 전역 매장 드롭다운 서버 렌더 + 중복 조회 제거 확인"""
import atexit
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_LINES = []
_print = print


def log(*a):
    _LINES.append(" ".join(str(x) for x in a))


@atexit.register
def _flush():
    with open(os.path.join(HERE, "_tmp_store.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(_LINES))


import psycopg2
import psycopg2.pool

QUERIES = []
STORES = [{"id": 1, "name": "강남역점"}, {"id": 2, "name": "홍대점"}]


class Row(dict):
    def __missing__(self, key):
        return None


class FakeCursor:
    def __init__(self):
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = " ".join((sql or "").split())
        QUERIES.append(self.sql)

    def executemany(self, sql, seq):
        pass

    def fetchone(self):
        return Row({"id": 1, "cnt": 1, "count": 1, "name": "강남역점", "value": "0", "key": "x"})

    def fetchall(self):
        s = self.sql.lower()
        if "from stores" in s:
            return [Row(x) for x in STORES]
        return []

    def close(self):
        pass


class FakeConn:
    closed = 0

    def cursor(self, *a, **k):
        return FakeCursor()

    def get_backend_pid(self):
        return 1

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakePool:
    def __init__(self, *a, **k):
        pass

    def getconn(self):
        return FakeConn()

    def putconn(self, c, close=False):
        pass

    def closeall(self):
        pass


psycopg2.pool.ThreadedConnectionPool = lambda *a, **k: FakePool()
os.environ["DATABASE_URL"] = "postgres://u:p@localhost:5432/fake"

import app as appmod  # noqa: E402

client = appmod.app.test_client()
fails = []

log("=== 1. 매장 목록이 HTML에 미리 들어있는지 ===")
for label, url in [("제품관리", "/"), ("판매", "/sales"), ("입출고", "/transactions"),
                   ("이동", "/transfer"), ("대시보드", "/dashboard"), ("일일보고", "/daily_report")]:
    html = client.get(url).get_data(as_text=True)
    has_opts = '<option value="1">강남역점</option>' in html and '<option value="2">홍대점</option>' in html
    has_seed = "window.__GLOBAL_STORES" in html
    log(f"   [{'OK ' if has_opts and has_seed else 'FAIL'}] {label}: 옵션 미리 렌더={has_opts}, 캐시 시드={has_seed}")
    if not (has_opts and has_seed):
        fails.append(f"{label} 매장 옵션 미리 렌더 실패")

log("\n=== 2. change 강제 발생 제거 확인 (base.html) ===")
base = open(os.path.join(HERE, "templates", "base.html"), encoding="utf-8").read()
checks = {
    "syncAllStoreDropdowns가 fireChange 인자를 받는다": "function syncAllStoreDropdowns(storeId, fireChange)" in base,
    "값이 실제로 다를 때만 change 발생": "el.value != storeId" in base,
    "초기 적용 시 change 미발생(인자 없이 호출)": "syncAllStoreDropdowns(sel.value);" in base,
    "사용자 변경 시에만 change 전달": "syncAllStoreDropdowns(val, true)" in base,
    "중복 load 방지": "if (changed > 0) return;" in base,
    "저장값을 파싱 중 즉시 적용": "localStorage.getItem('global_store_id')" in base,
}
for k, v in checks.items():
    log(f"   [{'OK ' if v else 'FAIL'}] {k}")
    if not v:
        fails.append(k)

log("\n=== 3. 한 번 고른 매장이 유지되는지 (자동 덮어쓰기 방지) ===")
ok_no_overwrite = "} else if (!savedId && stores.length > 0) {" in base
log(f"   [{'OK ' if ok_no_overwrite else 'FAIL'}] 저장값이 있으면 첫 매장으로 덮어쓰지 않음")
if not ok_no_overwrite:
    fails.append("귀속 매장 덮어쓰기 방지 실패")

log("\n=== 4. 매장 목록 캐시 (common.js) ===")
cjs = open(os.path.join(HERE, "static", "common.js"), encoding="utf-8").read()
cache_checks = {
    "캐시 읽기/쓰기 함수": "function readStoreListCache" in cjs and "function writeStoreListCache" in cjs,
    "api()가 /api/stores를 캐시에서 즉시 반환": "url === '/api/stores'" in cjs,
    "쓰기 요청 시 캐시 삭제": "clearStoreListCache();" in cjs,
    "즉시 조회 함수": "function getStoresInstant" in cjs,
}
for k, v in cache_checks.items():
    log(f"   [{'OK ' if v else 'FAIL'}] {k}")
    if not v:
        fails.append(k)

log("\n=== 5. 매장 목록 조회 쿼리 (화면 1개 렌더) ===")
QUERIES.clear()
client.get("/")
store_q = [q for q in QUERIES if "from stores" in q.lower()]
log(f"   stores 조회 쿼리 수: {len(store_q)}")
for q in store_q:
    log("     -", q[:80])
if len(store_q) != 1:
    fails.append(f"stores 쿼리 수 이상({len(store_q)})")

log("\n=== 6. 전체 화면 렌더 ===")
for url in ["/", "/dashboard", "/sales", "/transactions", "/transfer", "/stores", "/products" if False else "/settings",
            "/stocktake", "/quick_io", "/recommend_order", "/performance", "/forecast", "/daily_report",
            "/turnover", "/customers", "/brands", "/categories", "/suppliers", "/statistics",
            "/report_builder", "/timemachine"]:
    r = client.get(url)
    ok = r.status_code == 200
    log(f"   [{'OK ' if ok else 'FAIL'}] {url} -> {r.status_code}")
    if not ok:
        fails.append(f"{url} 렌더 실패")

log("\n결과: " + ("모두 통과" if not fails else f"실패 {fails}"))
