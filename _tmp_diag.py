# -*- coding: utf-8 -*-
"""임시 진단 (실행 후 삭제): /api/today_revenue 가 실패하는 조건 찾기"""
import atexit
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_L = []


def log(*a):
    _L.append(" ".join(str(x) for x in a))


@atexit.register
def _f():
    with open(os.path.join(HERE, "_tmp_diag.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(_L))


import psycopg2
import psycopg2.pool
import psycopg2.extras

MODE = {"rollup_missing": False, "settings_value": "0"}


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
        low = self.sql.lower()
        if MODE["rollup_missing"] and "daily_sales_rollup" in low and not low.startswith("create"):
            raise psycopg2.errors.UndefinedTable('relation "daily_sales_rollup" does not exist')

    def executemany(self, sql, seq):
        pass

    @property
    def rowcount(self):
        return 1

    def fetchone(self):
        low = self.sql.lower()
        if "as revenue" in low or "sum(revenue)" in low:
            return Row({"qty": 2, "revenue": 12345, "profit": 5000, "total": 0})
        if "count(" in low:
            return Row({"count": 1, "cnt": 1})
        return Row({"id": 1, "name": "x", "override_amount": None, "qty": 0, "min_qty": 0})

    def fetchall(self):
        low = self.sql.lower()
        if "from settings" in low:
            return [Row({"key": "monthly_target_revenue", "value": MODE["settings_value"]}),
                    Row({"key": "card_fee_rate", "value": "2.5"})]
        if "from stores" in low:
            return [Row({"id": 1, "name": "강남역점"})]
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


psycopg2.pool.ThreadedConnectionPool = lambda *a, **k: type("P", (), {
    "getconn": lambda self: FakeConn(),
    "putconn": lambda self, c, close=False: None,
    "closeall": lambda self: None,
})()
psycopg2.extras.execute_values = lambda cur, sql, args, template=None, page_size=100, fetch=False: None
os.environ["DATABASE_URL"] = "postgres://u:p@localhost:5432/fake"

import app as appmod  # noqa: E402

client = appmod.app.test_client()

log("=== A. 정상 상황 ===")
r = client.get("/api/today_revenue")
log("   status:", r.status_code, "|", str(r.get_json())[:200])

log("\n=== B. 집계표(daily_sales_rollup)가 아직 없는 상황 ===")
MODE["rollup_missing"] = True
r = client.get("/api/today_revenue")
log("   status:", r.status_code, "|", str(r.get_json())[:200])
r2 = client.get("/api/today_revenue?store_id=1")
log("   store_id=1 -> status:", r2.status_code, "|", str(r2.get_json())[:200])
r3 = client.get("/api/statistics?period=day")
log("   /api/statistics -> status:", r3.status_code, "| 건수:", len(r3.get_json() or []))
r4 = client.get("/api/dashboard")
log("   /api/dashboard -> status:", r4.status_code, "| today:", (r4.get_json() or {}).get("today"))
MODE["rollup_missing"] = False

log("\n=== C. 목표 설정값이 빈 문자열인 상황 ===")
MODE["settings_value"] = ""
r = client.get("/api/today_revenue")
log("   status:", r.status_code, "|", str(r.get_json())[:200])
MODE["settings_value"] = "0"

log("\n=== D. 목표 설정값이 소수점 문자열인 상황 ===")
MODE["settings_value"] = "1000000.0"
r = client.get("/api/today_revenue")
log("   status:", r.status_code, "|", str(r.get_json())[:200])
MODE["settings_value"] = "0"

log("\n=== E. 라우트 등록 확인 ===")
rules = [str(x) for x in appmod.app.url_map.iter_rules() if "today" in str(x)]
log("   ", rules)
