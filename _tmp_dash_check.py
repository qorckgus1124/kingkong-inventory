# -*- coding: utf-8 -*-
"""임시 검증 (실행 후 삭제): 대시보드 재고 원가 총액 카드 확인"""
import os
import re
import psycopg2
import psycopg2.pool

# 가짜 재고: 제품 3종 (수량 x 원가 = 10*1000 + 5*2000 + 2*30000 = 80,000원)
STOCK_ROW = {"total_cost_value": 80000, "total_sale_value": 150000, "total_profit_potential": 70000}
LAST_SQL = {"sql": ""}


class Row(dict):
    def __missing__(self, key):
        return None


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        LAST_SQL["sql"] = " ".join((sql or "").split())

    def executemany(self, sql, seq):
        pass

    def fetchone(self):
        s = LAST_SQL["sql"].lower()
        if "total_cost_value" in s:
            return Row(STOCK_ROW)
        if "count(" in s:
            return Row({"count": 1, "cnt": 1})
        return Row({"id": 1, "qty": 0, "revenue": 0, "profit": 0, "name": "샘플",
                    "override_amount": None, "value": "0"})

    def fetchall(self):
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

    def putconn(self, conn, close=False):
        pass

    def closeall(self):
        pass


psycopg2.pool.ThreadedConnectionPool = lambda *a, **k: FakePool()
os.environ["DATABASE_URL"] = "postgres://u:p@localhost:5432/fake"

import app as appmod  # noqa: E402

client = appmod.app.test_client()
fails = []

print("=== 1. API가 재고 원가 총액을 내려주는지 ===")
r = client.get("/api/dashboard")
print("   status:", r.status_code)
data = r.get_json()
sv = (data or {}).get("stock_value")
print("   stock_value:", sv)
if r.status_code != 200 or not sv or "total_cost_value" not in sv:
    fails.append("API stock_value 없음")
elif sv["total_cost_value"] != 80000:
    fails.append(f"원가 합계 값 불일치: {sv['total_cost_value']}")

print("\n=== 2. 매장 선택 시에도 정상 응답 ===")
r2 = client.get("/api/dashboard?store_id=1")
print("   status:", r2.status_code, "| stock_value:", (r2.get_json() or {}).get("stock_value"))
if r2.status_code != 200:
    fails.append("매장 지정 대시보드 실패")

print("\n=== 3. 실제 실행된 재고 원가 쿼리 (매장 필터 포함 여부) ===")
# 마지막에 실행된 SQL이 아니라, 재고 가치 쿼리 형태를 직접 확인
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
m = re.search(r"SELECT\s+COALESCE\(SUM\(ss\.qty \* p\.cost_price\), 0\) as total_cost_value.*?WHERE p\.is_active = 1", src, re.S)
print("   원가 합계 쿼리 존재:", bool(m))
print("   매장 필터 코드 존재:", "stock_q += \" AND ss.store_id = %s\"" in src)
if not m:
    fails.append("원가 합계 쿼리 확인 실패")

print("\n=== 4. 대시보드 화면에 카드가 그려지는지 ===")
page = client.get("/dashboard")
html = page.get_data(as_text=True)
print("   status:", page.status_code)
checks = {
    "카드 영역(id=stockCostValue)": 'id="stockCostValue"' in html,
    "범위 표시(id=stockCostScope)": 'id="stockCostScope"' in html,
    "라벨(재고 원가 총액)": "재고 원가 총액" in html,
    "값 채우는 스크립트": "data.stock_value?.total_cost_value" in html,
}
for k, v in checks.items():
    print(f"   [{'OK ' if v else 'FAIL'}] {k}")
    if not v:
        fails.append(k)

print("\n=== 5. 다른 대시보드 요소가 깨지지 않았는지 ===")
for key in ['id="todayRevenue"', 'id="monthProfit"', 'id="forecastRevenue"', 'id="warningList"',
            'id="categoryDoughnutChart"', 'id="trendChart"', 'id="dashboardMemoContent"']:
    ok = key in html
    print(f"   [{'OK ' if ok else 'FAIL'}] {key}")
    if not ok:
        fails.append(f"기존 요소 사라짐 {key}")

print("\n결과:", "모두 통과" if not fails else f"실패 {fails}")
