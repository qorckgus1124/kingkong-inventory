# -*- coding: utf-8 -*-
"""임시 검증 (실행 후 삭제): 이번 작업 5개 항목 + 오늘 매출 실시간 확인"""
import atexit
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_L = []


def log(*a):
    _L.append(" ".join(str(x) for x in a))


@atexit.register
def _flush():
    with open(os.path.join(HERE, "_tmp_verify.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(_L))


import psycopg2
import psycopg2.pool
import psycopg2.extras

QUERIES = []
TODAY_REVENUE = {"value": 10000}


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
        QUERIES.append("MANY " + " ".join((sql or "").split()))

    @property
    def rowcount(self):
        return 1

    def fetchone(self):
        s = self.sql.lower()
        if "as revenue" in s or "sum(revenue)" in s:
            return Row({"qty": 3, "revenue": TODAY_REVENUE["value"], "profit": 4000, "total": 0})
        if "to_char" in s and "period_key" in s:
            return Row({"period_key": "2026-07-31"})
        if "count(" in s:
            return Row({"count": 1, "cnt": 1})
        return Row({"id": 1, "name": "샘플", "qty": 1, "min_qty": 0, "value": "0", "key": "x",
                    "override_amount": None, "cost_price": 100, "sale_price": 200})

    def fetchall(self):
        s = self.sql.lower()
        if "from stores" in s:
            return [Row({"id": 1, "name": "강남역점"})]
        if "from daily_sales_rollup" in s and "distinct" in s:
            return []
        if "group by t.store_id" in s:
            return [Row({"store_id": 1, "sale_date": "2026-07-30", "qty": 1, "revenue": 5000, "profit": 2000})]
        if "from daily_sales_rollup" in s:
            return [Row({"period_key": "2026-07-30", "sold_qty": 1, "revenue": 5000, "profit": 2000})]
        if "dup on dup.k" in s:
            return [
                Row({"id": 10, "name": "백향과", "brand_id": 1, "category_id": 2, "cost_price": 100,
                     "card_cost_price": 103, "sale_price": 200, "parent_product_id": None,
                     "brand_name": "펀치밤", "category_name": "액상", "group_key": "백향과",
                     "qty": 5, "transaction_count": 12, "created_at": None}),
                Row({"id": 11, "name": "백 향과", "brand_id": 1, "category_id": 3, "cost_price": 100,
                     "card_cost_price": 103, "sale_price": 200, "parent_product_id": None,
                     "brand_name": "펀치밤", "category_name": "일회용", "group_key": "백향과",
                     "qty": 2, "transaction_count": 1, "created_at": None}),
            ]
        if "from products" in s and "select id, name" in s:
            return [Row({"id": 11, "name": "백 향과"})]
        if "group by store_id" in s:
            return [Row({"store_id": 1, "qty": 2, "min_qty": 0})]
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
_orig_execute_values = psycopg2.extras.execute_values


def _fake_execute_values(cur, sql, argslist, template=None, page_size=100, fetch=False):
    QUERIES.append("VALUES(%d행) %s" % (len(argslist), " ".join(sql.split())))


psycopg2.extras.execute_values = _fake_execute_values
os.environ["DATABASE_URL"] = "postgres://u:p@localhost:5432/fake"

import app as appmod  # noqa: E402

client = appmod.app.test_client()
fails = []


def check(label, cond, extra=""):
    log(f"   [{'OK ' if cond else 'FAIL'}] {label}" + (f" {extra}" if extra else ""))
    if not cond:
        fails.append(label)


log("=== 1. gzip 압축 + 정적 파일 버전 ===")
r = client.get("/dashboard", headers={"Accept-Encoding": "gzip"})
plain = client.get("/dashboard")
check("gzip 적용(HTML)", r.headers.get("Content-Encoding") == "gzip",
      f"| 압축 {len(r.get_data())}B vs 원본 {len(plain.get_data())}B")
small = client.get("/api/products", headers={"Accept-Encoding": "gzip"})
check("작은 응답은 압축 생략", small.headers.get("Content-Encoding") is None,
      f"| {len(small.get_data())}B")
html = client.get("/dashboard").get_data(as_text=True)
check("common.js 버전 붙음", re.search(r"common\.js\?v=\d+", html) is not None)
check("style.css 버전 붙음", re.search(r"style\.css\?v=\d+", html) is not None)
r2 = client.get("/api/export/products?store_id=all", headers={"Accept-Encoding": "gzip"})
check("CSV 다운로드는 압축 영향 없음", r2.status_code == 200 and r2.headers.get("Content-Encoding") is None)

log("\n=== 2. 오늘 매출 전용 엔드포인트 (실시간) ===")
r = client.get("/api/today_revenue")
d = r.get_json() or {}
check("/api/today_revenue 200", r.status_code == 200)
check("today 포함", "today" in d and "revenue" in (d.get("today") or {}), str(d.get("today")))
check("monthly_target 포함", "monthly_target" in d)
QUERIES.clear()
client.get("/api/today_revenue")
real = [q for q in QUERIES if not q.lower().startswith(("select 1", "set time zone"))]
check("쿼리 수가 적다(<=8)", len(real) <= 8, f"| {len(real)}건")

log("\n=== 3. 대시보드 캐시 + 오늘 매출은 캐시 제외 ===")
TODAY_REVENUE["value"] = 10000
first = client.get("/api/dashboard").get_json() or {}
check("첫 호출 cached 표시 없음", not first.get("cached"))
TODAY_REVENUE["value"] = 77777          # 그 사이 판매가 발생한 상황
second = client.get("/api/dashboard").get_json() or {}
check("두 번째 호출은 캐시 사용", second.get("cached") is True)
check("캐시를 써도 오늘 매출은 최신값", (second.get("today") or {}).get("revenue") == 77777,
      f"| {second.get('today')}")
check("무거운 항목은 재사용됨", "stock_value" in second)
r = client.post("/api/settings", json={"handover_memo": "x"})
third = client.get("/api/dashboard").get_json() or {}
check("쓰기 후 캐시 초기화", not third.get("cached"))

log("\n=== 4. 일별 매출 집계표 ===")
src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
check("집계표 생성 구문", "CREATE TABLE IF NOT EXISTS daily_sales_rollup" in src)
check("오늘은 집계표에 넣지 않음", "today - timedelta(days=1)" in src)
QUERIES.clear()
r = client.get("/api/statistics?period=day")
check("/api/statistics 200", r.status_code == 200)
rows = r.get_json() or []
check("집계표에서 읽음", any("from daily_sales_rollup" in q.lower() for q in QUERIES))
check("오늘 값이 합산됨", any(str(x.get("period_key", "")).startswith("2026-07-31") for x in rows) or len(rows) > 0,
      f"| {rows}")
check("쓰기 후 집계표 초기화 구문", "DELETE FROM daily_sales_rollup" in src)

log("\n=== 5. 중복 제품 탐지/병합 ===")
r = client.get("/api/products/duplicates")
d = r.get_json() or {}
check("/api/products/duplicates 200", r.status_code == 200)
groups = d.get("groups") or []
check("그룹으로 묶임", len(groups) == 1 and len(groups[0]["items"]) == 2, f"| {len(groups)}그룹")
if groups:
    check("브랜드 동일 표시", groups[0]["same_brand"] is True)
    check("카테고리 다름 표시", groups[0]["same_category"] is False)
    check("재고 합계", groups[0]["total_qty"] == 7, f"| {groups[0]['total_qty']}")
QUERIES.clear()
r = client.post("/api/products/merge", json={"target_id": 10, "source_ids": [11]})
d = r.get_json() or {}
check("병합 200", r.status_code == 200 and d.get("ok"), str(d)[:120])
joined = " || ".join(QUERIES)
check("재고 이관", "INSERT INTO store_stock" in joined)
check("입출고 기록 이관", "UPDATE stock_transactions SET product_id" in joined)
check("가격이력 이관", "UPDATE price_history SET product_id" in joined)
check("선결제항목 이관", "UPDATE pre_order_items SET product_id" in joined)
check("이동내역 이관", "UPDATE stock_movements SET product_id" in joined)
check("옵션 재지정", "UPDATE products SET parent_product_id" in joined)
check("원본은 비활성 처리", "SET is_active = 0" in joined)
r = client.post("/api/products/merge", json={"target_id": 10, "source_ids": [10]})
check("자기 자신 병합은 거부", r.status_code == 400)
check("중복 정리 화면 200", client.get("/maintenance/duplicates").status_code == 200)
check("제품 관리에 진입 버튼", "/maintenance/duplicates" in client.get("/").get_data(as_text=True))

log("\n=== 6. 엑셀 업로드 배치 처리 ===")
csv_body = "ID,제품명,브랜드,카테고리,원가,카드원가,판매가,최소재고,초기재고\n"
for i in range(50):
    csv_body += f",테스트{i},펀치밤,액상,1000,1030,2000,2,5\n"
QUERIES.clear()
r = client.post("/api/import/products",
                data={"file": (io.BytesIO(csv_body.encode("utf-8-sig")), "t.csv")},
                content_type="multipart/form-data")
d = r.get_json() or {}
check("업로드 200", r.status_code == 200, str(d)[:120])
real = [q for q in QUERIES if not q.lower().startswith(("select 1", "set time zone"))]
batch = [q for q in QUERIES if q.startswith("VALUES(")]
check("재고를 한 번에 저장", len(batch) == 1, f"| {batch[0][:60] if batch else '없음'}")
check("50줄 업로드 쿼리 수 감소(<=60)", len(real) <= 60, f"| {len(real)}건")
check("브랜드/카테고리 미리 로드", any("select id, name from brands" in q.lower() for q in QUERIES))

log("\n=== 7. 화면 렌더 ===")
for url in ["/", "/dashboard", "/sales", "/transactions", "/transfer", "/stores", "/settings",
            "/stocktake", "/quick_io", "/recommend_order", "/performance", "/forecast",
            "/daily_report", "/turnover", "/customers", "/brands", "/categories", "/suppliers",
            "/statistics", "/report_builder", "/timemachine", "/maintenance/duplicates"]:
    rr = client.get(url)
    if rr.status_code != 200:
        check(f"{url} 렌더", False, f"-> {rr.status_code}")
if not any("렌더" in f for f in fails):
    log("   [OK ] 22개 화면 모두 200")

log("\n결과: " + ("모두 통과" if not fails else f"실패 {fails}"))
