# -*- coding: utf-8 -*-
"""임시 검증 (실행 후 삭제)
가짜 DB로 검색 규칙(LIKE 패턴/토큰 AND/재고 0 숨김)과 쿼리 횟수를 확인한다.
가짜 DB는 SQL을 실제로 실행하지 않으므로, 생성된 SQL과 파라미터를 검사하고
파이썬 쪽 매칭 규칙은 별도로 직접 실행해서 확인한다.
"""
import atexit
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_tmp_search.txt")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_LINES = []


def log(*a):
    _LINES.append(" ".join(str(x) for x in a))


@atexit.register
def _flush():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(_LINES))


import psycopg2
import psycopg2.pool

QUERIES = []
PRODUCTS = [
    {"id": i, "name": f"제품{i}", "brand_id": 1, "category_id": 2, "cost_price": 100,
     "card_cost_price": 103, "sale_price": 200, "is_active": 1, "parent_product_id": None}
    for i in range(1, 41)
]


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
        QUERIES.append((self.sql, list(params) if params else []))

    def executemany(self, sql, seq):
        pass

    def fetchone(self):
        return Row({"id": 1, "cnt": 1, "count": 1, "name": "샘플", "color": "#fff", "qty": 1, "min_qty": 0})

    def fetchall(self):
        s = self.sql.lower()
        # 집계 쿼리를 먼저 판별해야 한다 (일반 제품 조회 패턴과 겹치기 때문)
        if "group by p.brand_id, c.name" in s:
            return [Row({"brand_id": 1, "category_name": "액상", "cnt": 5}),
                    Row({"brand_id": 1, "category_name": "일회용", "cnt": 2})]
        if "group by brand_id" in s:
            return [Row({"brand_id": 1, "cnt": 7})]
        if "parent_product_id" in s and "group by" in s:
            return []
        if "from store_stock" in s:
            return [Row({"product_id": p["id"], "qty": 3, "min_qty": 1}) for p in PRODUCTS]
        if "from products p" in s and "select p." in s:
            return [Row(p) for p in PRODUCTS]
        if "from brands" in s and "group by" not in s:
            return [Row({"id": 1, "name": "펀치밤", "color": "#111", "category_id": 2, "status": "approved"})]
        if "from categories" in s:
            return [Row({"id": 2, "name": "액상", "color": "#222"})]
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

# ---------------------------------------------------------------- 1. 매칭 규칙
log("=== 1. 검색어 처리 규칙 (파이썬 함수 직접 확인) ===")
log("   escape_like('2%')  ->", repr(appmod.escape_like("2%")))
if appmod.escape_like("2%") != "2\\%":
    fails.append("escape_like")
log("   search_tokens('밤 백향') ->", appmod.search_tokens("밤 백향"))
if appmod.search_tokens("밤 백향") != ["밤", "백향"]:
    fails.append("search_tokens")

cond, params = appmod.build_token_match_sql("밤 백향", ["pname", "bname"])
log("   조건 SQL:", cond)
log("   파라미터:", params)
if params != ["%밤%", "%밤%", "%백향%", "%백향%"]:
    fails.append("토큰 파라미터")
if cond.count(" AND ") != 1 or cond.count(" OR ") != 2:
    fails.append("토큰 AND/OR 구조")

cond2, params2 = appmod.build_token_match_sql("2%", ["pname"])
log("   '2%' 파라미터:", params2, "| ESCAPE 포함:", "ESCAPE" in cond2)
if params2 != ["%2\\%%"] or "ESCAPE" not in cond2:
    fails.append("2% 이스케이프")

# ---------------------------------------------------------------- 2. 실제 SQL 생성
def find_main_query(keyword="FROM products p"):
    """get_db()의 헬스체크/타임존 설정 등을 건너뛰고 실제 목록 조회 SQL을 찾는다."""
    for sql, params in QUERIES:
        if keyword in sql and "SELECT p.*" in sql:
            return sql, params
    for sql, params in QUERIES:
        if keyword in sql:
            return sql, params
    return "", []


def count_real_queries():
    """헬스체크(SELECT 1) / SET TIME ZONE 같은 부수 쿼리를 뺀 개수"""
    n = 0
    for sql, _ in QUERIES:
        low = sql.lower()
        if low.startswith("select 1") or low.startswith("set time zone"):
            continue
        n += 1
    return n


log("\n=== 2. 제품 검색 API가 만드는 SQL ===")
QUERIES.clear()
r = client.get("/api/products?q=" + "%EB%B0%A4%20%EB%B0%B1%ED%96%A5" + "&hide_zero_stock=1")
log("   status:", r.status_code, "| 실행 쿼리 수(부수 제외):", count_real_queries())
main_sql, main_params = find_main_query()
log("   토큰 조건 2개 포함:", main_sql.count("ESCAPE") >= 4)
log("   재고0 숨김(EXISTS) 포함:", "EXISTS ( SELECT 1 FROM store_stock" in main_sql or "EXISTS (SELECT 1 FROM store_stock" in main_sql)
log("   파라미터:", main_params)
if r.status_code != 200:
    fails.append("제품 검색 API 실패")
if "store_stock" not in main_sql:
    fails.append("재고0 숨김 조건 없음")
if count_real_queries() > 6:
    fails.append(f"쿼리 과다({count_real_queries()}건)")

log("\n   재고0 숨김 해제 시:")
QUERIES.clear()
client.get("/api/products?q=test&hide_zero_stock=0")
main_sql2, _ = find_main_query()
log("   store_stock 조건 포함:", "store_stock" in main_sql2)
if "store_stock" in main_sql2:
    fails.append("hide_zero_stock=0인데 조건이 들어감")

log("\n=== 3. 자동완성(/api/products/search) ===")
QUERIES.clear()
r = client.get("/api/products/search?q=%EB%B0%A4%20%EB%B0%B1%ED%96%A5&store_id=1")
log("   status:", r.status_code, "| 쿼리 수:", len(QUERIES), "| 반환:", len(r.get_json() or []))
sql0 = QUERIES[0][0] if QUERIES else ""
log("   관련도 정렬(ORDER BY CASE) 포함:", "ORDER BY CASE WHEN" in sql0)
log("   LIMIT 포함:", "LIMIT" in sql0)
if r.status_code != 200 or "ORDER BY CASE WHEN" not in sql0:
    fails.append("자동완성 SQL 이상")
if len(QUERIES) > 6:
    fails.append(f"자동완성 쿼리 과다({len(QUERIES)}건)")

log("\n=== 4. 브랜드 검색(제품명 매칭 + 카테고리) ===")
QUERIES.clear()
r = client.get("/api/brands?search=%EB%B0%A4%20%EB%B0%B1%ED%96%A5")
log("   status:", r.status_code, "| 쿼리 수:", len(QUERIES))
bsql = QUERIES[0][0] if QUERIES else ""
log("   제품명 EXISTS 매칭 포함:", "FROM products bp" in bsql)
data = r.get_json() or []
log("   응답 예시:", data[0] if data else None)
if r.status_code != 200 or "FROM products bp" not in bsql:
    fails.append("브랜드 검색이 제품명을 보지 않음")
if not data or "categories" not in data[0]:
    fails.append("브랜드 응답에 categories 없음")
elif [c["name"] for c in data[0]["categories"]] != ["액상", "일회용"]:
    fails.append(f"카테고리 구성 이상: {data[0]['categories']}")
if len(QUERIES) > 4:
    fails.append(f"브랜드 쿼리 과다({len(QUERIES)}건)")

log("\n=== 5. 제품 40건 조회 시 쿼리 수 (N+1 확인) ===")
QUERIES.clear()
r = client.get("/api/products?q=제품")
log("   반환 건수:", len(r.get_json() or []), "| 쿼리 수:", len(QUERIES))
if len(QUERIES) > 6:
    fails.append(f"N+1 남아있음({len(QUERIES)}건)")
keys = set((r.get_json() or [{}])[0].keys())
need = {"qty", "min_qty", "margin_rate", "brand_name", "brand_color", "category_name",
        "category_color", "variant_count", "is_variant", "parent_name"}
log("   빠진 항목:", (need - keys) or "없음")
if need - keys:
    fails.append(f"응답 키 누락 {need - keys}")

log("\n=== 6. 화면 렌더 ===")
for label, url in [("제품관리", "/"), ("판매", "/sales"), ("이동", "/transfer"), ("입출고", "/transactions"),
                   ("발주추천", "/recommend_order"), ("판매실적", "/performance"), ("매장", "/stores"),
                   ("빠른입출고", "/quick_io"), ("대시보드", "/dashboard")]:
    resp = client.get(url)
    ok = resp.status_code == 200
    log(f"   [{'OK ' if ok else 'FAIL'}] {label} {url} -> {resp.status_code}")
    if not ok:
        fails.append(f"{label} 렌더 실패")

html = client.get("/").get_data(as_text=True)
for key in ['id="includeZeroStock"', "hide_zero_stock", "apiSearch("]:
    ok = key in html
    log(f"   [{'OK ' if ok else 'FAIL'}] 제품관리 화면에 {key}")
    if not ok:
        fails.append(f"제품관리 {key} 누락")

log("\n결과: " + ("모두 통과" if not fails else f"실패 {fails}"))
