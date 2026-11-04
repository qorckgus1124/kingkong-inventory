"""어드민 전용 매장 리포트 (/admin/reports).

한 화면에서 매장별 상태(매출·추이·베스트셀러·회전율·재고·필요한 것·운영·고객)를 보고
매장끼리 비교한다. 매장 계정에는 메뉴도 데이터도 노출하지 않는다.

- 경로가 /admin/ , /api/admin/ 아래라 auth.py의 _STORE_BLOCKED_* 에서 한 번 막히고,
  모든 라우트에 @admin_required 를 한 번 더 건다(이중 방어).
- 모든 지표는 이 파일의 '공용 지표' 부분에서만 계산하고 모든 탭이 같은 함수를 쓴다.
- 지표 기준은 /api/analysis(compute_analysis)와 같다:
  제외 = 선결출고, 이동/교환 메모 거래, 옵션 부모 제품, 퀵상품(unlimited_stock).
"""
import csv
import io
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, Response, g, jsonify, render_template, request

import auth

bp = Blueprint("admin_reports", __name__)

# app.py가 init_admin_reports()로 넣어 준다 (순환 import를 피하려는 의존성 주입)
_get_db = None
_now_kst = None
_rollback = None
# 매출 기준 (설계 4장 '확정이 필요한 기준'의 기본값. 아래 두 값만 바꾸면 전체가 따라온다)
#  - 보정 매출(daily_revenue_override)은 지표에 섞지 않고 점검 화면에만 따로 보여준다.
#  - 선결제는 '선결예약' 거래가 기록된 시점(예약 시점)에 매출로 잡는다. (기존 매출 통계와 동일)
APPLY_REVENUE_OVERRIDE = False

SALE_TYPES = ("판매출고", "선결예약", "판매취소")
GOOD_SALE_TYPES = ("판매출고", "선결예약")

# ---------------------------------------------------------------------------
# 설정 (이상 징후 기준값) - settings 테이블에 저장
# ---------------------------------------------------------------------------
THRESHOLD_DEFAULTS = {
    "report_revenue_drop_pct": 30,      # 최근 7일 평균 대비 이만큼(%) 이상 떨어지면 매출 급락
    "report_cancel_multiplier": 2,      # 취소 건수가 평균의 이 배수 이상이면 과다
    "report_stocktake_diff_units": 10,  # 최근 7일 실사 차이 합(수량)이 이 이상이면 경고
}
THRESHOLD_LIMITS = {
    "report_revenue_drop_pct": (1, 100),
    "report_cancel_multiplier": (1, 50),
    "report_stocktake_diff_units": (1, 100000),
}


def init_admin_reports(app, get_db, now_kst, rollback_quietly):
    global _get_db, _now_kst, _rollback
    _get_db, _now_kst, _rollback = get_db, now_kst, rollback_quietly
    app.register_blueprint(bp)


# ---------------------------------------------------------------------------
# 작은 도구들
# ---------------------------------------------------------------------------
def _n(v, default=0):
    """Decimal/None -> int 또는 float"""
    if v is None:
        return default
    f = float(v)
    return int(f) if f == int(f) else round(f, 2)


def _pct(a, b, digits=1):
    """a / b * 100 (b가 0이면 None)"""
    if not b:
        return None
    return round(a * 100.0 / b, digits)


def _change_pct(cur_v, prev_v):
    """지난 기간 대비 증감률(%). 지난 기간이 0이면 None."""
    if not prev_v:
        return None
    return round((cur_v - prev_v) * 100.0 / abs(prev_v), 1)


class ReportError(ValueError):
    pass


def _cursor():
    _get_db()
    return g.cursor


def _q(sql, params=None):
    cur = _cursor()
    cur.execute(sql, params or [])
    return [dict(r) for r in cur.fetchall()]


# 짧은 응답 캐시 (같은 어드민이 탭을 오가거나 새로고침할 때 DB를 덜 때린다)
_CACHE = {}
CACHE_TTL = 30.0


def _cached(key, fn):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = fn()
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# 기간 / 매장 파라미터 검증
# ---------------------------------------------------------------------------
def parse_period(args):
    """period=today|7d|30d|month|custom (+start_date,end_date) -> 기간 dict.
    지난 기간 = 같은 길이의 바로 앞 기간."""
    today = _now_kst().date()
    period = (args.get("period") or "7d").strip()
    if period == "today":
        start, end = today, today
    elif period == "7d":
        start, end = today - timedelta(days=6), today
    elif period == "30d":
        start, end = today - timedelta(days=29), today
    elif period == "month":
        start, end = today.replace(day=1), today
    elif period == "custom":
        try:
            start = datetime.strptime((args.get("start_date") or "").strip(), "%Y-%m-%d").date()
            end = datetime.strptime((args.get("end_date") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ReportError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    else:
        raise ReportError("기간 값이 올바르지 않습니다.")
    if start > end:
        raise ReportError("시작일이 종료일보다 늦습니다.")
    days = (end - start).days + 1
    if days > 3660:
        raise ReportError("조회 기간은 최대 10년까지 가능합니다.")
    return {
        "key": period,
        "start": start,
        "end": end,
        "days": days,
        "prev_start": start - timedelta(days=days),
        "prev_end": start - timedelta(days=1),
    }


def _period_json(p):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in p.items()}


def _dt_range(start, end):
    """날짜 범위(양 끝 포함) -> [start 00:00, end+1 00:00) datetime"""
    return (datetime.combine(start, datetime.min.time()),
            datetime.combine(end + timedelta(days=1), datetime.min.time()))


def _int_arg(args, name, default, lo, hi):
    raw = args.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(float(str(raw).strip()))
    except ValueError:
        raise ReportError(f"'{name}' 값이 올바른 숫자가 아닙니다.")
    return min(max(v, lo), hi)


def _get_store(store_id):
    rows = _q("SELECT id, name FROM stores WHERE id = %s", [store_id])
    if not rows:
        raise ReportError("존재하지 않는 매장입니다.")
    return rows[0]


def _all_stores():
    return _q("SELECT id, name FROM stores ORDER BY id")


# ===========================================================================
# 공용 지표 (모든 탭이 이 함수들만 쓴다)
# ===========================================================================
# 순판매수량 = 판매출고 + 선결예약 - 판매취소
# 매출 = Σ 수량 x 판매단가 (취소는 음수) / 이익 = Σ 수량 x (판매단가 - 원가단가)
# 이익률 = 이익/매출 / 객단가 = 매출/결제 건수 (같은 매장·유형·분단위 시각 = 1건)
# 회전 횟수 = 기간 판매수량/현재고 / 소진 예상일 = 현재고/(기간 판매수량/일수)
# 재고 금액 = Σ 재고 x 원가
SIGNED = "(CASE WHEN t.type = '판매취소' THEN -t.quantity::bigint ELSE t.quantity::bigint END)"
PRICE = "COALESCE(t.unit_price, 0)::bigint"
COST = "COALESCE(t.unit_cost, 0)::bigint"
REV_SQL = f"COALESCE(SUM({SIGNED} * {PRICE}), 0)"
QTY_SQL = f"COALESCE(SUM({SIGNED}), 0)"
PROFIT_SQL = f"COALESCE(SUM({SIGNED} * ({PRICE} - {COST})), 0)"

# 옵션이 있는 부모 제품은 묶음 라벨이라 제외, 퀵상품(재고 무관)도 제외
PRODUCT_OK_SQL = (
    "NOT EXISTS (SELECT 1 FROM products v WHERE v.parent_product_id = p.id AND v.is_active = 1) "
    "AND COALESCE(p.unlimited_stock, 0) = 0"
)
MEMO_OK_SQL = "(t.memo IS NULL OR (t.memo NOT LIKE %s AND t.memo NOT LIKE %s))"
MEMO_PARAMS = ["%이동%", "%교환%"]


def _sales_where(start, end, store_ids=None, types=SALE_TYPES):
    """판매 거래 FROM/WHERE 조각과 파라미터. t=stock_transactions, p=products"""
    d0, d1 = _dt_range(start, end)
    sql = (
        " FROM stock_transactions t JOIN products p ON p.id = t.product_id"
        " WHERE t.type = ANY(%s) AND t.date_time >= %s AND t.date_time < %s"
        f" AND {MEMO_OK_SQL} AND {PRODUCT_OK_SQL}"
    )
    params = [list(types), d0, d1] + MEMO_PARAMS
    if store_ids is not None:
        sql += " AND t.store_id = ANY(%s)"
        params.append(list(store_ids))
    return sql, params


def metrics_by_store(start, end, store_ids=None):
    """매장별 핵심 지표. {store_id: {qty, revenue, profit, margin, orders, aov}} (거래가 없는 매장은 0)"""
    frm, params = _sales_where(start, end, store_ids)
    rows = _q(f"SELECT t.store_id, {QTY_SQL} AS qty, {REV_SQL} AS revenue, {PROFIT_SQL} AS profit"
              f"{frm} GROUP BY t.store_id", params)
    frm2, params2 = _sales_where(start, end, store_ids, GOOD_SALE_TYPES)
    order_rows = _q(
        "SELECT t.store_id, COUNT(DISTINCT (t.type || date_trunc('minute', t.date_time)::text)) AS orders"
        f"{frm2} GROUP BY t.store_id", params2)
    orders = {r["store_id"]: int(r["orders"]) for r in order_rows}
    out = {}
    ids = store_ids if store_ids is not None else [s["id"] for s in _all_stores()]
    for sid in ids:
        out[sid] = {"qty": 0, "revenue": 0, "profit": 0, "orders": orders.get(sid, 0)}
    for r in rows:
        m = out.setdefault(r["store_id"], {"qty": 0, "revenue": 0, "profit": 0, "orders": 0})
        m["qty"], m["revenue"], m["profit"] = _n(r["qty"]), _n(r["revenue"]), _n(r["profit"])
        m["orders"] = orders.get(r["store_id"], 0)
    for m in out.values():
        m["margin"] = _pct(m["profit"], m["revenue"])
        m["aov"] = round(m["revenue"] / m["orders"]) if m["orders"] else None
    return out


def daily_series(start, end, store_ids=None):
    """{store_id: {'YYYY-MM-DD': {revenue, profit, qty}}}"""
    frm, params = _sales_where(start, end, store_ids)
    rows = _q(f"SELECT t.store_id, date(t.date_time) AS d, {REV_SQL} AS revenue, {PROFIT_SQL} AS profit, "
              f"{QTY_SQL} AS qty{frm} GROUP BY t.store_id, date(t.date_time)", params)
    out = defaultdict(dict)
    for r in rows:
        out[r["store_id"]][r["d"].isoformat()] = {
            "revenue": _n(r["revenue"]), "profit": _n(r["profit"]), "qty": _n(r["qty"])}
    return out


def _date_labels(start, end):
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _lead_metrics(m, prev):
    """현재/직전 기간 지표 -> 증감률까지 붙인 dict"""
    out = dict(m)
    out["prev"] = prev
    for k in ("revenue", "profit", "qty", "orders"):
        out[f"{k}_change"] = _change_pct(m[k], prev[k])
    return out


# ---- 재고 행렬: 매장 x 제품 (재고, 최소수량, 원가, 기간 판매수량) ----
def stock_matrix(start, end, store_ids=None, product_ids=None):
    d0, d1 = _dt_range(start, end)
    params = [list(SALE_TYPES), d0, d1] + MEMO_PARAMS
    sql = f"""
        WITH s AS (
            SELECT t.store_id, t.product_id, {QTY_SQL} AS sold
            FROM stock_transactions t
            WHERE t.type = ANY(%s) AND t.date_time >= %s AND t.date_time < %s AND {MEMO_OK_SQL}
            GROUP BY t.store_id, t.product_id
        )
        SELECT ss.store_id, p.id AS product_id, p.name, p.option_name,
               b.name AS brand, c.name AS category,
               ss.qty, COALESCE(ss.min_qty, 0) AS min_qty,
               COALESCE(spc.cost_price, p.cost_price, 0) AS cost, COALESCE(p.sale_price, 0) AS price,
               COALESCE(s.sold, 0) AS sold
        FROM store_stock ss
        JOIN products p ON p.id = ss.product_id
        LEFT JOIN brands b ON b.id = p.brand_id
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN store_product_cost spc ON spc.store_id = ss.store_id AND spc.product_id = ss.product_id
        LEFT JOIN s ON s.store_id = ss.store_id AND s.product_id = ss.product_id
        WHERE p.is_active = 1 AND {PRODUCT_OK_SQL}
    """
    if store_ids is not None:
        sql += " AND ss.store_id = ANY(%s)"
        params.append(list(store_ids))
    if product_ids is not None:
        sql += " AND p.id = ANY(%s)"
        params.append(list(product_ids))
    rows = _q(sql, params)
    for r in rows:
        r["qty"] = int(r["qty"] or 0)
        r["min_qty"] = int(r["min_qty"] or 0)
        r["cost"] = int(r["cost"] or 0)
        r["price"] = int(r["price"] or 0)
        r["sold"] = max(int(r["sold"] or 0), 0)
        r["value"] = max(r["qty"], 0) * r["cost"]
    return rows


def _days_in(start, end):
    return (end - start).days + 1


def derive_stock_fields(r, days):
    """행에 회전 횟수/소진 예상일/상태를 붙인다 (모든 탭 공용)"""
    sold, qty = r["sold"], r["qty"]
    avg = sold / days if days > 0 else 0
    r["avg_daily"] = round(avg, 2)
    r["days_to_deplete"] = round(qty / avg, 1) if avg > 0 and qty >= 0 else None
    r["turnover"] = round(sold / qty, 2) if qty > 0 else None
    r["status"] = stock_status(r)
    r["label"] = r["name"]
    return r


def stock_status(r):
    qty, mn, sold = r["qty"], r["min_qty"], r["sold"]
    dd = r.get("days_to_deplete")
    if qty <= 0:
        return "품절"
    if (mn > 0 and qty <= mn) or (dd is not None and dd <= 7):
        return "부족"
    if sold <= 0:
        return "부진"
    if dd is not None and dd > 90:
        return "과다"
    return "정상"


def _is_low(r):
    """재고 부족 품목: 최소수량 이하이거나, 팔리는데 품절/소진 임박"""
    if r["min_qty"] > 0 and r["qty"] <= r["min_qty"]:
        return True
    if r["sold"] > 0 and (r["qty"] <= 0 or (r.get("days_to_deplete") or 999) <= 7):
        return True
    return False


# ===========================================================================
# 이상 징후 기준값
# ===========================================================================
def load_thresholds():
    rows = _q("SELECT key, value FROM settings WHERE key = ANY(%s)", [list(THRESHOLD_DEFAULTS)])
    out = dict(THRESHOLD_DEFAULTS)
    for r in rows:
        try:
            out[r["key"]] = float(r["value"]) if "." in str(r["value"]) else int(r["value"])
        except (TypeError, ValueError):
            pass
    return out


def compute_anomalies(stores, th):
    """매장별 이상 징후: 어제 매출 급락, 취소 과다, 최근 7일 실사 차이."""
    today = _now_kst().date()
    yday = today - timedelta(days=1)
    base_start = yday - timedelta(days=7)
    series = daily_series(base_start, yday, [s["id"] for s in stores])
    d0, d1 = _dt_range(base_start, yday)
    cancel_rows = _q(
        "SELECT t.store_id, date(t.date_time) AS d, COUNT(*) AS c FROM stock_transactions t "
        "WHERE t.type = '판매취소' AND t.date_time >= %s AND t.date_time < %s GROUP BY 1, 2", [d0, d1])
    cancels = defaultdict(dict)
    for r in cancel_rows:
        cancels[r["store_id"]][r["d"]] = int(r["c"])
    edits = defaultdict(dict)
    try:
        for r in _q("SELECT store_id, date(edited_at) AS d, COUNT(*) AS c FROM tx_edit_log "
                    "WHERE edited_at >= %s AND edited_at < %s GROUP BY 1, 2", [d0, d1]):
            edits[r["store_id"]][r["d"]] = int(r["c"])
    except Exception:
        _rollback()
    s0, s1 = _dt_range(today - timedelta(days=6), today)
    st_rows = _q(
        "SELECT store_id, COUNT(*) AS n, COALESCE(SUM(ABS(COALESCE(after_qty,0) - COALESCE(before_qty,0))),0) AS diff "
        "FROM stock_transactions WHERE type = '실사조정' AND date_time >= %s AND date_time < %s GROUP BY 1", [s0, s1])
    stocktake = {r["store_id"]: r for r in st_rows}

    alerts = []
    prior_days = [yday - timedelta(days=i) for i in range(1, 8)]
    for s in stores:
        sid, name = s["id"], s["name"]
        sr = series.get(sid, {})
        y_rev = sr.get(yday.isoformat(), {}).get("revenue", 0)
        avg = sum(sr.get(d.isoformat(), {}).get("revenue", 0) for d in prior_days) / 7.0
        if avg > 0 and y_rev <= avg * (1 - th["report_revenue_drop_pct"] / 100.0):
            drop = round((y_rev - avg) * 100.0 / avg, 1)
            alerts.append({"store_id": sid, "store": name, "kind": "revenue_drop", "severity": "danger",
                           "message": f"{yday.strftime('%m/%d')} 매출이 최근 7일 평균 대비 {drop}% (어제 {y_rev:,}원 / 평균 {round(avg):,}원)"})
        cc = cancels.get(sid, {})
        y_c = cc.get(yday, 0)
        c_avg = sum(cc.get(d, 0) for d in prior_days) / 7.0
        if y_c >= 3 and y_c >= max(c_avg, 0.5) * th["report_cancel_multiplier"]:
            alerts.append({"store_id": sid, "store": name, "kind": "cancel_spike", "severity": "warning",
                           "message": f"{yday.strftime('%m/%d')} 판매취소 {y_c}건 (최근 7일 평균 {round(c_avg, 1)}건)"})
        ee = edits.get(sid, {})
        y_e = ee.get(yday, 0)
        e_avg = sum(ee.get(d, 0) for d in prior_days) / 7.0
        if y_e >= 3 and y_e >= max(e_avg, 0.5) * th["report_cancel_multiplier"]:
            alerts.append({"store_id": sid, "store": name, "kind": "edit_spike", "severity": "warning",
                           "message": f"{yday.strftime('%m/%d')} 거래 수정 {y_e}건 (최근 7일 평균 {round(e_avg, 1)}건)"})
        st = stocktake.get(sid)
        if st and int(st["diff"]) >= th["report_stocktake_diff_units"]:
            alerts.append({"store_id": sid, "store": name, "kind": "stocktake_diff", "severity": "warning",
                           "message": f"최근 7일 재고 실사 차이 {int(st['diff'])}개 ({int(st['n'])}건 조정)"})
    return alerts


def compute_imbalance(stores, start, end, limit=30):
    """한쪽은 부족/품절인데 다른 쪽은 잉여인 제품 -> 이동 추천."""
    days = _days_in(start, end)
    ids = [s["id"] for s in stores]
    names = {s["id"]: s["name"] for s in stores}
    rows = stock_matrix(start, end, ids)
    by_prod = defaultdict(list)
    for r in rows:
        derive_stock_fields(r, days)
        by_prod[r["product_id"]].append(r)
    out = []
    for pid, items in by_prod.items():
        needy = [r for r in items if r["sold"] > 0 and (r["qty"] <= 0 or _is_low(r))]
        if not needy:
            continue
        for n in needy:
            target = max(n["min_qty"], int(math.ceil(n["avg_daily"] * 14)))
            need = max(target - max(n["qty"], 0), 0)
            if need <= 0:
                continue
            donors = []
            for d in items:
                if d["store_id"] == n["store_id"]:
                    continue
                keep = max(d["min_qty"], int(math.ceil(d["avg_daily"] * 21)))
                surplus = d["qty"] - keep
                if surplus >= 3:
                    donors.append((surplus, d))
            if not donors:
                continue
            donors.sort(key=lambda x: -x[0])
            surplus, d = donors[0]
            out.append({
                "product_id": pid, "product": n["name"], "brand": n["brand"],
                "to_store_id": n["store_id"], "to_store": names.get(n["store_id"]),
                "to_qty": n["qty"], "to_days_to_deplete": n["days_to_deplete"],
                "from_store_id": d["store_id"], "from_store": names.get(d["store_id"]),
                "from_qty": d["qty"], "surplus": surplus,
                "suggest_qty": int(min(surplus, need)),
                "value": int(min(surplus, need)) * n["cost"],
            })
    out.sort(key=lambda r: (r["to_qty"] > 0, -(r["suggest_qty"] * (r["value"] or 1))))
    return out[:limit]


# ===========================================================================
# 전체 비교 (overview)
# ===========================================================================
def build_overview(p):
    stores = _all_stores()
    ids = [s["id"] for s in stores]
    cur_m = metrics_by_store(p["start"], p["end"], ids)
    prev_m = metrics_by_store(p["prev_start"], p["prev_end"], ids)
    series = daily_series(p["start"], p["end"], ids)
    labels = _date_labels(p["start"], p["end"])

    # 재고 금액/부족 품목 (최근 30일 판매 속도 기준)
    s30 = _now_kst().date()
    matrix = stock_matrix(s30 - timedelta(days=29), s30, ids)
    stock_info = {sid: {"value": 0, "low": 0, "items": 0} for sid in ids}
    for r in matrix:
        derive_stock_fields(r, 30)
        si = stock_info[r["store_id"]]
        si["value"] += r["value"]
        si["items"] += 1
        if _is_low(r):
            si["low"] += 1

    total_rev = sum(m["revenue"] for m in cur_m.values())
    cards = []
    for s in stores:
        m = _lead_metrics(cur_m[s["id"]], prev_m[s["id"]])
        m.update({
            "store_id": s["id"], "store": s["name"],
            "share": _pct(m["revenue"], total_rev),
            "stock_value": stock_info[s["id"]]["value"],
            "low_stock_items": stock_info[s["id"]]["low"],
            "stock_items": stock_info[s["id"]]["items"],
        })
        cards.append(m)
    ranking = sorted(cards, key=lambda c: -c["revenue"])
    for i, c in enumerate(ranking, 1):
        c["rank"] = i

    total = {
        "revenue": total_rev,
        "profit": sum(m["profit"] for m in cur_m.values()),
        "qty": sum(m["qty"] for m in cur_m.values()),
        "orders": sum(m["orders"] for m in cur_m.values()),
        "prev_revenue": sum(m["revenue"] for m in prev_m.values()),
    }
    total["revenue_change"] = _change_pct(total["revenue"], total["prev_revenue"])
    total["margin"] = _pct(total["profit"], total["revenue"])

    th = load_thresholds()
    return {
        "period": _period_json(p),
        "stores": cards,
        "total": total,
        "trend": {
            "labels": labels,
            "series": [{"store_id": s["id"], "store": s["name"],
                        "revenue": [series.get(s["id"], {}).get(d, {}).get("revenue", 0) for d in labels]}
                       for s in stores],
        },
        "anomalies": compute_anomalies(stores, th),
        "imbalance": compute_imbalance(stores, s30 - timedelta(days=29), s30),
        "thresholds": th,
    }


# ===========================================================================
# 매장 상세 탭
# ===========================================================================
def section_summary(store, p, args):
    sid = store["id"]
    today = _now_kst().date()
    cur_m = metrics_by_store(p["start"], p["end"], [sid])[sid]
    prev_m = metrics_by_store(p["prev_start"], p["prev_end"], [sid])[sid]

    # 이번 주 vs 지난주 (월요일~오늘 / 지난주 같은 요일까지)
    wk0 = today - timedelta(days=today.weekday())
    span = (today - wk0).days
    lw0 = wk0 - timedelta(days=7)
    # 이번 달 vs 지난달 (1일~오늘 / 지난달 1일~같은 일자, 지난달이 더 짧으면 말일까지)
    mo0 = today.replace(day=1)
    last_month_end = mo0 - timedelta(days=1)
    lm0 = last_month_end.replace(day=1)
    lm_to = lm0 + timedelta(days=min(today.day, last_month_end.day) - 1)

    def pair(a0, a1, b0, b1, label_a, label_b):
        a = metrics_by_store(a0, a1, [sid])[sid]
        b = metrics_by_store(b0, b1, [sid])[sid]
        return {"label_a": label_a, "label_b": label_b, "a": a, "b": b,
                "revenue_change": _change_pct(a["revenue"], b["revenue"]),
                "profit_change": _change_pct(a["profit"], b["profit"])}

    return {
        "period": _period_json(p),
        "metrics": _lead_metrics(cur_m, prev_m),
        "week": pair(wk0, today, lw0, lw0 + timedelta(days=span), "이번 주", "지난주 같은 요일까지"),
        "month": pair(mo0, today, lm0, lm_to, "이번 달", "지난달 같은 일자까지"),
    }


def section_sales(store, p, args):
    sid = store["id"]
    gran = args.get("granularity", "day")
    if gran not in ("day", "week", "month"):
        gran = "day"
    frm, params = _sales_where(p["start"], p["end"], [sid])
    trend = _q(
        f"SELECT date_trunc('{gran}', t.date_time)::date AS d, {REV_SQL} AS revenue, "
        f"{PROFIT_SQL} AS profit, {QTY_SQL} AS qty{frm} GROUP BY 1 ORDER BY 1", params)
    trend = [{"date": r["d"].isoformat(), "revenue": _n(r["revenue"]), "profit": _n(r["profit"]),
              "qty": _n(r["qty"])} for r in trend]

    heat = _q(
        f"SELECT EXTRACT(DOW FROM t.date_time)::int AS dow, EXTRACT(HOUR FROM t.date_time)::int AS hr, "
        f"{REV_SQL} AS revenue, {QTY_SQL} AS qty{frm} GROUP BY 1, 2", params)
    grid = [[0] * 24 for _ in range(7)]  # 0=일요일
    for r in heat:
        grid[r["dow"]][r["hr"]] = _n(r["revenue"])

    # 요일별 합계 (히트맵 보조)
    dow_rows = _q(
        f"SELECT EXTRACT(DOW FROM t.date_time)::int AS dow, {REV_SQL} AS revenue{frm} GROUP BY 1", params)
    by_dow = [0] * 7
    for r in dow_rows:
        by_dow[r["dow"]] = _n(r["revenue"])

    # 결제수단별: 카드는 상품의 카드 원가(card_cost_price)를 반영한 이익도 같이 본다
    card_cost = (f"(CASE WHEN t.payment_method = '카드' AND COALESCE(p.card_cost_price, 0) > 0 "
                 f"THEN p.card_cost_price::bigint ELSE {COST} END)")
    pay = _q(
        f"SELECT COALESCE(NULLIF(t.payment_method, ''), '미지정') AS method, {REV_SQL} AS revenue, "
        f"{PROFIT_SQL} AS profit, COALESCE(SUM({SIGNED} * ({PRICE} - {card_cost})), 0) AS profit_card_adj, "
        f"{QTY_SQL} AS qty{frm} GROUP BY 1 ORDER BY revenue DESC", params)
    total_rev = sum(_n(r["revenue"]) for r in pay)
    payments = [{"method": r["method"], "revenue": _n(r["revenue"]), "profit": _n(r["profit"]),
                 "profit_card_adj": _n(r["profit_card_adj"]),
                 "card_cost_effect": _n(r["profit_card_adj"]) - _n(r["profit"]),
                 "share": _pct(_n(r["revenue"]), total_rev), "qty": _n(r["qty"])} for r in pay]

    pre = _q(f"SELECT COALESCE(SUM(CASE WHEN t.type = '선결예약' THEN {SIGNED} * {PRICE} ELSE 0 END), 0) AS pre_rev,"
             f" {REV_SQL} AS total{frm}", params)[0]
    return {
        "period": _period_json(p), "granularity": gran, "trend": trend,
        "heatmap": {"grid": grid, "by_dow": by_dow, "dow_labels": ["일", "월", "화", "수", "목", "금", "토"]},
        "payments": payments,
        "prepaid": {"revenue": _n(pre["pre_rev"]), "share": _pct(_n(pre["pre_rev"]), _n(pre["total"]))},
    }


def _flavor(name, brand):
    """제품명에서 브랜드/'액상'을 빼 '맛' 이름만 남긴다 (일일 보고와 같은 규칙)"""
    text = name or ""
    if brand:
        text = text.replace(brand, "")
    text = text.replace("액상", " ")
    text = " ".join(text.split()).strip(" -")
    return text or (name or "")


def section_bestsellers(store, p, args):
    sid = store["id"]
    metric = args.get("metric", "qty")
    if metric not in ("qty", "revenue", "profit"):
        metric = "qty"
    top_n = _int_arg(args, "top", 10, 1, 100)
    frm, params = _sales_where(p["start"], p["end"], [sid])
    rows = _q(
        f"SELECT p.id AS product_id, p.name, p.option_name, b.name AS brand, c.name AS category, "
        f"{QTY_SQL} AS qty, {REV_SQL} AS revenue, {PROFIT_SQL} AS profit "
        f"{frm.replace('WHERE', 'LEFT JOIN brands b ON b.id = p.brand_id LEFT JOIN categories c ON c.id = p.category_id WHERE', 1)}"
        f" GROUP BY p.id, p.name, p.option_name, b.name, c.name", params)
    products = []
    for r in rows:
        item = {"product_id": r["product_id"], "name": r["name"], "brand": r["brand"] or "-",
                "category": r["category"] or "미분류", "qty": _n(r["qty"]),
                "revenue": _n(r["revenue"]), "profit": _n(r["profit"])}
        item["margin"] = _pct(item["profit"], item["revenue"])
        item["flavor"] = _flavor(r["name"], r["brand"])
        if item["qty"] > 0 or item["revenue"] > 0:
            products.append(item)
    products.sort(key=lambda x: -x[metric])
    for i, it in enumerate(products, 1):
        it["rank"] = i

    def group(key):
        agg = defaultdict(lambda: {"qty": 0, "revenue": 0, "profit": 0, "items": 0})
        for it in products:
            a = agg[it[key]]
            a["qty"] += it["qty"]
            a["revenue"] += it["revenue"]
            a["profit"] += it["profit"]
            a["items"] += 1
        res = [{"name": k, **v, "margin": _pct(v["profit"], v["revenue"])} for k, v in agg.items()]
        res.sort(key=lambda x: -x[metric])
        return res[:top_n]

    flavors = defaultdict(lambda: {"qty": 0, "revenue": 0, "profit": 0})
    for it in products:
        key = f"{it['brand']} · {it['flavor']}" if it["brand"] != "-" else it["flavor"]
        f = flavors[key]
        f["qty"] += it["qty"]
        f["revenue"] += it["revenue"]
        f["profit"] += it["profit"]
    flavor_list = sorted(({"name": k, **v} for k, v in flavors.items()), key=lambda x: -x[metric])[:top_n]

    # 안 팔리는 제품: 재고는 있는데 기간 판매가 없는 제품 (재고 금액 큰 순)
    mat = stock_matrix(p["start"], p["end"], [sid])
    unsold = sorted((r for r in mat if r["sold"] <= 0 and r["qty"] > 0), key=lambda r: -r["value"])[:30]
    return {
        "period": _period_json(p), "metric": metric,
        "products": products[:top_n], "all_count": len(products),
        "brands": group("brand"), "categories": group("category"), "flavors": flavor_list,
        "unsold": [{"product_id": r["product_id"], "name": r["name"], "brand": r["brand"] or "-",
                    "qty": r["qty"], "value": r["value"]} for r in unsold],
    }


def section_turnover(store, p, args):
    sid = store["id"]
    group_by = args.get("group_by", "product")
    if group_by not in ("product", "brand", "category"):
        group_by = "product"
    days = p["days"]
    mat = stock_matrix(p["start"], p["end"], [sid])
    for r in mat:
        derive_stock_fields(r, days)
    sellers = sorted((r["sold"] for r in mat if r["sold"] > 0), reverse=True)
    top_cut = sellers[max(len(sellers) // 5 - 1, 0)] if sellers else None  # 상위 20% 기준 판매수량
    turns = sorted(r["turnover"] for r in mat if r["turnover"] is not None and r["sold"] > 0)
    med_turn = turns[len(turns) // 2] if turns else None

    def tag(sold, turnover, dd):
        if top_cut is None or sold < top_cut or sold <= 0:
            return ""
        if dd is not None and dd <= 7:
            return "잘 팔림 · 재고 빠듯"
        if turnover is not None and med_turn is not None and turnover < med_turn:
            return "잘 팔림 · 회전 느림"
        return "잘 팔림 · 회전 빠름"

    if group_by == "product":
        rows = [{"label": r["name"], "brand": r["brand"] or "-", "qty": r["qty"], "sold": r["sold"],
                 "avg_daily": r["avg_daily"], "turnover": r["turnover"],
                 "days_to_deplete": r["days_to_deplete"], "stock_value": r["value"],
                 "tag": tag(r["sold"], r["turnover"], r["days_to_deplete"])}
                for r in mat if r["sold"] > 0 or r["qty"] > 0]
    else:
        key = "brand" if group_by == "brand" else "category"
        agg = defaultdict(lambda: {"qty": 0, "sold": 0, "value": 0})
        for r in mat:
            a = agg[r[key] or ("-" if key == "brand" else "미분류")]
            a["qty"] += max(r["qty"], 0)
            a["sold"] += r["sold"]
            a["value"] += r["value"]
        rows = []
        for k, a in agg.items():
            avg = a["sold"] / days
            rows.append({"label": k, "brand": "", "qty": a["qty"], "sold": a["sold"],
                         "avg_daily": round(avg, 2),
                         "turnover": round(a["sold"] / a["qty"], 2) if a["qty"] > 0 else None,
                         "days_to_deplete": round(a["qty"] / avg, 1) if avg > 0 else None,
                         "stock_value": a["value"], "tag": ""})
    sort_by = args.get("sort_by", "sold")
    if sort_by not in ("sold", "turnover", "days_to_deplete"):
        sort_by = "sold"
    if sort_by == "days_to_deplete":
        rows.sort(key=lambda r: (r["days_to_deplete"] is None, r["days_to_deplete"] or 0))
    else:
        rows.sort(key=lambda r: (r[sort_by] is None, -(r[sort_by] or 0)))
    limit = _int_arg(args, "limit", 200, 1, 2000)
    return {"period": _period_json(p), "group_by": group_by, "rows": rows[:limit],
            "total_rows": len(rows), "median_turnover": med_turn}


def section_stock(store, p, args):
    sid = store["id"]
    end = _now_kst().date()
    start = end - timedelta(days=29)
    mat = stock_matrix(start, end, [sid])
    for r in mat:
        derive_stock_fields(r, 30)
    counts = defaultdict(int)
    for r in mat:
        counts[r["status"]] += 1
    summary = {
        "items": len(mat),
        "qty": sum(max(r["qty"], 0) for r in mat),
        "value": sum(r["value"] for r in mat),
        "retail_value": sum(max(r["qty"], 0) * r["price"] for r in mat),
        "counts": {k: counts.get(k, 0) for k in ("품절", "부족", "정상", "과다", "부진")},
        "stale_value": sum(r["value"] for r in mat if r["status"] in ("과다", "부진")),
    }
    status = args.get("status", "")
    rows = [r for r in mat if not status or r["status"] == status]
    order = {"품절": 0, "부족": 1, "과다": 2, "부진": 3, "정상": 4}
    sort_by = args.get("sort_by", "status")
    if sort_by == "value":
        rows.sort(key=lambda r: -r["value"])
    elif sort_by == "qty":
        rows.sort(key=lambda r: r["qty"])
    else:
        rows.sort(key=lambda r: (order[r["status"]], r["days_to_deplete"] if r["days_to_deplete"] is not None else 9999))
    limit = _int_arg(args, "limit", 300, 1, 5000)
    out = [{"product_id": r["product_id"], "name": r["name"], "brand": r["brand"] or "-",
            "category": r["category"] or "미분류", "qty": r["qty"], "min_qty": r["min_qty"],
            "cost": r["cost"], "value": r["value"], "sold_30d": r["sold"],
            "days_to_deplete": r["days_to_deplete"], "status": r["status"]} for r in rows[:limit]]
    return {"summary": summary, "rows": out, "total_rows": len(rows)}


def section_needs(store, p, args):
    sid = store["id"]
    target_days = _int_arg(args, "target_days", 14, 1, 365)
    end = _now_kst().date()
    start = end - timedelta(days=29)
    stores = _all_stores()
    names = {s["id"]: s["name"] for s in stores}
    mat_all = stock_matrix(start, end, [s["id"] for s in stores])
    for r in mat_all:
        derive_stock_fields(r, 30)
    mine = [r for r in mat_all if r["store_id"] == sid]
    others = defaultdict(list)
    for r in mat_all:
        if r["store_id"] != sid:
            others[r["product_id"]].append(r)

    sellers = sorted((r["sold"] for r in mine if r["sold"] > 0), reverse=True)
    top_cut = sellers[max(len(sellers) // 5 - 1, 0)] if sellers else None

    reorder = []
    for r in mine:
        short = r["sold"] > 0 and (
            r["qty"] <= 0 or (r["days_to_deplete"] is not None and r["days_to_deplete"] <= target_days)
            or (r["min_qty"] > 0 and r["qty"] <= r["min_qty"]))
        if not short:
            continue
        want = int(math.ceil(r["avg_daily"] * target_days * 1.1))
        need = max(want - max(r["qty"], 0), 0)
        best = top_cut is not None and r["sold"] >= top_cut
        # 발주 우선순위: 베스트셀러 x 소진 임박 (점수가 클수록 급함)
        dd = r["days_to_deplete"] if r["days_to_deplete"] is not None else 0
        priority = (2 if best else 1) * (1.0 / (dd + 1.0)) * 100 + r["sold"] * 0.01
        reorder.append({
            "product_id": r["product_id"], "name": r["name"], "brand": r["brand"] or "-",
            "qty": r["qty"], "min_qty": r["min_qty"], "sold_30d": r["sold"],
            "avg_daily": r["avg_daily"], "days_to_deplete": r["days_to_deplete"],
            "recommend_qty": need, "recommend_cost": need * r["cost"],
            "bestseller": bool(best), "priority": round(priority, 2),
            "flag": "잘 팔리는데 재고 빠듯" if best and dd <= 7 else "",
        })
    reorder.sort(key=lambda x: -x["priority"])

    # 타 매장 이동 추천: 내 매장이 부족한 제품을 다른 매장 잉여분으로 채운다
    transfers = []
    for item in reorder:
        if item["recommend_qty"] <= 0:
            continue
        donors = []
        for d in others.get(item["product_id"], []):
            keep = max(d["min_qty"], int(math.ceil(d["avg_daily"] * 21)))
            surplus = d["qty"] - keep
            if surplus >= 3:
                donors.append((surplus, d))
        donors.sort(key=lambda x: -x[0])
        if donors:
            surplus, d = donors[0]
            transfers.append({"product_id": item["product_id"], "name": item["name"],
                              "from_store_id": d["store_id"], "from_store": names[d["store_id"]],
                              "from_qty": d["qty"], "surplus": surplus,
                              "to_store_id": sid, "suggest_qty": int(min(surplus, item["recommend_qty"]))})

    # 부진 재고 처리 제안: 30일간 판매가 없는 재고 (재고 금액 큰 순)
    dead = []
    for r in sorted((r for r in mine if r["sold"] <= 0 and r["qty"] > 0), key=lambda r: -r["value"])[:30]:
        sells_elsewhere = [d for d in others.get(r["product_id"], []) if d["sold"] > 0]
        if sells_elsewhere:
            best_d = max(sells_elsewhere, key=lambda d: d["sold"])
            action = f"{names[best_d['store_id']]}에서 판매 중 → 이동 검토"
        else:
            action = "전 매장 무판매 → 할인/반품/묶음 판매 검토"
        dead.append({"product_id": r["product_id"], "name": r["name"], "brand": r["brand"] or "-",
                     "qty": r["qty"], "value": r["value"], "action": action})
    return {"target_days": target_days, "reorder": reorder, "transfers": transfers, "dead_stock": dead}


def _hours(check_in, check_out):
    try:
        h1, m1 = [int(x) for x in str(check_in).split(":")[:2]]
        h2, m2 = [int(x) for x in str(check_out).split(":")[:2]]
    except (ValueError, TypeError):
        return None
    mins = (h2 * 60 + m2) - (h1 * 60 + m1)
    if mins < 0:
        mins += 24 * 60  # 자정을 넘긴 근무
    return round(mins / 60.0, 1)


_FIELD_LABELS = {"type": "유형", "store_id": "매장", "quantity": "수량", "unit_cost": "원가", "unit_price": "판매가",
                 "payment_method": "결제수단", "staff": "담당자", "memo": "메모", "date_time": "일시"}


def _field_labels(csv):
    return ", ".join(_FIELD_LABELS.get(x, x) for x in (csv or "").split(",") if x)


def section_ops(store, p, args):
    sid = store["id"]
    frm, params = _sales_where(p["start"], p["end"], [sid])
    staff_rows = _q(
        f"SELECT COALESCE(NULLIF(t.staff, ''), '미지정') AS staff, {QTY_SQL} AS qty, {REV_SQL} AS revenue, "
        f"{PROFIT_SQL} AS profit, "
        f"COUNT(DISTINCT CASE WHEN t.type <> '판매취소' THEN t.type || date_trunc('minute', t.date_time)::text END) AS orders, "
        f"COUNT(*) FILTER (WHERE t.type = '판매취소') AS cancels{frm} GROUP BY 1 ORDER BY revenue DESC", params)
    staff = [{"staff": r["staff"], "qty": _n(r["qty"]), "revenue": _n(r["revenue"]), "profit": _n(r["profit"]),
              "orders": int(r["orders"] or 0), "cancels": int(r["cancels"] or 0),
              "cancel_rate": _pct(int(r["cancels"] or 0), int(r["orders"] or 0) + int(r["cancels"] or 0))}
             for r in staff_rows]

    att = _q("SELECT staff_name, date, check_in, check_out FROM attendance "
             "WHERE store_id = %s AND date >= %s AND date <= %s ORDER BY date",
             [sid, p["start"].isoformat(), p["end"].isoformat()])
    att_by = defaultdict(lambda: {"days": 0, "hours": 0.0, "open": 0})
    for r in att:
        a = att_by[r["staff_name"]]
        a["days"] += 1
        h = _hours(r["check_in"], r["check_out"]) if r["check_out"] else None
        if h is None:
            a["open"] += 1  # 퇴근 기록 없음
        else:
            a["hours"] += h
    attendance = [{"staff": k, "days": v["days"], "hours": round(v["hours"], 1),
                   "avg_hours": round(v["hours"] / max(v["days"] - v["open"], 1), 1) if v["days"] > v["open"] else None,
                   "missing_checkout": v["open"]} for k, v in att_by.items()]
    attendance.sort(key=lambda x: -x["days"])

    d0, d1 = _dt_range(p["start"], p["end"])
    cancels = _q(
        "SELECT t.date_time, pr.name AS product, t.quantity, COALESCE(NULLIF(t.staff,''),'미지정') AS staff, "
        "t.unit_price, t.memo FROM stock_transactions t JOIN products pr ON pr.id = t.product_id "
        "WHERE t.store_id = %s AND t.type = '판매취소' AND t.date_time >= %s AND t.date_time < %s "
        "ORDER BY t.date_time DESC LIMIT 100", [sid, d0, d1])
    cancels = [{"date_time": r["date_time"].strftime("%Y-%m-%d %H:%M"), "product": r["product"],
                "quantity": r["quantity"], "staff": r["staff"],
                "amount": int(r["quantity"] or 0) * int(r["unit_price"] or 0), "memo": r["memo"] or ""}
               for r in cancels]

    stocktake = _q(
        "SELECT t.date_time, pr.name AS product, t.before_qty, t.after_qty, "
        "COALESCE(NULLIF(t.staff,''),'미지정') AS staff, COALESCE(spc.cost_price, pr.cost_price, 0) AS cost "
        "FROM stock_transactions t JOIN products pr ON pr.id = t.product_id "
        "LEFT JOIN store_product_cost spc ON spc.store_id = t.store_id AND spc.product_id = t.product_id "
        "WHERE t.store_id = %s AND t.type = '실사조정' AND t.date_time >= %s AND t.date_time < %s "
        "ORDER BY t.date_time DESC LIMIT 200", [sid, d0, d1])
    st_rows = []
    for r in stocktake:
        diff = int(r["after_qty"] or 0) - int(r["before_qty"] or 0)
        st_rows.append({"date_time": r["date_time"].strftime("%Y-%m-%d %H:%M"), "product": r["product"],
                        "before": r["before_qty"], "after": r["after_qty"], "diff": diff,
                        "diff_value": diff * int(r["cost"] or 0), "staff": r["staff"]})
    # 거래 수정 이력 (tx_edit_log: 이 기능 배포 이후의 수정부터 기록됨)
    edits = {"available": True, "count": 0, "rows": []}
    try:
        e_rows = _q(
            "SELECT l.edited_at, pr.name AS product, l.edited_by, l.changed_fields, l.transaction_id "
            "FROM tx_edit_log l LEFT JOIN products pr ON pr.id = l.product_id "
            "WHERE l.store_id = %s AND l.edited_at >= %s AND l.edited_at < %s "
            "ORDER BY l.edited_at DESC LIMIT 100", [sid, d0, d1])
        e_cnt = _q("SELECT COUNT(*) AS c FROM tx_edit_log WHERE store_id = %s AND edited_at >= %s AND edited_at < %s",
                   [sid, d0, d1])
        edits["count"] = int(e_cnt[0]["c"]) if e_cnt else 0
        edits["rows"] = [{"edited_at": r["edited_at"].strftime("%Y-%m-%d %H:%M"), "product": r["product"] or "-",
                          "by": r["edited_by"] or "-", "fields": _field_labels(r["changed_fields"]),
                          "transaction_id": r["transaction_id"]} for r in e_rows]
    except Exception:
        _rollback()
        edits = {"available": False, "count": 0, "rows": []}
    return {
        "period": _period_json(p), "staff": staff, "attendance": attendance, "cancels": cancels, "edits": edits,
        "stocktake": {"rows": st_rows,
                      "count": len(st_rows),
                      "abs_units": sum(abs(r["diff"]) for r in st_rows),
                      "net_value": sum(r["diff_value"] for r in st_rows)},
        "notes": ["거래 수정 내역은 수정 이력 기능이 적용된 이후부터 집계됩니다. 그 이전 수정은 DB에 남아 있지 않습니다."],
    }


def section_customers(store, p, args):
    sid = store["id"]
    d0, d1 = _dt_range(p["start"], p["end"])
    # 선결제 주문(취소 제외)을 고객 단위로 본다. 현장 판매는 고객 정보가 없어 포함되지 않는다.
    orders = _q(
        "SELECT po.id, po.customer_id, po.total_amount, po.created_at FROM pre_orders po "
        "WHERE po.store_id = %s AND COALESCE(po.status, '') <> '취소됨' AND po.created_at < %s", [sid, d1])
    first_at = {}
    in_period = defaultdict(list)
    anon = 0
    for o in orders:
        cid = o["customer_id"]
        if cid is None:
            if o["created_at"] >= d0:
                anon += 1
            continue
        if cid not in first_at or o["created_at"] < first_at[cid]:
            first_at[cid] = o["created_at"]
        if o["created_at"] >= d0:
            in_period[cid].append(o)
    cust_ids = list(in_period)
    info = {}
    if cust_ids:
        for r in _q("SELECT id, name, phone, COALESCE(is_vip, 0) AS is_vip FROM customers WHERE id = ANY(%s)", [cust_ids]):
            info[r["id"]] = r
    new_ids = [c for c in cust_ids if first_at[c] >= d0]
    returning = [c for c in cust_ids if first_at[c] < d0]
    repeat = [c for c in cust_ids if first_at[c] < d0 or len(in_period[c]) >= 2]
    vip_ids = [c for c in cust_ids if info.get(c, {}).get("is_vip")]
    spend = {c: sum(int(o["total_amount"] or 0) for o in in_period[c]) for c in cust_ids}

    def mask(phone):
        phone = phone or ""
        return phone[:3] + "-****-" + phone[-4:] if len(phone) >= 8 else phone

    top = sorted(cust_ids, key=lambda c: -spend[c])[:20]
    top_rows = [{"name": info.get(c, {}).get("name") or "(이름 없음)", "phone": mask(info.get(c, {}).get("phone")),
                 "orders": len(in_period[c]), "spend": spend[c],
                 "vip": bool(info.get(c, {}).get("is_vip")),
                 "type": "신규" if c in new_ids else "단골"} for c in top]
    vip_total = _q("SELECT COUNT(*) AS c FROM customers WHERE COALESCE(is_vip, 0) = 1")[0]["c"]
    total = len(cust_ids)
    return {
        "period": _period_json(p),
        "summary": {"customers": total, "new": len(new_ids), "returning": len(returning),
                    "new_share": _pct(len(new_ids), total), "returning_share": _pct(len(returning), total),
                    "repeat_rate": _pct(len(repeat), total), "anonymous_orders": anon,
                    "vip_customers": len(vip_ids), "vip_spend": sum(spend[c] for c in vip_ids),
                    "vip_total_all_stores": int(vip_total)},
        "top_customers": top_rows,
        "notes": ["고객 지표는 선결제(예약) 주문 기준입니다. 현장 판매는 고객 정보가 없어 포함되지 않습니다."],
    }


SECTIONS = {
    "summary": section_summary, "sales": section_sales, "bestsellers": section_bestsellers,
    "turnover": section_turnover, "stock": section_stock, "needs": section_needs,
    "ops": section_ops, "customers": section_customers,
}

# CSV 내보내기: 섹션별 (결과 안의 목록 키, [(필드, 머리글)...])
EXPORTS = {
    "sales": ("trend", [("date", "날짜"), ("revenue", "매출"), ("profit", "이익"), ("qty", "판매수량")]),
    "bestsellers": ("products", [("rank", "순위"), ("name", "제품"), ("brand", "브랜드"), ("category", "카테고리"),
                                 ("qty", "판매수량"), ("revenue", "매출"), ("profit", "이익"), ("margin", "이익률(%)")]),
    "turnover": ("rows", [("label", "항목"), ("brand", "브랜드"), ("qty", "현재고"), ("sold", "기간 판매수량"),
                          ("avg_daily", "일평균"), ("turnover", "회전 횟수"), ("days_to_deplete", "소진 예상일"),
                          ("stock_value", "재고 금액"), ("tag", "비고")]),
    "stock": ("rows", [("name", "제품"), ("brand", "브랜드"), ("category", "카테고리"), ("qty", "재고"),
                       ("min_qty", "최소수량"), ("cost", "원가"), ("value", "재고 금액"),
                       ("sold_30d", "30일 판매"), ("days_to_deplete", "소진 예상일"), ("status", "상태")]),
    "needs": ("reorder", [("name", "제품"), ("brand", "브랜드"), ("qty", "재고"), ("sold_30d", "30일 판매"),
                          ("days_to_deplete", "소진 예상일"), ("recommend_qty", "추천 발주"),
                          ("recommend_cost", "추천 발주 금액"), ("flag", "비고")]),
    "ops": ("staff", [("staff", "담당자"), ("orders", "결제 건수"), ("qty", "판매수량"), ("revenue", "매출"),
                      ("profit", "이익"), ("cancels", "취소 건수"), ("cancel_rate", "취소율(%)")]),
    "customers": ("top_customers", [("name", "고객"), ("phone", "연락처"), ("type", "구분"),
                                    ("orders", "주문 수"), ("spend", "금액"), ("vip", "VIP")]),
}
OVERVIEW_EXPORT = [("store", "매장"), ("revenue", "매출"), ("profit", "이익"), ("margin", "이익률(%)"),
                   ("qty", "판매수량"), ("orders", "결제 건수"), ("aov", "객단가"), ("revenue_change", "매출 증감(%)"),
                   ("stock_value", "재고 금액"), ("low_stock_items", "재고 부족 품목"), ("share", "매출 비중(%)")]


def _csv_response(filename, columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in columns])
    data = buf.getvalue().encode("utf-8-sig")  # 엑셀에서 한글이 깨지지 않도록 BOM
    return Response(data, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Cache-Control": "no-store"})


# ===========================================================================
# 데이터 일관성 점검: 리포트 기준 vs 기존 매출 통계/금일 보고 기준
# ===========================================================================
def build_consistency(day):
    """같은 날 같은 매장의 숫자를 두 기준으로 나란히 보여준다.
    legacy = 기존 매출 통계(daily_sales_rollup)가 쓰는 식(제외 규칙 없음), report = 이 리포트(분석 기준)."""
    stores = _all_stores()
    ids = [s["id"] for s in stores]
    rep = metrics_by_store(day, day, ids)
    d0, d1 = _dt_range(day, day)
    legacy_rows = _q(
        "SELECT t.store_id, "
        "COALESCE(SUM(CASE WHEN t.type IN ('판매출고','선결예약') THEN t.quantity WHEN t.type='판매취소' THEN -t.quantity ELSE 0 END),0) AS qty, "
        "COALESCE(SUM(CASE WHEN t.type IN ('판매출고','선결예약') THEN COALESCE(t.quantity,0)*COALESCE(t.unit_price,0) "
        "WHEN t.type='판매취소' THEN -COALESCE(t.quantity,0)*COALESCE(t.unit_price,0) ELSE 0 END),0) AS revenue "
        "FROM stock_transactions t WHERE t.type = ANY(%s) AND t.date_time >= %s AND t.date_time < %s GROUP BY 1",
        [list(SALE_TYPES), d0, d1])
    legacy = {r["store_id"]: r for r in legacy_rows}
    rollup = {r["store_id"]: r for r in _q(
        "SELECT store_id, qty, revenue FROM daily_sales_rollup WHERE sale_date = %s", [day])}
    overrides = {r["store_id"]: r for r in _q(
        "SELECT store_id, override_amount, original_amount FROM daily_revenue_override WHERE target_date = %s",
        [day.isoformat()])}
    # 제외된 거래의 매출 (사유별, 한 거래가 여러 사유에 걸리면 중복 집계)
    reasons = {
        "이동/교환 메모 거래": "(t.memo LIKE %s OR t.memo LIKE %s)",
        "옵션 부모 제품": "EXISTS (SELECT 1 FROM products v WHERE v.parent_product_id = p.id AND v.is_active = 1)",
        "퀵상품": "COALESCE(p.unlimited_stock, 0) <> 0",
    }
    excluded = defaultdict(dict)
    for label, cond in reasons.items():
        params = [list(SALE_TYPES), d0, d1] + (MEMO_PARAMS if "%s" in cond else [])
        sql = (f"SELECT t.store_id, {REV_SQL} AS revenue FROM stock_transactions t "
               f"JOIN products p ON p.id = t.product_id WHERE t.type = ANY(%s) AND t.date_time >= %s "
               f"AND t.date_time < %s AND {cond} GROUP BY 1")
        for r in _q(sql, params):
            excluded[r["store_id"]][label] = _n(r["revenue"])
    out = []
    for s in stores:
        sid = s["id"]
        lg = legacy.get(sid, {"qty": 0, "revenue": 0})
        item = {
            "store_id": sid, "store": s["name"],
            "report_revenue": rep[sid]["revenue"], "report_qty": rep[sid]["qty"],
            "legacy_revenue": _n(lg["revenue"]), "legacy_qty": _n(lg["qty"]),
            "rollup_revenue": _n(rollup[sid]["revenue"]) if sid in rollup else None,
            "override_amount": _n(overrides[sid]["override_amount"]) if sid in overrides else None,
            "excluded": excluded.get(sid, {}),
        }
        item["diff_revenue"] = item["report_revenue"] - item["legacy_revenue"]
        item["diff_qty"] = item["report_qty"] - item["legacy_qty"]
        item["explained"] = (item["diff_revenue"] == 0 and item["diff_qty"] == 0) or bool(item["excluded"])
        out.append(item)
    return {"date": day.isoformat(), "stores": out,
            "apply_override": APPLY_REVENUE_OVERRIDE,
            "note": "리포트는 분석 화면과 같은 제외 규칙(선결출고, 이동/교환 메모, 옵션 부모, 퀵상품)을 적용하고, "
                    "기존 매출 통계/금일 보고는 이 규칙을 쓰지 않아 차이가 날 수 있습니다. 보정 매출은 지표에 반영하지 않습니다."}


# ===========================================================================
# 라우트
# ===========================================================================
def _json_error(e, status=400):
    return jsonify({"error": str(e)}), status


def _guard(fn):
    """검증 오류는 400, DB 오류는 트랜잭션을 되돌리고 500(JSON)"""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except ReportError as e:
            return _json_error(e, 400)
        except Exception as e:  # noqa: BLE001
            _rollback()
            print(f"❌ 매장 리포트 오류: {e}")
            return jsonify({"error": "리포트를 불러오는 중 오류가 발생했습니다."}), 500
    wrapper.__name__ = fn.__name__
    return wrapper


@bp.route("/admin/reports")
@auth.admin_required
def page_overview():
    return render_template("admin_reports.html", active="admin_reports")


@bp.route("/admin/reports/<int:store_id>")
@auth.admin_required
def page_store(store_id):
    _get_db()
    row = _q("SELECT id, name FROM stores WHERE id = %s", [store_id])
    if not row:
        return render_template("admin_reports.html", active="admin_reports"), 404
    return render_template("admin_report_store.html", active="admin_reports",
                           store=row[0], stores=_all_stores())


@bp.route("/api/admin/reports/overview")
@auth.admin_required
@_guard
def api_overview():
    p = parse_period(request.args)
    key = ("overview", p["start"], p["end"])
    data = _cached(key, lambda: build_overview(p))
    if request.args.get("format") == "csv":
        return _csv_response("store_report_overview.csv", OVERVIEW_EXPORT, data["stores"])
    return jsonify(data)


@bp.route("/api/admin/reports/store/<int:store_id>/<section>")
@auth.admin_required
@_guard
def api_store_section(store_id, section):
    fn = SECTIONS.get(section)
    if fn is None:
        return _json_error("알 수 없는 탭입니다.", 404)
    store = _get_store(store_id)
    p = parse_period(request.args)
    arg_key = tuple(sorted((k, v) for k, v in request.args.items() if k != "format"))
    data = _cached(("section", store_id, section, arg_key), lambda: fn(store, p, request.args))
    if request.args.get("format") == "csv":
        spec = EXPORTS.get(section)
        if not spec:
            return _json_error("이 탭은 내보내기를 지원하지 않습니다.", 400)
        list_key, columns = spec
        return _csv_response(f"store_{store_id}_{section}.csv", columns, data.get(list_key, []))
    data = dict(data)
    data["store"] = store
    return jsonify(data)


@bp.route("/api/admin/reports/thresholds", methods=["GET", "POST"])
@auth.admin_required
@_guard
def api_thresholds():
    if request.method == "GET":
        return jsonify(load_thresholds())
    body = request.get_json(silent=True) or {}
    cur = _cursor()
    for key, (lo, hi) in THRESHOLD_LIMITS.items():
        if key not in body:
            continue
        try:
            value = int(float(body[key]))
        except (TypeError, ValueError):
            raise ReportError("기준값은 숫자여야 합니다.")
        if not lo <= value <= hi:
            raise ReportError(f"기준값 범위는 {lo}~{hi} 입니다.")
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value", [key, str(value)])
    g.db.commit()
    _CACHE.clear()
    return jsonify(load_thresholds())


_SHARE_SALT = "admin-report-share"
_SHARE_MAX_AGE = 7 * 24 * 3600


def _share_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.secret_key, salt=_SHARE_SALT)


def _valid_share_path(path):
    # 리포트 화면 경로만 허용한다 (다른 주소로 보내는 열린 리다이렉트 방지)
    return (isinstance(path, str) and len(path) <= 500 and (path == "/admin/reports" or path.startswith(("/admin/reports/", "/admin/reports?")))
            and not path.startswith("//") and "\\" not in path and "\n" not in path and "\r" not in path)


@bp.route("/api/admin/reports/share", methods=["POST"])
@auth.admin_required
@_guard
def api_share_create():
    body = request.get_json(silent=True) or {}
    path = body.get("path") or ""
    if not _valid_share_path(path):
        return _json_error(ValueError("공유할 수 없는 경로입니다."))
    token = _share_serializer().dumps({"p": path})
    return jsonify({"url": request.host_url.rstrip("/") + "/admin/reports/s/" + token,
                    "expires_days": _SHARE_MAX_AGE // 86400})


@bp.route("/admin/reports/s/<token>")
@auth.admin_required
def page_share_open(token):
    """공유 링크: 로그인한 어드민만 열 수 있다. 토큰은 화면 위치(매장/탭/기간)만 담고 데이터는 담지 않는다."""
    from flask import redirect
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _share_serializer().loads(token, max_age=_SHARE_MAX_AGE)
    except SignatureExpired:
        return "공유 링크가 만료되었습니다. 새 링크를 만들어 주세요.", 410
    except BadSignature:
        return "올바르지 않은 공유 링크입니다.", 400
    path = data.get("p") if isinstance(data, dict) else None
    if not _valid_share_path(path):
        return "올바르지 않은 공유 링크입니다.", 400
    return redirect(path)


@bp.route("/api/admin/reports/consistency")
@auth.admin_required
@_guard
def api_consistency():
    raw = (request.args.get("date") or "").strip()
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date() if raw else _now_kst().date()
    except ValueError:
        raise ReportError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return jsonify(build_consistency(day))
