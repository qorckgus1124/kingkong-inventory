# -*- coding: utf-8 -*-
"""
사내 재고·판매관리 프로그램 (PostgreSQL + Render 호환) - 전체 버전
"""
import os
import json
import shutil
import io
import csv
import re
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.errors
from flask import Flask, g, jsonify, render_template, request, send_file, send_from_directory

# 서버가 어느 지역(UTC 등)에서 돌아가더라도 항상 한국 시간 기준으로 동작하도록 고정
KST = ZoneInfo("Asia/Seoul")


def now_kst():
    """항상 한국 표준시(KST) 기준의 현재 시각을 반환한다."""
    return datetime.now(KST)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "inventory.db")  # 더 이상 사용하지 않음 (PostgreSQL)
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
IMAGE_DIR = os.path.join(BASE_DIR, "static", "product_images")
BRAND_CSV_PATH = os.path.join(BASE_DIR, "카테고리 상품별 브랜드 정리.csv")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")


def normalize_search(text):
    """검색어/대상 문자열의 공백을 모두 제거해 정규화한다.
    예) "드 알" -> "드알" / "파드 알로에그레이프" -> "파드알로에그레이프"
    이렇게 하면 검색어의 각 글자가 서로 붙어서(연속으로) 등장하는 제품만
    찾게 되어, 단어 경계를 넘나드는 부분 일치(예: "파드" + "알로에")는 잡아내고
    이름 안에서 멀리 떨어진 글자들이 우연히 둘 다 포함된 경우(예: "레드" ...
    "알로에")는 걸러낸다.
    """
    return re.sub(r"\s+", "", text or "")


def upsert_customer(cur, name, phone, address=None):
    """전화번호를 기준으로 고객을 찾아 없으면 새로 등록하고, 있으면 이름/주소가
    비어있던 경우에 한해 채워 넣는다. 선결제 주문이 들어올 때마다 호출되어
    고객 테이블을 자동으로 최신 상태로 유지한다. 고객 id를 반환한다 (전화번호가
    없으면 None)."""
    phone = (phone or "").strip()
    if not phone:
        return None
    name = (name or "").strip() or None
    address = (address or "").strip() or None
    cur.execute("SELECT id, name, address FROM customers WHERE phone = %s", (phone,))
    existing = cur.fetchone()
    if existing:
        updates = []
        params = []
        if name and not existing["name"]:
            updates.append("name = %s")
            params.append(name)
        if address and not existing["address"]:
            updates.append("address = %s")
            params.append(address)
        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(existing["id"])
            cur.execute(f"UPDATE customers SET {', '.join(updates)} WHERE id = %s", params)
        return existing["id"]
    cur.execute(
        "INSERT INTO customers (name, phone, address) VALUES (%s, %s, %s) RETURNING id",
        (name, phone, address)
    )
    return cur.fetchone()["id"]


# Flask 3.0 기본 JSON 인코더는 datetime을 "Tue, 28 Jul 2026 09:15:00 GMT" 같은
# HTTP 날짜 형식으로 직렬화한다. 프론트엔드에서는 "2026-07-28" 형식을 기대하므로
# ISO 8601 형식("2026-07-28T09:15:00")으로 직렬화하도록 바꾼다.
from flask.json.provider import DefaultJSONProvider
import datetime as _dt

class _ISODateJSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, (_dt.datetime, _dt.date)):
            return o.isoformat()
        return DefaultJSONProvider.default(o)

app.json = _ISODateJSONProvider(app)


# ---------------------------------------------------------------------------
# DB 연결 (PostgreSQL) - 커넥션 풀 사용
# ---------------------------------------------------------------------------
# 매 요청마다 새 TCP/SSL 연결을 맺으면 원격 DB(Aiven) 왕복 지연이 요청마다 추가되어
# 체감 속도가 크게 느려진다. gunicorn 워커(프로세스) 하나당 커넥션 풀을 하나 만들어두고,
# 요청이 올 때마다 풀에서 연결을 빌려 쓰고 반환하는 방식으로 바꾼다.

_DB_POOL = None


def _get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # 로컬 개발용 기본값 (PostgreSQL 설치 필요)
        database_url = "postgres://postgres:1234@localhost:5432/inventory_db"
        print("⚠️ DATABASE_URL 환경 변수가 없어 기본값을 사용합니다.")
    return database_url


def _create_pool():
    result = urlparse(_get_database_url())
    # minconn=1: 워커 시작 시 연결 1개 미리 생성
    # maxconn: 워커 하나당 최대 동시 연결 수 (Neon pooler(-pooler)를 쓰면 실제 Postgres 커넥션이
    # 아니라 PgBouncer 커넥션이라 여유 있게 잡아도 안전하다. DB_POOL_MAXCONN 환경변수로 조절 가능)
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=int(os.environ.get("DB_POOL_MAXCONN", "10")),
        dbname=result.path[1:],
        user=result.username,
        password=result.password,
        host=result.hostname,
        port=result.port,
        sslmode='require',  # Render에서는 'require'로 자동 설정됨
        connect_timeout=10,
    )


def _init_pool():
    global _DB_POOL
    if _DB_POOL is None:
        _DB_POOL = _create_pool()
    return _DB_POOL


def _reset_pool():
    # 풀이 고갈되었거나(pool exhausted) 손상된 상태로 보일 때, 기존 풀을 버리고
    # 완전히 새 풀을 만들어 서비스 재시작 없이도 스스로 복구되도록 한다.
    global _DB_POOL
    old_pool = _DB_POOL
    _DB_POOL = _create_pool()
    if old_pool is not None:
        try:
            old_pool.closeall()
        except Exception:
            pass
    return _DB_POOL


def get_db():
    if "db" not in g:
        pool = _init_pool()
        try:
            conn = pool.getconn()
        except psycopg2.pool.PoolError:
            # 풀이 고갈된 상태(예: DB가 잠깐 잠들었다 깨어나는 순간에 요청이 몰린 경우)라면
            # 서버를 재시작하지 않아도 스스로 새 풀을 만들어 복구를 시도한다.
            pool = _reset_pool()
            conn = pool.getconn()
        try:
            # 풀에 오래 유지된 연결이 원격에서 끊겼을 수 있으므로 가벼운 헬스체크 후,
            # 죽어있으면 버리고 새 연결을 받아온다.
            with conn.cursor() as probe:
                probe.execute("SELECT 1")
        except Exception:
            pool.putconn(conn, close=True)
            conn = pool.getconn()
        # 참고: 새로 만들어진 연결은 원래 autocommit이 기본값 False라서
        # 여기서 다시 설정할 필요가 없다. 오히려 방금 위 헬스체크(SELECT 1)로
        # 트랜잭션이 열린 상태에서 이 값을 다시 대입하면
        # "set_session cannot be used inside a transaction" 에러가 난다.
        # (예전에는 Aiven 연결이 자주 끊겨서 매번 새 연결을 받아왔고,
        #  그 새 연결엔 헬스체크를 안 태워서 우연히 안 터졌을 뿐이다.)
        # 커넥션 풀에서 재사용되는 연결이라도 세션 타임존을 한국시간(KST)으로 맞춰준다.
        # (psycopg2 connection 객체는 임의 속성을 못 붙이는 타입이라 "한 번만 설정" 캐싱은
        #  AttributeError를 일으킨다. SET TIME ZONE 자체는 매우 가벼운 명령이라 매 요청마다
        #  실행해도 성능에 문제가 없다.)
        with conn.cursor() as tz_cur:
            tz_cur.execute("SET TIME ZONE 'Asia/Seoul'")
        conn.commit()
        g.db = conn
        g.cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        g._db_pool_ref = pool
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    cursor = g.pop("cursor", None)
    pool = g.pop("_db_pool_ref", None)
    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            pass
    if db is not None and pool is not None:
        try:
            if exception is not None:
                db.rollback()
        except Exception:
            pass
        # 커넥션을 닫지 않고 풀에 반환해 재사용한다.
        # 오류가 있었던 연결은 close=True로 버려서 다음 요청에 새 연결을 만들도록 한다.
        try:
            pool.putconn(db, close=(exception is not None))
        except Exception:
            # 이 연결이 속했던 풀이 그 사이 재생성(reset)되어 이미 닫혔을 수 있다.
            # 그런 경우 연결을 그냥 직접 닫아서 자원만 정리한다.
            try:
                db.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# DB 초기화 (PostgreSQL 스키마)
# ---------------------------------------------------------------------------

def init_db():
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.makedirs(IMAGE_DIR, exist_ok=True)

        conn = get_db()
        cur = g.cursor

        # 테이블 생성 (PostgreSQL 문법)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                color TEXT DEFAULT '#8a8f98'
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brands (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                color TEXT DEFAULT '#8a8f98',
                status TEXT DEFAULT 'approved',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL,
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                cost_price INTEGER DEFAULT 0,
                card_cost_price INTEGER DEFAULT 0,
                sale_price INTEGER DEFAULT 0,
                image_path TEXT,
                memo TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                address TEXT,
                business_number TEXT,
                manager TEXT,
                phone TEXT,
                schedule TEXT,
                staffs TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_stock (
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                qty INTEGER DEFAULT 0,
                min_qty INTEGER DEFAULT 0,
                PRIMARY KEY (store_id, product_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                contact TEXT,
                phone TEXT,
                link TEXT,
                memo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_transactions (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
                ref_transaction_id INTEGER REFERENCES stock_transactions(id) ON DELETE SET NULL,
                type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_cost INTEGER,
                unit_price INTEGER,
                payment_method TEXT,
                before_qty INTEGER,
                after_qty INTEGER,
                date_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                staff TEXT,
                memo TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL,
                old_value INTEGER,
                new_value INTEGER,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                staff TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                staff_name TEXT NOT NULL,
                date TEXT NOT NULL,
                check_in TEXT,
                check_out TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(store_id, staff_name, date)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pre_orders (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                customer_name TEXT,
                customer_phone TEXT,
                customer_address TEXT,
                request_memo TEXT,
                payment_method TEXT,
                total_amount INTEGER DEFAULT 0,
                status TEXT DEFAULT '대기',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pre_order_items (
                id SERIAL PRIMARY KEY,
                pre_order_id INTEGER NOT NULL REFERENCES pre_orders(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                discount_amount INTEGER DEFAULT 0
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brand_category_mapping (
                id SERIAL PRIMARY KEY,
                brand_name TEXT NOT NULL,
                category_name TEXT NOT NULL,
                UNIQUE(brand_name, category_name)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                id SERIAL PRIMARY KEY,
                from_store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                to_store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL,
                staff TEXT,
                memo TEXT,
                status TEXT DEFAULT '대기',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                cancelled_at TIMESTAMP
            );
        """)
        # stock_movements가 stock_transactions보다 나중에 생성되므로,
        # 이동 거래(이동출고/이동입고) 레코드가 어느 이동 건에 속하는지 연결하는 컬럼을
        # 별도 ALTER로 추가한다 (취소된 이동 건의 거래 내역을 조회에서 제외하기 위함).
        cur.execute("""
            ALTER TABLE stock_transactions
            ADD COLUMN IF NOT EXISTS movement_id INTEGER REFERENCES stock_movements(id) ON DELETE SET NULL;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_revenue_override (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                target_date TEXT NOT NULL,
                override_amount INTEGER NOT NULL,
                original_amount INTEGER NOT NULL,
                staff TEXT,
                memo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(store_id, target_date)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_revenue_history (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                target_date TEXT NOT NULL,
                old_amount INTEGER,
                new_amount INTEGER,
                staff TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # ---------------------------------------------------------------
        # 고객 관리(CRM)
        # ---------------------------------------------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                name TEXT,
                phone TEXT UNIQUE,
                address TEXT,
                memo TEXT,
                is_vip INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 선결제 주문이 어느 고객 소속인지 연결 (전화번호로 매칭)
        cur.execute("""
            ALTER TABLE pre_orders
            ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL;
        """)
        # 기존에 쌓여있던 선결제 주문의 고객명/연락처를 고객 테이블로 승격(백필)
        cur.execute("""
            INSERT INTO customers (name, phone, address)
            SELECT DISTINCT ON (customer_phone) customer_name, customer_phone, customer_address
            FROM pre_orders
            WHERE customer_phone IS NOT NULL AND customer_phone <> ''
            ORDER BY customer_phone, created_at DESC
            ON CONFLICT (phone) DO NOTHING;
        """)
        cur.execute("""
            UPDATE pre_orders po
            SET customer_id = c.id
            FROM customers c
            WHERE po.customer_id IS NULL AND po.customer_phone = c.phone
              AND po.customer_phone IS NOT NULL AND po.customer_phone <> '';
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pre_orders_customer_id ON pre_orders(customer_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);")

        # 기본 매장 (ID=1)
        cur.execute("INSERT INTO stores (id, name) VALUES (1, '강남역점') ON CONFLICT (id) DO NOTHING;")

        # 기본 카테고리
        cur.execute("SELECT COUNT(*) FROM categories")
        if cur.fetchone()['count'] == 0:
            cur.executemany(
                "INSERT INTO categories (name, color) VALUES (%s, %s)",
                [("일회용", "#4f8ff7"), ("기기", "#e07a5f"), ("액상", "#81b29a")]
            )
            cur.execute("INSERT INTO products (id, name) VALUES (1, '샘플 제품') ON CONFLICT (id) DO NOTHING;")

        # 기본 설정
        cur.execute("INSERT INTO settings (key, value) VALUES ('card_fee_rate', '2.5') ON CONFLICT (key) DO NOTHING;")
        cur.execute("INSERT INTO settings (key, value) VALUES ('target_stock_days', '7') ON CONFLICT (key) DO NOTHING;")
        cur.execute("INSERT INTO settings (key, value) VALUES ('monthly_target_revenue', '0') ON CONFLICT (key) DO NOTHING;")

        conn.commit()
        create_indexes()
        load_brand_mapping_from_csv()

        print("✅ 데이터베이스 초기화 완료 (테이블 생성, 기본 데이터 삽입)")

    except Exception as e:
        print(f"❌ DB 초기화 오류: {e}")
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# 인덱스 생성 (PostgreSQL)
# ---------------------------------------------------------------------------

def create_indexes():
    conn = get_db()
    cur = g.cursor
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON stock_transactions(date_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_type ON stock_transactions(type);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_store ON stock_transactions(store_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_product ON stock_transactions(product_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_store_product ON store_stock(store_id, product_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pre_orders_store ON pre_orders(store_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pre_orders_status ON pre_orders(status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_brands_name ON brands(name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_movements_status ON stock_movements(status);")
    conn.commit()


# ---------------------------------------------------------------------------
# 브랜드 매핑 데이터 로드 (CSV)
# ---------------------------------------------------------------------------

def load_brand_mapping_from_csv():
    if not os.path.exists(BRAND_CSV_PATH):
        print(f"⚠️ 브랜드 CSV 파일을 찾을 수 없습니다: {BRAND_CSV_PATH}")
        return

    conn = get_db()
    cur = g.cursor
    try:
        with open(BRAND_CSV_PATH, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            inserted = 0
            for row in reader:
                brand = row.get('상품 브랜드', '').strip()
                category = row.get('카테고리', '').strip()
                if brand and category:
                    cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (category,))
                    cur.execute("INSERT INTO brand_category_mapping (brand_name, category_name) VALUES (%s, %s) ON CONFLICT DO NOTHING;", (brand, category))
                    cur.execute("""
                        INSERT INTO brands (name, category_id, status)
                        SELECT %s, id, 'approved' FROM categories WHERE name = %s
                        ON CONFLICT (name) DO NOTHING;
                    """, (brand, category))
                    inserted += 1
            conn.commit()
            print(f"✅ 브랜드 매핑 데이터 {inserted}개 로드 완료")
    except Exception as e:
        print(f"❌ 브랜드 CSV 로드 오류: {e}")


# ===== Render(gunicorn)에서도 DB 초기화가 실행되도록 =====
with app.app_context():
    init_db()
# ========================================================


# ---------------------------------------------------------------------------
# 헬퍼 함수 (PostgreSQL)
# ---------------------------------------------------------------------------

def get_brand_list_from_db():
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT DISTINCT brand_name FROM brand_category_mapping")
    rows = cur.fetchall()
    brands = [r["brand_name"] for r in rows]
    brands.sort(key=len, reverse=True)
    return brands

def extract_brand_from_name(product_name, brand_list):
    if not product_name:
        return None, product_name
    product_lower = product_name.lower()
    for brand in brand_list:
        brand_lower = brand.lower()
        if brand_lower in product_lower:
            remaining = product_name.replace(brand, "").strip()
            remaining = re.sub(r'\s*(액상|팟)\s*', ' ', remaining).strip()
            remaining = re.sub(r'\s+', ' ', remaining)
            return brand, remaining
    return None, product_name

def get_brand_id_from_name(brand_name):
    conn = get_db()
    cur = g.cursor
    brand_name = brand_name.strip()
    if not brand_name:
        return None
    cur.execute("SELECT id, status FROM brands WHERE name = %s", (brand_name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO brands (name, status) VALUES (%s, 'pending') RETURNING id", (brand_name,))
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id

def get_category_id_from_brand(brand_name):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT category_name FROM brand_category_mapping WHERE brand_name = %s LIMIT 1", (brand_name,))
    row = cur.fetchone()
    if not row:
        return None
    cat_name = row["category_name"]
    cur.execute("SELECT id FROM categories WHERE name = %s", (cat_name,))
    cat_row = cur.fetchone()
    return cat_row["id"] if cat_row else None

def auto_assign_brand_and_category(product_name, user_brand_id=None, user_category_id=None):
    conn = get_db()
    cur = g.cursor
    if user_brand_id:
        cur.execute("SELECT name FROM brands WHERE id = %s", (user_brand_id,))
        brand_row = cur.fetchone()
        brand_name = brand_row["name"] if brand_row else None
    else:
        brand_list = get_brand_list_from_db()
        brand_name, _ = extract_brand_from_name(product_name, brand_list)
    brand_id = None
    if brand_name:
        brand_id = get_brand_id_from_name(brand_name)
    category_id = user_category_id
    if not category_id and brand_name:
        auto_cat_id = get_category_id_from_brand(brand_name)
        if auto_cat_id:
            category_id = auto_cat_id
    return brand_id, category_id

def product_row_to_dict(db, row, store_id=None):
    if row is None:
        return None
    d = dict(row)
    cur = g.cursor
    try:
        if store_id:
            cur.execute("SELECT qty, min_qty FROM store_stock WHERE store_id=%s AND product_id=%s", (store_id, row["id"]))
            stock = cur.fetchone()
            d["qty"] = stock["qty"] if stock else 0
            d["min_qty"] = stock["min_qty"] if stock else 0
        else:
            cur.execute("SELECT COALESCE(SUM(qty),0) as total FROM store_stock WHERE product_id=%s", (row["id"],))
            total = cur.fetchone()
            d["qty"] = total["total"] if total else 0
            cur.execute("SELECT COALESCE(SUM(min_qty),0) as total_min FROM store_stock WHERE product_id=%s", (row["id"],))
            min_sum = cur.fetchone()
            d["min_qty"] = min_sum["total_min"] if min_sum else 0
    except Exception as e:
        print(f"⚠️ 재고 조회 오류 (product_id={row['id']}): {e}")
        d["qty"] = 0
        d["min_qty"] = 0

    sale = row["sale_price"] or 0
    cost = row["cost_price"] or 0
    d["margin_rate"] = round((sale - cost) / sale * 100, 1) if sale > 0 else None

    if d.get("brand_id"):
        cur.execute("SELECT name, color FROM brands WHERE id = %s", (d["brand_id"],))
        brand = cur.fetchone()
        if brand:
            d["brand_name"] = brand["name"]
            d["brand_color"] = brand["color"]
        else:
            d["brand_name"] = None
            d["brand_color"] = None
    else:
        d["brand_name"] = None
        d["brand_color"] = None

    return d

def _apply_stock_delta(db, store_id, product_id, ttype, quantity):
    cur = g.cursor
    cur.execute("SELECT qty FROM store_stock WHERE store_id=%s AND product_id=%s", (store_id, product_id))
    stock = cur.fetchone()
    current_qty = stock["qty"] if stock else 0
    is_decrease = ttype in {"판매출고", "반품", "폐기", "이동출고", "조정", "입고취소"}
    if is_decrease and quantity > current_qty:
        return f"현재 재고({current_qty})보다 많은 수량은 처리할 수 없습니다."
    if stock is None:
        new_qty = -quantity if is_decrease else quantity
        cur.execute("INSERT INTO store_stock (store_id, product_id, qty, min_qty) VALUES (%s, %s, %s, 0)", (store_id, product_id, max(new_qty, 0)))
    else:
        delta = -quantity if is_decrease else quantity
        cur.execute("UPDATE store_stock SET qty = qty + %s WHERE store_id=%s AND product_id=%s", (delta, store_id, product_id))
    return None


# ---------------------------------------------------------------------------
# 라우트 (페이지)
# ---------------------------------------------------------------------------

@app.route("/api/version")
def api_version():
    """지금 Render에 실제로 어떤 코드가 떠 있는지 브라우저에서 바로 확인하기 위한 엔드포인트.
    예) https://<내-render-주소>/api/version 접속했을 때 아래 값이 안 보이면
    (404가 뜨거나 다른 값이 보이면) 새로 배포한 코드가 아직 반영되지 않은 것이다."""
    return jsonify({
        "build": "search-fix-v4-race-2026-07-27",
        "note": "검색어 공백 무시 + 브랜드/제품명 경계 버그 수정 + 타이핑 중 검색 결과 뒤집힘(경쟁 상태) 수정 적용됨",
    })


@app.route("/")
def index():
    return render_template("products.html", active="products")

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active="dashboard", now=now_kst())

@app.route("/categories")
def categories_page():
    return render_template("categories.html", active="categories")

@app.route("/brands")
def brands_page():
    return render_template("brands.html", active="brands")

@app.route("/transactions")
def transactions_page():
    return render_template("transactions.html", active="transactions")

@app.route("/sales")
def sales_page():
    return render_template("sales.html", active="sales")

@app.route("/performance")
def performance_page():
    return render_template("performance.html", active="performance")

@app.route("/statistics")
def statistics_page():
    return render_template("statistics.html", active="statistics")

@app.route("/stores")
def stores_page():
    return render_template("stores.html", active="stores")

@app.route("/suppliers")
def suppliers_page():
    return render_template("suppliers.html", active="suppliers")

@app.route("/settings")
def settings_page():
    return render_template("settings.html", active="settings")

@app.route("/daily_report")
def daily_report_page():
    return render_template("daily_report.html", active="daily_report")

@app.route("/recommend_order")
def recommend_order_page():
    return render_template("recommend_order.html", active="recommend_order")

@app.route("/transfer")
def transfer_page():
    return render_template("transfer.html", active="transfer")

@app.route("/forecast")
def forecast_page():
    return render_template("forecast.html", active="forecast")

@app.route("/customers")
def customers_page():
    return render_template("customers.html", active="customers")

@app.route("/stocktake")
def stocktake_page():
    return render_template("stocktake.html", active="stocktake")

@app.route("/turnover")
def turnover_page():
    return render_template("turnover.html", active="turnover")

@app.route("/static/product_images/<path:filename>")
def product_image(filename):
    return send_from_directory(IMAGE_DIR, filename)


# ---------------------------------------------------------------------------
# API - 카테고리
# ---------------------------------------------------------------------------

@app.route("/api/categories", methods=["GET", "POST"])
def api_categories():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            cur.execute("SELECT * FROM categories ORDER BY id")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            print(f"⚠️ 카테고리 조회 오류: {e}")
            return jsonify([])
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    color = data.get("color") or "#8a8f98"
    if not name:
        return jsonify({"error": "카테고리명을 입력해주세요."}), 400
    try:
        cur.execute("INSERT INTO categories (name, color) VALUES (%s, %s) RETURNING id", (name, color))
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"id": new_id, "name": name, "color": color})
    except psycopg2.IntegrityError:
        return jsonify({"error": "이미 존재하는 카테고리명입니다."}), 400
    except Exception as e:
        print(f"❌ 카테고리 추가 오류: {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

@app.route("/api/categories/<int:cid>", methods=["PUT", "DELETE"])
def api_category_detail(cid):
    conn = get_db()
    cur = g.cursor
    if request.method == "DELETE":
        try:
            cur.execute("UPDATE products SET category_id=NULL WHERE category_id=%s", (cid,))
            cur.execute("DELETE FROM categories WHERE id=%s", (cid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 카테고리 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    color = data.get("color") or "#8a8f98"
    if not name:
        return jsonify({"error": "카테고리명을 입력해주세요."}), 400
    try:
        cur.execute("UPDATE categories SET name=%s, color=%s WHERE id=%s", (name, color, cid))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 카테고리 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 브랜드
# ---------------------------------------------------------------------------

@app.route("/api/brands", methods=["GET", "POST"])
def api_brands():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            status_filter = request.args.get("status")
            search = request.args.get("search", "").strip()
            sql = """
                SELECT b.*, c.name as category_name
                FROM brands b
                LEFT JOIN categories c ON c.id = b.category_id
            """
            params = []
            conditions = []
            if status_filter:
                conditions.append("b.status = %s")
                params.append(status_filter)
            if search:
                conditions.append("REPLACE(b.name, ' ', '') ILIKE %s")
                params.append(f"%{normalize_search(search)}%")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY b.name"
            cur.execute(sql, params)
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                cur.execute("SELECT COUNT(*) as cnt FROM products WHERE brand_id = %s", (r["id"],))
                count = cur.fetchone()
                d["product_count"] = count["cnt"] if count else 0
                result.append(d)
            return jsonify(result)
        except Exception as e:
            print(f"⚠️ 브랜드 조회 오류: {e}")
            return jsonify([])

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    category_id = data.get("category_id")
    color = data.get("color") or "#8a8f98"
    status = data.get("status", "pending")

    if not name:
        return jsonify({"error": "브랜드명을 입력해주세요."}), 400

    try:
        cur.execute(
            "INSERT INTO brands (name, category_id, color, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, category_id, color, status)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"id": new_id, "name": name})
    except psycopg2.IntegrityError:
        return jsonify({"error": "이미 존재하는 브랜드명입니다."}), 400
    except Exception as e:
        print(f"❌ 브랜드 추가 오류: {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

@app.route("/api/brands/<int:bid>", methods=["PUT", "DELETE"])
def api_brand_detail(bid):
    conn = get_db()
    cur = g.cursor
    if request.method == "DELETE":
        try:
            cur.execute("UPDATE products SET brand_id = NULL WHERE brand_id = %s", (bid,))
            cur.execute("DELETE FROM brands WHERE id = %s", (bid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 브랜드 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500

    data = request.get_json(force=True)
    name_provided = "name" in data
    name = (data.get("name") or "").strip() if name_provided else None
    category_id = data.get("category_id")
    color_provided = "color" in data
    color = data.get("color") or "#8a8f98"
    status = data.get("status")

    if name_provided and not name:
        return jsonify({"error": "브랜드명을 입력해주세요."}), 400

    updates = []
    params = []
    if name_provided:
        updates.append("name = %s")
        params.append(name)
    if category_id is not None:
        updates.append("category_id = %s")
        params.append(category_id)
    if color_provided:
        updates.append("color = %s")
        params.append(color)
    if status is not None:
        updates.append("status = %s")
        params.append(status)

    if not updates:
        return jsonify({"error": "변경할 내용이 없습니다."}), 400

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(bid)

    try:
        cur.execute(f"UPDATE brands SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 브랜드 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500

@app.route("/api/brands/batch_approve", methods=["POST"])
def api_brands_batch_approve():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "선택된 브랜드가 없습니다."}), 400
    try:
        ids = [int(i) for i in ids]
    except:
        return jsonify({"error": "올바른 ID가 아닙니다."}), 400

    try:
        placeholders = ','.join(['%s'] * len(ids))
        cur.execute(f"UPDATE brands SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})", ids)
        conn.commit()
        return jsonify({"ok": True, "updated": len(ids)})
    except Exception as e:
        print(f"❌ 일괄 승인 오류: {e}")
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500

@app.route("/api/brands/batch_update", methods=["POST"])
def api_brands_batch_update():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    updates = data.get("updates", {})
    if not ids:
        return jsonify({"error": "선택된 브랜드가 없습니다."}), 400
    if not updates:
        return jsonify({"error": "수정할 항목이 없습니다."}), 400

    allowed_fields = {"category_id", "color", "status"}
    set_clauses = []
    params = []
    for key, value in updates.items():
        if key in allowed_fields:
            set_clauses.append(f"{key} = %s")
            params.append(value)
    if not set_clauses:
        return jsonify({"error": "수정할 수 있는 필드가 없습니다."}), 400
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    placeholders = ','.join(['%s'] * len(ids))
    sql = f"UPDATE brands SET {', '.join(set_clauses)} WHERE id IN ({placeholders})"
    try:
        cur.execute(sql, params + ids)
        conn.commit()
        return jsonify({"ok": True, "updated": len(ids)})
    except Exception as e:
        print(f"❌ 일괄 수정 오류: {e}")
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 매장
# ---------------------------------------------------------------------------

@app.route("/api/stores", methods=["GET", "POST"])
def api_stores():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            cur.execute("SELECT * FROM stores ORDER BY id")
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("schedule"):
                    try:
                        d["schedule"] = json.loads(d["schedule"])
                    except:
                        d["schedule"] = {}
                else:
                    d["schedule"] = {}
                if d.get("staffs"):
                    try:
                        d["staffs"] = json.loads(d["staffs"])
                    except:
                        d["staffs"] = []
                else:
                    d["staffs"] = []
                result.append(d)
            return jsonify(result)
        except Exception as e:
            print(f"⚠️ 매장 목록 조회 오류: {e}")
            return jsonify([])

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "매장명을 입력해주세요."}), 400

    schedule = data.get("schedule") or {}
    schedule_str = json.dumps(schedule)
    staffs = data.get("staffs") or []
    staffs_str = json.dumps(staffs)

    try:
        cur.execute(
            """INSERT INTO stores (name, address, business_number, manager, phone, schedule, staffs)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, data.get("address", ""), data.get("business_number", ""),
             data.get("manager", ""), data.get("phone", ""), schedule_str, staffs_str)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"id": new_id, "name": name})
    except psycopg2.IntegrityError as e:
        if "UNIQUE constraint failed" in str(e):
            return jsonify({"error": "이미 존재하는 매장명입니다."}), 400
        return jsonify({"error": f"데이터베이스 오류: {str(e)}"}), 400
    except Exception as e:
        print(f"❌ 매장 추가 오류: {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

@app.route("/api/stores/<int:sid>", methods=["PUT", "DELETE"])
def api_store_detail(sid):
    conn = get_db()
    cur = g.cursor
    if request.method == "DELETE":
        try:
            cur.execute("DELETE FROM stores WHERE id=%s", (sid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 매장 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "매장명을 입력해주세요."}), 400

    schedule = data.get("schedule")
    staffs = data.get("staffs")
    set_clauses = []
    params = []

    if schedule is not None:
        set_clauses.append("schedule=%s")
        params.append(json.dumps(schedule))
    if staffs is not None:
        set_clauses.append("staffs=%s")
        params.append(json.dumps(staffs))
    if data.get("address") is not None:
        set_clauses.append("address=%s")
        params.append(data.get("address", ""))
    if data.get("business_number") is not None:
        set_clauses.append("business_number=%s")
        params.append(data.get("business_number", ""))
    if data.get("manager") is not None:
        set_clauses.append("manager=%s")
        params.append(data.get("manager", ""))
    if data.get("phone") is not None:
        set_clauses.append("phone=%s")
        params.append(data.get("phone", ""))

    if not set_clauses:
        return jsonify({"error": "수정할 내용이 없습니다."}), 400

    params.append(sid)
    sql = f"UPDATE stores SET name=%s, {', '.join(set_clauses)} WHERE id=%s"
    params.insert(0, name)

    try:
        cur.execute(sql, params)
        conn.commit()
        return jsonify({"ok": True})
    except psycopg2.IntegrityError as e:
        if "UNIQUE constraint failed" in str(e):
            return jsonify({"error": "이미 존재하는 매장명입니다."}), 400
        return jsonify({"error": f"데이터베이스 오류: {str(e)}"}), 400
    except Exception as e:
        print(f"❌ 매장 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 거래처
# ---------------------------------------------------------------------------

@app.route("/api/suppliers", methods=["GET", "POST"])
def api_suppliers():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            cur.execute("SELECT * FROM suppliers ORDER BY id")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            print(f"⚠️ 거래처 조회 오류: {e}")
            return jsonify([])
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "거래처명을 입력해주세요."}), 400
    try:
        cur.execute(
            "INSERT INTO suppliers (name, contact, phone, link, memo) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, data.get("contact"), data.get("phone"), data.get("link"), data.get("memo"))
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"id": new_id, "name": name})
    except Exception as e:
        print(f"❌ 거래처 추가 오류: {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

@app.route("/api/suppliers/<int:sid>", methods=["PUT", "DELETE"])
def api_supplier_detail(sid):
    conn = get_db()
    cur = g.cursor
    if request.method == "DELETE":
        try:
            cur.execute("DELETE FROM suppliers WHERE id=%s", (sid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 거래처 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "거래처명을 입력해주세요."}), 400
    try:
        cur.execute(
            "UPDATE suppliers SET name=%s, contact=%s, phone=%s, link=%s, memo=%s WHERE id=%s",
            (name, data.get("contact"), data.get("phone"), data.get("link"), data.get("memo"), sid)
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 거래처 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 제품
# ---------------------------------------------------------------------------

@app.route("/api/products", methods=["GET", "POST"])
def api_products():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            q = request.args.get("q", "").strip()
            category_id = request.args.get("category_id")
            brand_id = request.args.get("brand_id")
            store_id = request.args.get("store_id")
            sort = request.args.get("sort", "id")
            order = request.args.get("order", "asc")
            show_inactive = request.args.get("show_inactive", "0") == "1"
            min_qty_warning = request.args.get("min_qty_warning", "0") == "1"

            sql = """
                SELECT p.*, b.name as brand_name, b.color as brand_color
                FROM products p
                LEFT JOIN brands b ON b.id = p.brand_id
                WHERE 1=1
            """
            params = []
            if not show_inactive:
                sql += " AND p.is_active=1"
            if q:
                q_norm = normalize_search(q)
                sql += """ AND (
                    REPLACE(p.name, ' ', '') ILIKE %s
                    OR REPLACE(COALESCE(b.name, ''), ' ', '') ILIKE %s
                )"""
                params.append(f"%{q_norm}%")
                params.append(f"%{q_norm}%")
            if category_id:
                sql += " AND p.category_id = %s"
                params.append(category_id)
            if brand_id:
                sql += " AND p.brand_id = %s"
                params.append(brand_id)

            if sort == "qty":
                cur.execute(sql, params)
                rows = cur.fetchall()
                result = []
                for r in rows:
                    try:
                        result.append(product_row_to_dict(conn, r, store_id))
                    except Exception as e:
                        print(f"⚠️ 제품 #{r['id']} 변환 오류: {e}")
                        d = dict(r)
                        d["qty"] = 0
                        d["min_qty"] = 0
                        d["margin_rate"] = None
                        result.append(d)
                reverse = (order.lower() == "desc")
                result.sort(key=lambda x: x.get("qty", 0), reverse=reverse)
                if min_qty_warning:
                    result = [p for p in result if p.get("qty", 0) <= p.get("min_qty", 0)]
                return jsonify(result)
            else:
                allowed_sort = {"id": "id", "name": "name", "cost_price": "cost_price", "sale_price": "sale_price", "card_cost_price": "card_cost_price"}
                sort_col = allowed_sort.get(sort, "id")
                order_sql = "DESC" if order.lower() == "desc" else "ASC"
                sql += f" ORDER BY {sort_col} {order_sql}"
                cur.execute(sql, params)
                rows = cur.fetchall()
                result = []
                for r in rows:
                    try:
                        result.append(product_row_to_dict(conn, r, store_id))
                    except Exception as e:
                        print(f"⚠️ 제품 #{r['id']} 변환 오류: {e}")
                        d = dict(r)
                        d["qty"] = 0
                        d["min_qty"] = 0
                        d["margin_rate"] = None
                        result.append(d)
                if min_qty_warning:
                    result = [p for p in result if p.get("qty", 0) <= p.get("min_qty", 0)]
                return jsonify(result)

        except Exception as e:
            print(f"❌ 제품 목록 API 오류: {e}")
            import traceback
            traceback.print_exc()
            return jsonify([])

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "제품명을 입력해주세요."}), 400

    user_brand_id = data.get("brand_id") or None
    user_category_id = data.get("category_id") or None

    brand_id, category_id = auto_assign_brand_and_category(name, user_brand_id, user_category_id)

    if user_brand_id:
        brand_id = user_brand_id
    if user_category_id:
        category_id = user_category_id

    cost_price = int(data.get("cost_price") or 0)
    card_cost_price = int(data.get("card_cost_price") or 0)
    sale_price = int(data.get("sale_price") or 0)
    memo = data.get("memo") or None
    initial_qty = int(data.get("initial_qty") or 0)
    store_id = data.get("store_id")

    try:
        cur.execute(
            """INSERT INTO products (name, brand_id, category_id, cost_price, card_cost_price, sale_price, memo)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, brand_id, category_id, cost_price, card_cost_price, sale_price, memo)
        )
        product_id = cur.fetchone()["id"]

        if store_id:
            cur.execute(
                """INSERT INTO store_stock (store_id, product_id, qty, min_qty)
                   VALUES (%s, %s, %s, 0)
                   ON CONFLICT(store_id, product_id) DO UPDATE SET qty = excluded.qty""",
                (store_id, product_id, initial_qty)
            )
        else:
            cur.execute("SELECT id FROM stores ORDER BY id")
            stores = cur.fetchall()
            for idx, s in enumerate(stores):
                qty = initial_qty if idx == 0 else 0
                cur.execute(
                    "INSERT INTO store_stock (store_id, product_id, qty, min_qty) VALUES (%s, %s, %s, 0) ON CONFLICT DO NOTHING",
                    (s["id"], product_id, qty)
                )

        conn.commit()
        cur.execute("SELECT * FROM products WHERE id=%s", (product_id,))
        row = cur.fetchone()
        return jsonify(product_row_to_dict(conn, row))
    except psycopg2.IntegrityError as e:
        return jsonify({"error": f"데이터베이스 오류: {str(e)}"}), 400
    except Exception as e:
        print(f"❌ 제품 등록 오류: {e}")
        return jsonify({"error": f"등록 중 오류 발생: {str(e)}"}), 500

@app.route("/api/products/search")
def api_products_search():
    conn = get_db()
    cur = g.cursor
    q = request.args.get("q", "").strip()
    store_id = request.args.get("store_id")
    if not q:
        return jsonify([])
    q_norm = normalize_search(q)
    try:
        cur.execute("""
            SELECT p.*, b.name as brand_name, b.color as brand_color
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            WHERE p.is_active = 1 AND (
                REPLACE(p.name, ' ', '') ILIKE %s
                OR REPLACE(COALESCE(b.name, ''), ' ', '') ILIKE %s
            )
            ORDER BY p.name LIMIT 15
        """, (f"%{q_norm}%", f"%{q_norm}%"))
        rows = cur.fetchall()
        result = []
        for r in rows:
            try:
                result.append(product_row_to_dict(conn, r, store_id))
            except:
                result.append(dict(r))
        return jsonify(result)
    except Exception as e:
        print(f"⚠️ 제품 검색 오류 (q={q}): {e}")
        return jsonify([])

@app.route("/api/products/<int:pid>", methods=["GET", "PUT", "DELETE"])
def api_product_detail(pid):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "제품을 찾을 수 없습니다."}), 404

    if request.method == "GET":
        try:
            return jsonify(product_row_to_dict(conn, row))
        except Exception as e:
            print(f"⚠️ 제품 상세 조회 오류: {e}")
            return jsonify(dict(row))
    if request.method == "DELETE":
        try:
            cur.execute("DELETE FROM stock_transactions WHERE product_id = %s", (pid,))
            cur.execute("DELETE FROM store_stock WHERE product_id = %s", (pid,))
            cur.execute("DELETE FROM price_history WHERE product_id = %s", (pid,))
            cur.execute("DELETE FROM products WHERE id = %s", (pid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 제품 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500

    data = request.get_json(force=True)
    name = data.get("name", row["name"])
    brand_id = data.get("brand_id", row["brand_id"])
    category_id = data.get("category_id", row["category_id"])
    staff = data.get("staff") or ""

    price_fields = ["cost_price", "card_cost_price", "sale_price"]
    for field in price_fields:
        if field in data:
            new_val = int(data[field] or 0)
            old_val = row[field] or 0
            if new_val != old_val:
                cur.execute(
                    "INSERT INTO price_history (product_id, field_name, old_value, new_value, staff) VALUES (%s, %s, %s, %s, %s)",
                    (pid, field, old_val, new_val, staff)
                )

    cost_price = int(data.get("cost_price", row["cost_price"]) or 0)
    card_cost_price = int(data.get("card_cost_price", row["card_cost_price"]) or 0)
    sale_price = int(data.get("sale_price", row["sale_price"]) or 0)
    memo = data.get("memo", row["memo"])
    image_path = data.get("image_path", row["image_path"])
    is_active = data.get("is_active", row["is_active"])

    try:
        cur.execute(
            """UPDATE products SET name=%s, brand_id=%s, category_id=%s, cost_price=%s, card_cost_price=%s,
               sale_price=%s, memo=%s, image_path=%s, is_active=%s, updated_at=CURRENT_TIMESTAMP
               WHERE id=%s""",
            (name, brand_id, category_id, cost_price, card_cost_price, sale_price, memo, image_path, is_active, pid)
        )
        conn.commit()
        cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
        row = cur.fetchone()
        return jsonify(product_row_to_dict(conn, row))
    except Exception as e:
        print(f"❌ 제품 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500

@app.route("/api/products/<int:pid>/price_history")
def api_product_price_history(pid):
    get_db()
    cur = g.cursor
    try:
        cur.execute(
            """SELECT id, product_id, field_name, old_value, new_value, changed_at, staff
               FROM price_history WHERE product_id = %s ORDER BY changed_at DESC""",
            (pid,)
        )
        rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"❌ 가격 변경 이력 조회 오류: {e}")
        return jsonify([])

@app.route("/api/products/batch_update", methods=["POST"])
def api_products_batch_update():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    product_ids = data.get("product_ids", [])
    updates = data.get("updates", {})
    if not product_ids:
        return jsonify({"error": "선택된 제품이 없습니다."}), 400
    if not updates:
        return jsonify({"error": "수정할 항목이 없습니다."}), 400

    placeholders = ','.join(['%s'] * len(product_ids))
    cur.execute(f"SELECT id FROM products WHERE id IN ({placeholders})", product_ids)
    rows = cur.fetchall()
    if not rows:
        return jsonify({"error": "해당 제품을 찾을 수 없습니다."}), 404

    updated = 0
    for r in rows:
        pid = r["id"]
        set_clauses = []
        params = []
        for key, value in updates.items():
            if key in ["brand_id", "category_id", "cost_price", "card_cost_price", "sale_price"]:
                set_clauses.append(f"{key} = %s")
                params.append(value)
        if set_clauses:
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            params.append(pid)
            try:
                cur.execute(f"UPDATE products SET {', '.join(set_clauses)} WHERE id = %s", params)
                updated += 1
            except Exception as e:
                print(f"⚠️ 제품 #{pid} 일괄 수정 오류: {e}")

    conn.commit()
    return jsonify({"ok": True, "updated": updated})

@app.route("/api/products/batch_update_price", methods=["POST"])
def api_batch_update_price():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    product_ids = data.get("product_ids", [])
    field = data.get("field")
    operation = data.get("operation")
    value = data.get("value", 0)

    if not product_ids:
        return jsonify({"error": "선택된 제품이 없습니다."}), 400
    if field not in ("cost_price", "card_cost_price", "sale_price"):
        return jsonify({"error": "올바른 필드를 선택해주세요."}), 400
    if operation not in ("set", "percent"):
        return jsonify({"error": "올바른 방식을 선택해주세요."}), 400
    if operation == "percent" and (value < -100 or value > 1000):
        return jsonify({"error": "증감율은 -100 ~ 1000 사이여야 합니다."}), 400
    if operation == "set" and value < 0:
        return jsonify({"error": "가격은 0 이상이어야 합니다."}), 400

    placeholders = ','.join(['%s'] * len(product_ids))
    cur.execute(f"SELECT id, {field} FROM products WHERE id IN ({placeholders})", product_ids)
    rows = cur.fetchall()
    if not rows:
        return jsonify({"error": "해당 제품을 찾을 수 없습니다."}), 404

    updated = 0
    for r in rows:
        old_val = r[field] or 0
        if operation == "set":
            new_val = int(value)
        else:
            new_val = int(old_val * (1 + value / 100))
            if new_val < 0:
                new_val = 0
        if new_val != old_val:
            try:
                cur.execute(f"UPDATE products SET {field}=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new_val, r["id"]))
                cur.execute(
                    "INSERT INTO price_history (product_id, field_name, old_value, new_value, staff) VALUES (%s, %s, %s, %s, %s)",
                    (r["id"], field, old_val, new_val, "일괄변경")
                )
                updated += 1
            except Exception as e:
                print(f"⚠️ 제품 #{r['id']} 가격 변경 오류: {e}")

    conn.commit()
    return jsonify({"ok": True, "updated": updated})


# ---------------------------------------------------------------------------
# API - 입출고
# ---------------------------------------------------------------------------

@app.route("/api/transactions", methods=["GET"])
def api_transactions_get():
    conn = get_db()
    cur = g.cursor
    try:
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        product_id = request.args.get("product_id")
        store_id = request.args.get("store_id")
        ttype = request.args.get("type")
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
        include_cancelled = request.args.get("include_cancelled", "0") == "1"

        where_clause = "WHERE 1=1"
        params = []
        if start_date:
            where_clause += " AND date(t.date_time) >= date(%s)"
            params.append(start_date)
        if end_date:
            where_clause += " AND date(t.date_time) <= date(%s)"
            params.append(end_date)
        if product_id:
            where_clause += " AND t.product_id = %s"
            params.append(product_id)
        if store_id:
            where_clause += " AND t.store_id = %s"
            params.append(store_id)
        if ttype:
            where_clause += " AND t.type = %s"
            params.append(ttype)
        if not include_cancelled:
            where_clause += " AND t.type NOT IN ('판매취소', '입고취소')"

        count_sql = f"SELECT COUNT(*) as total FROM stock_transactions t {where_clause}"
        cur.execute(count_sql, params)
        total_row = cur.fetchone()
        total = total_row["total"] if total_row else 0

        sql = f"""
            SELECT t.*, p.name as product_name, b.name as brand_name, b.color as brand_color,
                   s.name as store_name, sup.name as supplier_name,
                   (SELECT COUNT(*) FROM stock_transactions c
                    WHERE c.ref_transaction_id = t.id AND c.type IN ('판매취소', '입고취소')) as cancel_count
            FROM stock_transactions t
            JOIN products p ON p.id = t.product_id
            LEFT JOIN brands b ON b.id = p.brand_id
            JOIN stores s ON s.id = t.store_id
            LEFT JOIN suppliers sup ON sup.id = t.supplier_id
            {where_clause}
            ORDER BY t.date_time DESC, t.id DESC
            LIMIT %s OFFSET %s
        """
        params_with_pagination = params + [limit, offset]
        cur.execute(sql, params_with_pagination)
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["cancelled"] = d["type"] in ("판매출고", "입고") and d["cancel_count"] > 0
            if d["type"] in ["판매취소", "입고취소"] or d["cancelled"]:
                d["is_cancelled"] = True
            else:
                d["is_cancelled"] = False
            result.append(d)

        return jsonify({
            "items": result,
            "total": total,
            "limit": limit,
            "offset": offset
        })
    except Exception as e:
        print(f"❌ 입출고 목록 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": f"데이터를 불러오는 중 오류 발생: {str(e)}",
            "items": [],
            "total": 0,
            "limit": 20,
            "offset": 0
        }), 500

@app.route("/api/transactions", methods=["POST"])
def api_transactions_post():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    product_id = data.get("product_id")
    store_id = data.get("store_id")
    ttype = data.get("type")
    quantity = data.get("quantity")
    staff = data.get("staff") or ""
    memo = data.get("memo") or None
    date_time = data.get("date_time") or None
    supplier_id = data.get("supplier_id") or None

    if not product_id or not store_id or not ttype:
        return jsonify({"error": "제품, 매장, 유형은 필수입니다."}), 400
    if ttype not in ["입고", "판매출고", "반품", "폐기", "조정", "실사조정", "이동출고", "이동입고", "선결예약", "입고취소"]:
        return jsonify({"error": "이 단계에서 지원하지 않는 유형입니다."}), 400
    try:
        quantity = int(quantity)
        assert quantity > 0
    except:
        return jsonify({"error": "수량은 1 이상의 숫자여야 합니다."}), 400

    cur.execute("SELECT * FROM products WHERE id=%s", (product_id,))
    product = cur.fetchone()
    if not product:
        return jsonify({"error": "제품을 찾을 수 없습니다."}), 404

    err = _apply_stock_delta(conn, store_id, product_id, ttype, quantity)
    if err:
        return jsonify({"error": err}), 400

    unit_cost = product["cost_price"]
    unit_price = product["sale_price"] if ttype == "판매출고" else None

    try:
        cur.execute(
            """INSERT INTO stock_transactions
            (product_id, store_id, supplier_id, type, quantity, unit_cost, unit_price, staff, memo, date_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP)) RETURNING id""",
            (product_id, store_id, supplier_id, ttype, quantity, unit_cost, unit_price, staff, memo, date_time)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"id": new_id, "ok": True})
    except Exception as e:
        print(f"❌ 입출고 등록 오류: {e}")
        return jsonify({"error": "등록 중 오류가 발생했습니다."}), 500

_DECREASE_TYPES = {"판매출고", "반품", "폐기", "이동출고", "조정", "입고취소"}


def _reverse_stock_raw(store_id, product_id, ttype, quantity):
    """주어진 거래가 재고에 준 영향을 그대로 되돌린다 (검증 없이 원상복구용)."""
    cur = g.cursor
    sign = -1 if ttype in _DECREASE_TYPES else 1
    # 원래 거래가 재고를 sign*quantity 만큼 바꿨으므로, 되돌리려면 반대로 적용한다.
    cur.execute("UPDATE store_stock SET qty = qty - (%s) WHERE store_id=%s AND product_id=%s", (sign * quantity, store_id, product_id))


@app.route("/api/transactions/<int:tid>", methods=["PUT"])
def api_transaction_update(tid):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM stock_transactions WHERE id=%s", (tid,))
    original = cur.fetchone()
    if not original:
        return jsonify({"error": "거래를 찾을 수 없습니다."}), 404

    # 이미 취소되었거나, 다른 거래를 취소/되돌리기 한 기록 자체는 수정할 수 없다.
    if original["type"] in ("판매취소", "입고취소"):
        return jsonify({"error": "취소 기록은 수정할 수 없습니다."}), 400
    cur.execute(
        "SELECT id FROM stock_transactions WHERE ref_transaction_id=%s AND type IN ('입고취소', '판매취소')",
        (tid,)
    )
    if cur.fetchone():
        return jsonify({"error": "이미 취소된 거래는 수정할 수 없습니다. 먼저 취소를 해제해주세요."}), 400

    data = request.get_json(force=True)
    new_quantity = data.get("quantity", original["quantity"])
    new_type = data.get("type", original["type"])
    new_store_id = data.get("store_id", original["store_id"])
    new_unit_price = data.get("unit_price", original["unit_price"])
    new_unit_cost = data.get("unit_cost", original["unit_cost"])
    new_staff = data.get("staff", original["staff"])
    new_memo = data.get("memo", original["memo"])
    new_date_time = data.get("date_time") or original["date_time"]

    if new_type not in ["입고", "판매출고", "반품", "폐기", "조정", "실사조정", "이동출고", "이동입고", "선결예약"]:
        return jsonify({"error": "이 유형은 수정할 수 없습니다."}), 400
    try:
        new_quantity = int(new_quantity)
        assert new_quantity > 0
    except (TypeError, ValueError, AssertionError):
        return jsonify({"error": "수량은 1 이상의 숫자여야 합니다."}), 400

    try:
        # 1) 기존 거래가 재고에 미친 영향을 되돌린다.
        _reverse_stock_raw(original["store_id"], original["product_id"], original["type"], original["quantity"])
        # 2) 수정된 값으로 재고를 다시 반영한다. 재고 부족 등으로 실패하면 원래 상태로 롤백한다.
        err = _apply_stock_delta(conn, new_store_id, original["product_id"], new_type, new_quantity)
        if err:
            conn.rollback()
            return jsonify({"error": err}), 400

        cur.execute(
            """UPDATE stock_transactions
               SET type=%s, store_id=%s, quantity=%s, unit_cost=%s, unit_price=%s,
                   staff=%s, memo=%s, date_time=%s
               WHERE id=%s""",
            (new_type, new_store_id, new_quantity, new_unit_cost, new_unit_price,
             new_staff, new_memo, new_date_time, tid)
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        print(f"❌ 거래 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500


@app.route("/api/transactions/<int:tid>/cancel", methods=["POST"])
def api_transaction_cancel(tid):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM stock_transactions WHERE id=%s", (tid,))
    original = cur.fetchone()
    if not original:
        return jsonify({"error": "원본 거래를 찾을 수 없습니다."}), 404
    if original["type"] != "판매출고":
        return jsonify({"error": "판매출고 건만 취소할 수 있습니다."}), 400
    cur.execute("SELECT COUNT(*) as c FROM stock_transactions WHERE ref_transaction_id=%s AND type='판매취소'", (tid,))
    already = cur.fetchone()["c"]
    if already:
        return jsonify({"error": "이미 취소 처리된 거래입니다."}), 400

    data = request.get_json(force=True) if request.data else {}
    reason = data.get("reason") or "취소/반품"
    staff = data.get("staff") or ""

    err = _apply_stock_delta(conn, original["store_id"], original["product_id"], "판매취소", original["quantity"])
    if err:
        return jsonify({"error": err}), 400

    try:
        cur.execute(
            """INSERT INTO stock_transactions
            (product_id, store_id, ref_transaction_id, type, quantity, unit_cost, unit_price,
             payment_method, staff, memo)
            VALUES (%s, %s, %s, '판매취소', %s, %s, %s, %s, %s, %s)""",
            (original["product_id"], original["store_id"], tid, original["quantity"],
             original["unit_cost"], original["unit_price"], original["payment_method"],
             staff, reason)
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 거래 취소 오류: {e}")
        return jsonify({"error": "취소 중 오류가 발생했습니다."}), 500

@app.route("/api/transactions/<int:tid>/undo", methods=["POST"])
def api_transaction_undo(tid):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM stock_transactions WHERE id=%s", (tid,))
    trans = cur.fetchone()
    if not trans:
        return jsonify({"error": "거래를 찾을 수 없습니다."}), 404

    cur.execute("SELECT id FROM stock_transactions WHERE ref_transaction_id=%s AND type IN ('입고취소', '판매취소')", (tid,))
    cancelled = cur.fetchone()
    if cancelled:
        return jsonify({"error": "이미 취소된 거래입니다."}), 400

    reverse_map = {
        "입고": "입고취소",
        "판매출고": "판매취소",
        "반품": "입고",
        "폐기": "입고",
    }
    reverse_type = reverse_map.get(trans["type"])
    if not reverse_type:
        return jsonify({"error": f"'{trans['type']}' 유형은 취소할 수 없습니다."}), 400

    product_id = trans["product_id"]
    store_id = trans["store_id"]
    quantity = trans["quantity"]

    err = _apply_stock_delta(conn, store_id, product_id, reverse_type, quantity)
    if err:
        return jsonify({"error": err}), 400

    try:
        cur.execute(
            """INSERT INTO stock_transactions
            (product_id, store_id, ref_transaction_id, type, quantity, unit_cost, unit_price,
             payment_method, staff, memo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (product_id, store_id, tid, reverse_type, quantity,
             trans["unit_cost"], trans["unit_price"],
             trans["payment_method"], trans["staff"] or "", f"{trans['type']} 취소")
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "undo_id": new_id})
    except Exception as e:
        print(f"❌ 되돌리기 오류: {e}")
        return jsonify({"error": "되돌리기 중 오류가 발생했습니다."}), 500

@app.route("/api/transactions/cancel_batch", methods=["POST"])
def api_transactions_cancel_batch():
    conn = get_db()
    cur = g.cursor
    try:
        data = request.get_json(force=True)
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "삭제할 항목을 선택해주세요."}), 400

        ids = [int(i) for i in ids]
        placeholders = ','.join(['%s'] * len(ids))
        cur.execute(f"SELECT * FROM stock_transactions WHERE id IN ({placeholders})", ids)
        rows = cur.fetchall()
        sale_rows = [r for r in rows if r["type"] == "판매출고"]

        if not sale_rows:
            return jsonify({"cancelled": 0, "failed": len(ids), "errors": ["선택한 항목 중 판매출고가 없습니다."]}), 400

        # 이미 취소(판매취소) 처리된 판매출고 건은 삭제 대상에서 제외한다.
        # 원본을 지워버리면 짝이 맞아야 할 판매취소(-) 기록만 고아로 남아
        # 매출/이익 집계가 원인 모를 마이너스로 잡히는 버그가 있었기 때문.
        valid_rows = []
        already_cancelled_count = 0
        for row in sale_rows:
            cur.execute(
                "SELECT COUNT(*) as c FROM stock_transactions WHERE ref_transaction_id=%s AND type='판매취소'",
                (row["id"],)
            )
            already_cancelled = cur.fetchone()["c"] > 0
            if already_cancelled:
                already_cancelled_count += 1
            else:
                valid_rows.append(row)

        errors = []
        if already_cancelled_count:
            errors.append(f"이미 취소 처리된 {already_cancelled_count}건은 건너뛰었습니다. (취소 기록만 남아 매출이 왜곡되는 것을 방지)")

        if not valid_rows:
            return jsonify({"cancelled": 0, "failed": len(ids), "errors": errors or ["선택한 항목이 모두 이미 취소된 건입니다."]}), 400

        # 판매출고를 완전히 삭제하기 전에 매장 재고를 판매 전 상태로 복구한다.
        for row in valid_rows:
            _apply_stock_delta(conn, row["store_id"], row["product_id"], "판매취소", row["quantity"])

        valid_ids = [r["id"] for r in valid_rows]
        placeholders2 = ','.join(['%s'] * len(valid_ids))
        cur.execute(f"DELETE FROM stock_transactions WHERE id IN ({placeholders2})", valid_ids)
        conn.commit()

        return jsonify({
            "cancelled": len(valid_ids),
            "failed": len(ids) - len(valid_ids),
            "errors": errors
        })
    except Exception as e:
        conn.rollback()
        print(f"❌ 일괄 삭제 오류: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API - 재고 이동
# ---------------------------------------------------------------------------

@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)

    items = data.get("items")
    from_store_id = data.get("from_store_id")
    to_store_id = data.get("to_store_id")
    staff = data.get("staff") or ""
    memo = data.get("memo") or None

    if not from_store_id or not to_store_id:
        return jsonify({"error": "출발 매장과 도착 매장을 선택해주세요."}), 400
    if from_store_id == to_store_id:
        return jsonify({"error": "출발 매장과 도착 매장이 같습니다."}), 400

    cur.execute("SELECT name FROM stores WHERE id = %s", (from_store_id,))
    from_store = cur.fetchone()
    cur.execute("SELECT name FROM stores WHERE id = %s", (to_store_id,))
    to_store = cur.fetchone()
    from_name = from_store["name"] if from_store else "알수없음"
    to_name = to_store["name"] if to_store else "알수없음"

    if not items:
        product_id = data.get("product_id")
        quantity = data.get("quantity")
        if not product_id or not quantity:
            return jsonify({"error": "제품과 수량을 입력해주세요."}), 400
        items = [{"product_id": product_id, "quantity": quantity}]

    results = {"success": [], "failed": []}

    for item in items:
        product_id = item.get("product_id")
        quantity = item.get("quantity")

        if not product_id or not quantity:
            results["failed"].append({"product_id": product_id, "error": "제품 또는 수량 누락"})
            continue

        try:
            quantity = int(quantity)
            if quantity <= 0:
                raise ValueError("수량은 1 이상이어야 합니다.")
        except:
            results["failed"].append({"product_id": product_id, "error": "수량이 올바르지 않습니다."})
            continue

        cur.execute("SELECT qty FROM store_stock WHERE store_id=%s AND product_id=%s", (from_store_id, product_id))
        stock = cur.fetchone()
        if not stock or stock["qty"] < quantity:
            results["failed"].append({"product_id": product_id, "error": f"재고 부족 (현재: {stock['qty'] if stock else 0})"})
            continue

        try:
            out_memo = f"({to_name} 이동)" if not memo else memo
            cur.execute(
                """INSERT INTO stock_transactions
                   (product_id, store_id, type, quantity, unit_cost, unit_price, staff, memo, date_time)
                   VALUES (%s, %s, '이동출고', %s, 0, 0, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
                (product_id, from_store_id, quantity, staff, out_memo)
            )
            out_trans_id = cur.fetchone()["id"]

            in_memo = f"({from_name}에서 줌)" if not memo else f"({from_name}에서 줌)"
            cur.execute(
                """INSERT INTO stock_transactions
                   (product_id, store_id, ref_transaction_id, type, quantity, unit_cost, unit_price, staff, memo, date_time)
                   VALUES (%s, %s, %s, '이동입고', %s, 0, 0, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
                (product_id, to_store_id, out_trans_id, quantity, staff, in_memo)
            )
            in_trans_id = cur.fetchone()["id"]

            cur.execute("UPDATE stock_transactions SET ref_transaction_id = %s WHERE id = %s", (in_trans_id, out_trans_id))

            cur.execute(
                """INSERT INTO stock_movements (from_store_id, to_store_id, product_id, quantity, staff, memo, status, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, '완료', CURRENT_TIMESTAMP) RETURNING id""",
                (from_store_id, to_store_id, product_id, quantity, staff, out_memo)
            )
            movement_id = cur.fetchone()["id"]

            cur.execute(
                "UPDATE stock_transactions SET movement_id = %s WHERE id IN (%s, %s)",
                (movement_id, out_trans_id, in_trans_id)
            )

            _apply_stock_delta(conn, from_store_id, product_id, "이동출고", quantity)
            _apply_stock_delta(conn, to_store_id, product_id, "이동입고", quantity)

            conn.commit()
            results["success"].append({"product_id": product_id, "quantity": quantity, "movement_id": movement_id})

        except Exception as e:
            conn.rollback()
            results["failed"].append({"product_id": product_id, "error": str(e)})

    return jsonify({
        "ok": True,
        "success": results["success"],
        "failed": results["failed"]
    })

@app.route("/api/transfer/<int:movement_id>/cancel", methods=["POST"])
def api_transfer_cancel(movement_id):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM stock_movements WHERE id = %s", (movement_id,))
    movement = cur.fetchone()
    if not movement:
        return jsonify({"error": "이동 기록을 찾을 수 없습니다."}), 404
    if movement["status"] != "완료":
        return jsonify({"error": "완료된 이동만 취소할 수 있습니다."}), 400
    if movement["status"] == "취소":
        return jsonify({"error": "이미 취소된 이동입니다."}), 400

    try:
        err1 = _apply_stock_delta(conn, movement["from_store_id"], movement["product_id"], "입고", movement["quantity"])
        if err1:
            return jsonify({"error": f"출발 매장 재고 복구 실패: {err1}"}), 400
        err2 = _apply_stock_delta(conn, movement["to_store_id"], movement["product_id"], "판매출고", movement["quantity"])
        if err2:
            return jsonify({"error": f"도착 매장 재고 복구 실패: {err2}"}), 400

        cur.execute(
            """INSERT INTO stock_transactions
            (product_id, store_id, type, quantity, staff, memo)
            VALUES (%s, %s, '이동취소', %s, %s, %s)""",
            (movement["product_id"], movement["from_store_id"], movement["quantity"],
             movement["staff"], f"이동 취소 (ID: {movement_id})")
        )

        cur.execute(
            "UPDATE stock_movements SET status = '취소', cancelled_at = CURRENT_TIMESTAMP WHERE id = %s",
            (movement_id,)
        )
        conn.commit()
        return jsonify({"ok": True, "movement_id": movement_id})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"취소 중 오류: {str(e)}"}), 500

@app.route("/api/movements/cancel_batch", methods=["POST"])
def api_movements_cancel_batch():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "취소할 항목을 선택해주세요."}), 400

    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "잘못된 항목입니다."}), 400

    cancelled = 0
    failed = 0
    errors = []

    for movement_id in ids:
        try:
            cur.execute("SELECT * FROM stock_movements WHERE id = %s", (movement_id,))
            movement = cur.fetchone()
            if not movement:
                failed += 1
                errors.append(f"#{movement_id}: 이동 기록을 찾을 수 없습니다.")
                continue
            if movement["status"] != "완료":
                failed += 1
                errors.append(f"#{movement_id}: 완료된 이동만 취소할 수 있습니다.")
                continue

            err1 = _apply_stock_delta(conn, movement["from_store_id"], movement["product_id"], "입고", movement["quantity"])
            if err1:
                conn.rollback()
                failed += 1
                errors.append(f"#{movement_id}: {err1}")
                continue
            err2 = _apply_stock_delta(conn, movement["to_store_id"], movement["product_id"], "판매출고", movement["quantity"])
            if err2:
                conn.rollback()
                failed += 1
                errors.append(f"#{movement_id}: {err2}")
                continue

            cur.execute(
                """INSERT INTO stock_transactions
                (product_id, store_id, type, quantity, staff, memo)
                VALUES (%s, %s, '이동취소', %s, %s, %s)""",
                (movement["product_id"], movement["from_store_id"], movement["quantity"],
                 movement["staff"], f"이동 취소 (ID: {movement_id})")
            )
            cur.execute(
                "UPDATE stock_movements SET status = '취소', cancelled_at = CURRENT_TIMESTAMP WHERE id = %s",
                (movement_id,)
            )
            conn.commit()
            cancelled += 1
        except Exception as e:
            conn.rollback()
            failed += 1
            errors.append(f"#{movement_id}: {str(e)}")

    return jsonify({"ok": True, "cancelled": cancelled, "failed": failed, "errors": errors})


@app.route("/api/movements")
def api_movements():
    conn = get_db()
    cur = g.cursor
    try:
        status = request.args.get("status")
        store_id = request.args.get("store_id")

        sql = """
            SELECT m.*, p.name as product_name, p.cost_price as unit_cost,
                   (m.quantity * p.cost_price) as total_cost,
                   fs.name as from_store_name, ts.name as to_store_name
            FROM stock_movements m
            JOIN products p ON p.id = m.product_id
            JOIN stores fs ON fs.id = m.from_store_id
            JOIN stores ts ON ts.id = m.to_store_id
            WHERE 1=1
        """
        params = []
        if status:
            sql += " AND m.status = %s"
            params.append(status)
        if store_id:
            sql += " AND (m.from_store_id = %s OR m.to_store_id = %s)"
            params.extend([store_id, store_id])
        sql += " ORDER BY m.created_at DESC LIMIT 50"

        cur.execute(sql, params)
        rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"❌ 이동 내역 조회 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# API - 금일 보고 목록 (매장별 브랜드 그룹 판매 집계)
# ---------------------------------------------------------------------------

def _brand_sales_rows(cur, store_id, date_str, brand_names):
    """지정한 브랜드(들)의 특정 매장/날짜 순 판매 수량(제품명별)을 반환한다."""
    if not brand_names:
        return []
    placeholders = ",".join(["%s"] * len(brand_names))
    sql = f"""
        SELECT p.name as product_name,
               COALESCE(SUM(CASE WHEN t.type = '판매출고' THEN t.quantity ELSE 0 END), 0)
               - COALESCE(SUM(CASE WHEN t.type = '판매취소' THEN t.quantity ELSE 0 END), 0) as qty
        FROM stock_transactions t
        JOIN products p ON p.id = t.product_id
        JOIN brands b ON b.id = p.brand_id
        WHERE b.name IN ({placeholders})
          AND t.store_id = %s
          AND date(t.date_time) = %s
          AND t.type IN ('판매출고', '판매취소')
        GROUP BY p.id, p.name
    """
    cur.execute(sql, list(brand_names) + [store_id, date_str])
    return [(r["product_name"], r["qty"]) for r in cur.fetchall()]


def _bucket_by_percent(rows):
    """제품명 맨 앞의 'N%' 표기로 그룹화 (예: '2% 딸기키위' -> '2%')."""
    buckets = {}
    for name, qty in rows:
        m = re.match(r"^\s*(\d+)\s*%", name or "")
        if not m:
            continue
        label = f"{m.group(1)}%"
        buckets[label] = buckets.get(label, 0) + (qty or 0)
    return buckets


def _all_percent_labels_for_brand(cur, brand_names):
    """오늘 판매 여부와 상관없이, 해당 브랜드에 등록된 모든 제품명에서
    'N%' 표기를 전부 뽑아 라벨 집합으로 반환한다.
    (오늘 판매가 0개인 %도 항상 목록에 표시하기 위함)"""
    if not brand_names:
        return set()
    placeholders = ",".join(["%s"] * len(brand_names))
    cur.execute(f"""
        SELECT p.name as product_name
        FROM products p
        JOIN brands b ON b.id = p.brand_id
        WHERE b.name IN ({placeholders})
    """, list(brand_names))
    labels = set()
    for r in cur.fetchall():
        m = re.match(r"^\s*(\d+)\s*%", r["product_name"] or "")
        if m:
            labels.add(f"{m.group(1)}%")
    return labels


def _bucket_by_prefix(rows, prefix_labels):
    """제품명 맨 앞 접두어로 그룹화. prefix_labels: [(레이블, [접두어,...]), ...] 순서대로 매칭."""
    buckets = {label: 0 for label, _ in prefix_labels}
    for name, qty in rows:
        n = (name or "").strip()
        for label, prefixes in prefix_labels:
            if any(n.startswith(p) for p in prefixes):
                buckets[label] += (qty or 0)
                break
    return buckets


@app.route("/api/daily_sales_summary")
def api_daily_sales_summary():
    conn = get_db()
    cur = g.cursor
    try:
        store_id = request.args.get("store_id")
        date_str = request.args.get("date") or now_kst().strftime("%Y-%m-%d")
        if not store_id:
            return jsonify({"error": "매장을 선택해주세요."}), 400

        cur.execute("SELECT name FROM stores WHERE id=%s", (store_id,))
        store_row = cur.fetchone()
        if not store_row:
            return jsonify({"error": "매장을 찾을 수 없습니다."}), 404
        store_name = store_row["name"]
        date_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d")

        sections = []

        # ---- 플릭(일회용, DB상 브랜드명은 "플릭 슬림") : 니코틴 %로 그룹화 ----
        flik_rows = _brand_sales_rows(cur, store_id, date_str, ["플릭 슬림"])
        flik_buckets = _bucket_by_percent(flik_rows)
        # 플릭은 0%/1% 두 종류만 존재 → 오늘 판매가 없어도 0개로 항상 표시
        for _pct in ["0%", "1%"]:
            flik_buckets.setdefault(_pct, 0)
        sections.append({
            "key": "flik",
            "title": f"[{date_label} {store_name} 플릭]",
            "lines": [{"label": k, "qty": v} for k, v in sorted(flik_buckets.items(), key=lambda x: int(x[0].rstrip('%')))],
            "extra_lines": [],
            "total": sum(flik_buckets.values())
        })

        # ---- 엘프바(일회용) : 니코틴 % + 조인원 킷/팟 ----
        elfbar_rows = _brand_sales_rows(cur, store_id, date_str, ["엘프바 25K 아이스킹"])
        elfbar_buckets = _bucket_by_percent(elfbar_rows)
        # 오늘 판매가 없는 %도(예: 1%) 항상 표시되도록, 제품으로 등록된 % 옵션은 모두 0으로 채워둔다
        for _pct in _all_percent_labels_for_brand(cur, ["엘프바 25K 아이스킹"]):
            elfbar_buckets.setdefault(_pct, 0)
        joinone_rows = _brand_sales_rows(cur, store_id, date_str, ["엘프바 조인원"])
        joinone_buckets = _bucket_by_prefix(joinone_rows, [("조인원 킷", ["킷"]), ("조인원 팟", ["팟"])])
        sections.append({
            "key": "elfbar",
            "title": f"[{date_label} {store_name} 엘프바]",
            "lines": [{"label": k, "qty": v} for k, v in sorted(elfbar_buckets.items(), key=lambda x: int(x[0].rstrip('%')))],
            "extra_lines": [{"label": k, "qty": v} for k, v in joinone_buckets.items()],
            "total": sum(elfbar_buckets.values()) + sum(joinone_buckets.values())
        })

        # ---- 칠렉스 바이브(일회용) : 킷/팟 (브랜드 자체가 분리되어 있음) ----
        vibe_kit_rows = _brand_sales_rows(cur, store_id, date_str, ["칠렉스 바이브 킷"])
        vibe_pod_rows = _brand_sales_rows(cur, store_id, date_str, ["칠렉스 바이브 팟"])
        vibe_kit_qty = sum(q for _, q in vibe_kit_rows)
        vibe_pod_qty = sum(q for _, q in vibe_pod_rows)
        sections.append({
            "key": "chillex_vibe",
            "title": f"[{date_label} {store_name} 칠렉스 바이브]",
            "lines": [{"label": "바이브 킷", "qty": vibe_kit_qty}, {"label": "바이브 팟", "qty": vibe_pod_qty}],
            "extra_lines": [],
            "total": vibe_kit_qty + vibe_pod_qty
        })

        # ---- 카오린 전자담배 : 스타터킷 / 카트리지(킷+팟) / 배터리 ----
        kaorin_rows = _brand_sales_rows(cur, store_id, date_str, ["카오린"])
        kaorin_buckets = _bucket_by_prefix(kaorin_rows, [
            ("배터리", ["배터리"]),
            ("카트리지", ["킷", "팟"]),
        ])
        # 접두어가 없는 나머지는 스타터킷으로 처리
        starter_qty = sum(q for _, q in kaorin_rows) - sum(kaorin_buckets.values())
        sections.append({
            "key": "kaorin",
            "title": f"[{date_label} {store_name} 카오린 전자담배]",
            "lines": [
                {"label": "스타터킷", "qty": starter_qty},
                {"label": "카트리지", "qty": kaorin_buckets["카트리지"]},
                {"label": "배터리", "qty": kaorin_buckets["배터리"]},
            ],
            "extra_lines": [],
            "total": starter_qty + kaorin_buckets["카트리지"] + kaorin_buckets["배터리"]
        })

        return jsonify({
            "store_id": int(store_id),
            "store_name": store_name,
            "date": date_str,
            "sections": sections
        })
    except Exception as e:
        print(f"❌ 금일 보고 목록 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "데이터를 불러오는 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 금일 판매 목록
# ---------------------------------------------------------------------------

@app.route("/api/daily_report")
def api_daily_report():
    conn = get_db()
    cur = g.cursor
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "매장을 선택해주세요."}), 400

    try:
        today = now_kst().strftime("%Y-%m-%d")
        date_display = now_kst().strftime("%m/%d")

        cur.execute("SELECT name FROM stores WHERE id=%s", (store_id,))
        store = cur.fetchone()
        store_name = store["name"] if store else ""

        brand_list = get_brand_list_from_db()
        if not brand_list:
            brand_list = [
                "플릭", "네스티 리큐", "블루몽", "네스티바 30K", "칠렉스 바이브",
                "카오린", "네스티바 20K", "릴렉스 팟", "말론바", "메가킥",
                "델리", "블라스트", "카오린 팟", "깔끔 액상",
                "말론 S", "버니 액상", "네스티원 40K", "엘프바 조인원 팟",
                "발라리안 R", "하복 SE", "릴렉스 인피니티 2 플러스",
                "펀치밤", "얼려먹구싶오", "콩이랑 망고", "네스티 블라스트",
                "네스티원", "플릭 슬림", "릴렉스", "네스티바 30K",
                "팬텀 MK.1 프로토타입", "수리퀴드"
            ]
            brand_list.sort(key=len, reverse=True)

        cur.execute("""
            SELECT t.id as trans_id, t.type, t.quantity, p.name as product_name,
                   c.name as category_name, t.memo,
                   t.ref_transaction_id, t2.memo as ref_memo,
                   b.name as brand_name,
                   t.store_id
            FROM stock_transactions t
            JOIN products p ON p.id = t.product_id
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN stock_transactions t2 ON t2.id = t.ref_transaction_id
            WHERE t.type IN ('판매출고', '판매취소', '입고', '입고취소', '이동출고', '이동입고')
              AND date(t.date_time) = date(%s)
              AND t.store_id = %s
              AND EXTRACT(HOUR FROM t.date_time) BETWEEN 8 AND 15
              AND NOT (
                    t.type IN ('판매취소', '입고취소')
                 OR (t.type = '판매출고' AND EXISTS (
                        SELECT 1 FROM stock_transactions cx
                        WHERE cx.ref_transaction_id = t.id AND cx.type = '판매취소'))
                 OR (t.type = '입고' AND EXISTS (
                        SELECT 1 FROM stock_transactions cx
                        WHERE cx.ref_transaction_id = t.id AND cx.type = '입고취소'))
                 OR (t.type IN ('이동출고', '이동입고') AND EXISTS (
                        SELECT 1 FROM stock_movements sm
                        WHERE sm.id = t.movement_id AND sm.status = '취소'))
              )
            ORDER BY t.date_time
        """, (today, store_id))
        morning_rows = cur.fetchall()

        cur.execute("""
            SELECT t.id as trans_id, t.type, t.quantity, p.name as product_name,
                   c.name as category_name, t.memo,
                   t.ref_transaction_id, t2.memo as ref_memo,
                   b.name as brand_name,
                   t.store_id
            FROM stock_transactions t
            JOIN products p ON p.id = t.product_id
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN stock_transactions t2 ON t2.id = t.ref_transaction_id
            WHERE t.type IN ('판매출고', '판매취소', '입고', '입고취소', '이동출고', '이동입고')
              AND date(t.date_time) = date(%s)
              AND t.store_id = %s
              AND EXTRACT(HOUR FROM t.date_time) >= 16
              AND NOT (
                    t.type IN ('판매취소', '입고취소')
                 OR (t.type = '판매출고' AND EXISTS (
                        SELECT 1 FROM stock_transactions cx
                        WHERE cx.ref_transaction_id = t.id AND cx.type = '판매취소'))
                 OR (t.type = '입고' AND EXISTS (
                        SELECT 1 FROM stock_transactions cx
                        WHERE cx.ref_transaction_id = t.id AND cx.type = '입고취소'))
                 OR (t.type IN ('이동출고', '이동입고') AND EXISTS (
                        SELECT 1 FROM stock_movements sm
                        WHERE sm.id = t.movement_id AND sm.status = '취소'))
              )
            ORDER BY t.date_time
        """, (today, store_id))
        afternoon_rows = cur.fetchall()

        def extract_brand_and_product(full_name, db_brand_name):
            if db_brand_name:
                remaining = full_name.replace(db_brand_name, "").strip()
                remaining = re.sub(r'\s*(액상|팟)\s*', ' ', remaining).strip()
                remaining = re.sub(r'\s+', ' ', remaining)
                return db_brand_name, remaining
            for brand in brand_list:
                if brand in full_name:
                    remaining = full_name.replace(brand, "").strip()
                    remaining = re.sub(r'\s*(액상|팟)\s*', ' ', remaining).strip()
                    remaining = re.sub(r'\s+', ' ', remaining)
                    return brand, remaining
            return None, full_name

        def build_report(rows, time_label):
            grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

            for r in rows:
                cat = r['category_name'] or '미분류'
                typ = r['type']
                full_name = r['product_name']
                qty = r['quantity']
                memo = r['memo'] or None
                ref_memo = r['ref_memo'] or None
                db_brand_name = r['brand_name']
                store_id = r['store_id']
                trans_id = r['trans_id']

                brand, display_name = extract_brand_and_product(full_name, db_brand_name)

                extra_info = ""
                if typ == '이동입고':
                    if memo:
                        match = re.search(r'\((\S+?)\s*에서 줌\)', memo)
                        if match:
                            extra_info = f"({match.group(1)}에서 줌)"
                    if not extra_info and ref_memo:
                        match = re.search(r'^(\S+?)\s*이동', ref_memo)
                        if match:
                            extra_info = f"({match.group(1)}에서 줌)"
                    if not extra_info and r['ref_transaction_id']:
                        try:
                            cur.execute("SELECT store_id FROM stock_transactions WHERE id = %s", (r['ref_transaction_id'],))
                            ref_row = cur.fetchone()
                            if ref_row:
                                cur.execute("SELECT name FROM stores WHERE id = %s", (ref_row['store_id'],))
                                from_store = cur.fetchone()
                                if from_store:
                                    extra_info = f"({from_store['name']}에서 줌)"
                        except:
                            pass
                elif typ == '이동출고':
                    if memo:
                        match = re.search(r'\((\S+?)\s*이동\)', memo)
                        if match:
                            extra_info = f"({match.group(1)} 이동)"
                    if not extra_info:
                        try:
                            cur.execute("SELECT store_id FROM stock_transactions WHERE ref_transaction_id = %s AND type = '이동입고' LIMIT 1", (trans_id,))
                            target_row = cur.fetchone()
                            if target_row:
                                cur.execute("SELECT name FROM stores WHERE id = %s", (target_row['store_id'],))
                                to_store = cur.fetchone()
                                if to_store:
                                    extra_info = f"({to_store['name']} 이동)"
                        except:
                            pass
                elif memo and '이동' in memo:
                    match = re.search(r'(\S+?)\s*이동', memo)
                    if match:
                        store = match.group(1)
                        if typ == '입고':
                            extra_info = f"({store}에서 줌)"
                        elif typ == '판매출고':
                            extra_info = f"({store} 이동)"

                if typ in ['판매취소', '입고취소']:
                    qty = -qty

                if qty == 0:
                    continue

                brand_key = brand if brand else '기타'
                grouped[cat][typ][brand_key].append((display_name, qty, extra_info))

            # 카테고리 표기 순서: 기기 -> 액상 -> 일회용
            CATEGORY_ORDER = ["기기", "액상", "일회용"]

            def build_section(types_filter):
                """카테고리별 블록(줄 리스트)들을 만들어 리스트로 반환.
                구분선(-----)은 여기서 넣지 않고, 호출부에서 블록 '사이'에만 넣는다."""
                blocks = []
                for cat in CATEGORY_ORDER:
                    if cat not in grouped:
                        continue
                    cat_types = [t for t in grouped[cat].keys() if t in types_filter]
                    if not cat_types:
                        continue

                    block = [f"{{{cat}}}"]
                    brand_order = []
                    for typ in sorted(cat_types):
                        for brand in grouped[cat][typ].keys():
                            if brand not in brand_order:
                                brand_order.append(brand)

                    for idx, brand in enumerate(brand_order):
                        block.append(f"({brand})")
                        all_items = []
                        for typ in sorted(cat_types):
                            if brand in grouped[cat][typ]:
                                all_items.extend(grouped[cat][typ][brand])
                        # 같은 제품명 + 같은 부가정보(이동 정보 등)는 수량을 합쳐서 한 줄로 표시
                        # (예: 같은 제품을 결제 1개 + 서비스 1개로 나눠 등록해도 "제품명 2"로 합산됨)
                        merged_qty = {}
                        merged_order = []
                        for product, qty, extra in all_items:
                            key = (product, extra)
                            if key not in merged_qty:
                                merged_qty[key] = 0
                                merged_order.append(key)
                            merged_qty[key] += qty
                        for product, extra in sorted(merged_order, key=lambda x: x[0]):
                            qty = merged_qty[(product, extra)]
                            if qty == 0:
                                continue
                            if extra:
                                block.append(f"{product} {qty}{extra}")
                            else:
                                block.append(f"{product} {qty}")
                        if idx != len(brand_order) - 1:
                            block.append("")
                    blocks.append(block)
                return blocks

            lines = []
            lines.append(f"{date_display} {store_name} 입출고 목록")
            lines.append(f"{time_label} 입출고 내역")
            lines.append("-" * 26)

            lines.append("[출고]")
            out_blocks = build_section(['판매출고', '이동출고'])
            if out_blocks:
                for i, block in enumerate(out_blocks):
                    if i > 0:
                        lines.append("-" * 26)
                    lines.extend(block)
            else:
                lines.append("(출고 내역 없음)")

            lines.append("=" * 15)

            lines.append("[입고]")
            in_blocks = build_section(['입고', '이동입고'])
            if in_blocks:
                for i, block in enumerate(in_blocks):
                    if i > 0:
                        lines.append("-" * 26)
                    lines.extend(block)
            else:
                lines.append("(입고 내역 없음)")

            return "\n".join(lines)

        morning_text = build_report(morning_rows, "4시")
        afternoon_text = build_report(afternoon_rows, "마감")

        return jsonify({
            "store_name": store_name,
            "date": date_display,
            "morning": morning_text,
            "afternoon": afternoon_text
        })
    except Exception as e:
        print(f"❌ 금일 판매 목록 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "데이터를 불러오는 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 발주 추천
# ---------------------------------------------------------------------------

@app.route("/api/recommend_order")
def api_recommend_order():
    conn = get_db()
    cur = g.cursor
    try:
        store_id = request.args.get("store_id")
        days = int(request.args.get("days", 14))
        target_days = int(request.args.get("target_days", 7))
        category_id = request.args.get("category_id")
        brand_id = request.args.get("brand_id")
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
        sort_by = request.args.get("sort_by", "recommend_qty")
        if sort_by not in ("recommend_qty", "shortage", "total_sold", "current_qty"):
            sort_by = "recommend_qty"

        filters = []
        stock_params = []

        if store_id and store_id != '':
            filters.append("ss.store_id = %s")
            stock_params.append(int(store_id))
        if category_id and category_id != '':
            filters.append("p.category_id = %s")
            stock_params.append(int(category_id))
        if brand_id and brand_id != '':
            filters.append("p.brand_id = %s")
            stock_params.append(int(brand_id))

        filter_clause = " AND " + " AND ".join(filters) if filters else ""

        stock_sql = f"""
            SELECT
                p.id as product_id,
                COALESCE(SUM(ss.qty), 0) as current_qty,
                COALESCE(SUM(ss.min_qty), 0) as min_qty
            FROM products p
            LEFT JOIN store_stock ss ON ss.product_id = p.id
            WHERE p.is_active = 1
            {filter_clause}
            GROUP BY p.id
        """

        sale_params = [days]
        sale_where = "type IN ('판매출고', '판매취소') AND date(date_time) >= CURRENT_DATE - INTERVAL '%s days'"
        if store_id and store_id != '':
            sale_where += " AND store_id = %s"
            sale_params.append(int(store_id))
        sale_where = sale_where.replace('%s', '%s')  # 첫 번째 파라미터를 days로 사용

        sale_sql = f"""
            SELECT
                product_id,
                COALESCE(SUM(CASE WHEN type = '판매출고' THEN quantity ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = '판매취소' THEN quantity ELSE 0 END), 0) as total_sold
            FROM stock_transactions
            WHERE {sale_where}
            GROUP BY product_id
        """

        main_sql = f"""
        WITH stock AS ({stock_sql}),
             sales AS ({sale_sql})
        SELECT
            p.id as product_id,
            p.name as product_name,
            b.name as brand_name,
            b.color as brand_color,
            c.name as category_name,
            c.color as category_color,
            p.cost_price,
            p.sale_price,
            COALESCE(s.current_qty, 0) as current_qty,
            COALESCE(s.min_qty, 0) as min_qty,
            COALESCE(sales.total_sold, 0) as total_sold
        FROM products p
        LEFT JOIN brands b ON b.id = p.brand_id
        LEFT JOIN categories c ON c.id = p.category_id
        INNER JOIN stock s ON s.product_id = p.id
        LEFT JOIN sales ON sales.product_id = p.id
        WHERE p.is_active = 1
        """

        def build_item(d):
            total_sold = d["total_sold"] or 0
            current_qty = d["current_qty"] or 0
            avg_daily = round(total_sold / days, 1) if days > 0 else 0
            expected_demand = round(total_sold / days * target_days) if days > 0 else 0
            shortage = max(0, expected_demand - current_qty)
            recommend_order = int(shortage * 1.1) + 1 if shortage > 0 else 0
            return {
                "product_id": d["product_id"],
                "product_name": d["product_name"],
                "brand_name": d["brand_name"],
                "brand_color": d["brand_color"],
                "category_name": d["category_name"],
                "category_color": d["category_color"],
                "cost_price": d["cost_price"],
                "sale_price": d["sale_price"],
                "current_qty": current_qty,
                "min_qty": d["min_qty"],
                "total_sold": total_sold,
                "avg_daily_sales": avg_daily,
                "expected_demand": expected_demand,
                "shortage": shortage,
                "recommend_qty": recommend_order
            }

        # 정렬 기준에 따라 파이썬에서 정확히 계산된 값(추천 발주량/부족량 등)으로 정렬해야
        # target_days 같은 화면 옵션이 그대로 반영된다. 그래서 페이지네이션 전에 전체를 한 번에
        # 가져와 정렬한 뒤, 필요한 페이지만 잘라서 응답한다.
        cur.execute(main_sql, stock_params + sale_params)
        all_rows = cur.fetchall()
        all_items = [build_item(dict(r)) for r in all_rows]

        if sort_by == "shortage":
            all_items.sort(key=lambda x: x["shortage"], reverse=True)
        elif sort_by == "total_sold":
            all_items.sort(key=lambda x: x["total_sold"], reverse=True)
        elif sort_by == "current_qty":
            all_items.sort(key=lambda x: x["current_qty"])
        else:  # recommend_qty
            all_items.sort(key=lambda x: x["recommend_qty"], reverse=True)

        total = len(all_items)
        result = all_items[offset:offset + limit]

        total_recommend_qty = sum(it["recommend_qty"] for it in all_items)
        total_recommend_cost = sum(it["recommend_qty"] * (it["cost_price"] or 0) for it in all_items)

        cur.execute("SELECT value FROM settings WHERE key = %s", ("order_budget",))
        budget_row = cur.fetchone()
        try:
            order_budget = float(budget_row["value"]) if budget_row and budget_row["value"] else 0
        except (TypeError, ValueError):
            order_budget = 0

        return jsonify({
            "items": result,
            "total": total,
            "limit": limit,
            "offset": offset,
            "total_recommend_qty": total_recommend_qty,
            "total_recommend_cost": total_recommend_cost,
            "order_budget": order_budget,
            "order_budget_remaining": order_budget - total_recommend_cost
        })

    except Exception as e:
        print(f"❌ 발주 추천 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "items": [],
            "total": 0,
            "limit": 20,
            "offset": 0,
            "error": str(e)
        }), 500


# ---------------------------------------------------------------------------
# API - 출고 예측
# ---------------------------------------------------------------------------

@app.route("/api/forecast")
def api_forecast():
    conn = get_db()
    cur = g.cursor
    try:
        days = int(request.args.get("days", 90))
        store_id = request.args.get("store_id")
        search_q = request.args.get("q", "").strip()
        category_id = request.args.get("category_id")
        brand_id = request.args.get("brand_id")
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
        sort_by = request.args.get("sort_by", "shortage")
        if sort_by not in ("shortage", "total_sold", "current_qty", "recommend_qty"):
            sort_by = "shortage"

        stock_where = ["p.is_active = 1"]
        stock_params = []
        if store_id and store_id != '':
            stock_where.append("ss.store_id = %s")
            stock_params.append(int(store_id))
        stock_sql = f"""
            SELECT
                p.id as product_id,
                COALESCE(SUM(ss.qty), 0) as current_qty,
                COALESCE(SUM(ss.min_qty), 0) as min_qty
            FROM products p
            LEFT JOIN store_stock ss ON ss.product_id = p.id
            WHERE {" AND ".join(stock_where)}
            GROUP BY p.id
        """

        sale_params = [days]
        sale_where = "type IN ('판매출고', '판매취소') AND date(date_time) >= CURRENT_DATE - INTERVAL '%s days'"
        if store_id and store_id != '':
            sale_where += " AND store_id = %s"
            sale_params.append(int(store_id))
        sale_where = sale_where.replace('%s', '%s')

        sale_sql = f"""
            SELECT
                product_id,
                COALESCE(SUM(CASE WHEN type = '판매출고' THEN quantity ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = '판매취소' THEN quantity ELSE 0 END), 0) as total_sold
            FROM stock_transactions
            WHERE {sale_where}
            GROUP BY product_id
        """

        main_where = ["p.is_active = 1"]
        main_params = []
        if category_id and category_id != '':
            main_where.append("p.category_id = %s")
            main_params.append(int(category_id))
        if brand_id and brand_id != '':
            main_where.append("p.brand_id = %s")
            main_params.append(int(brand_id))
        if search_q:
            search_q_norm = normalize_search(search_q)
            main_where.append("""(
                REPLACE(p.name, ' ', '') ILIKE %s
                OR REPLACE(COALESCE(b.name, ''), ' ', '') ILIKE %s
            )""")
            main_params.append(f"%{search_q_norm}%")
            main_params.append(f"%{search_q_norm}%")

        main_sql = f"""
            SELECT
                p.id as product_id,
                p.name as product_name,
                b.name as brand_name,
                b.color as brand_color,
                p.cost_price,
                p.sale_price,
                COALESCE(s.current_qty, 0) as current_qty,
                COALESCE(s.min_qty, 0) as min_qty,
                COALESCE(sales.total_sold, 0) as total_sold
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN ({stock_sql}) s ON s.product_id = p.id
            LEFT JOIN ({sale_sql}) sales ON sales.product_id = p.id
            WHERE {" AND ".join(main_where)}
        """

        params = main_params + stock_params + sale_params
        cur.execute(main_sql, params)
        rows = cur.fetchall()
        all_items = []
        for r in rows:
            d = dict(r)
            total_sold = d["total_sold"]
            current_qty = d["current_qty"]
            avg_daily = round(total_sold / days, 1) if days > 0 else 0
            shortage = max(0, total_sold - current_qty)
            recommend_order = int(shortage * 1.1) + 1 if shortage > 0 else 0

            all_items.append({
                "product_id": d["product_id"],
                "product_name": d["product_name"],
                "brand_name": d["brand_name"],
                "brand_color": d["brand_color"],
                "cost_price": d["cost_price"],
                "sale_price": d["sale_price"],
                "current_qty": current_qty,
                "min_qty": d["min_qty"],
                "total_sold": total_sold,
                "avg_daily_sales": avg_daily,
                "expected_demand": total_sold,
                "shortage": shortage,
                "recommend_qty": recommend_order
            })

        if sort_by == "total_sold":
            all_items.sort(key=lambda x: x["total_sold"], reverse=True)
        elif sort_by == "current_qty":
            all_items.sort(key=lambda x: x["current_qty"])
        elif sort_by == "recommend_qty":
            all_items.sort(key=lambda x: x["recommend_qty"], reverse=True)
        else:  # shortage
            all_items.sort(key=lambda x: x["shortage"], reverse=True)

        total = len(all_items)
        result = all_items[offset:offset + limit]

        return jsonify({
            "items": result,
            "total": total,
            "limit": limit,
            "offset": offset
        })
    except Exception as e:
        print(f"❌ 출고 예측 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "items": [],
            "total": 0,
            "limit": 20,
            "offset": 0,
            "error": str(e)
        }), 500


# ---------------------------------------------------------------------------
# API - 근무자 출근 기록
# ---------------------------------------------------------------------------

@app.route("/api/attendance", methods=["GET", "POST"])
def api_attendance():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        store_id = request.args.get("store_id")
        date = request.args.get("date")
        if not store_id:
            return jsonify({"error": "매장을 선택해주세요."}), 400
        try:
            sql = "SELECT * FROM attendance WHERE store_id = %s"
            params = [store_id]
            if date:
                sql += " AND date = %s"
                params.append(date)
            sql += " ORDER BY date DESC, staff_name"
            cur.execute(sql, params)
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            print(f"⚠️ 출근 기록 조회 오류: {e}")
            return jsonify([])

    data = request.get_json(force=True)
    store_id = data.get("store_id")
    staff_name = data.get("staff_name")
    date = data.get("date")
    check_in = data.get("check_in")
    check_out = data.get("check_out")

    if not store_id or not staff_name or not date:
        return jsonify({"error": "매장, 이름, 날짜는 필수입니다."}), 400

    try:
        cur.execute("SELECT * FROM attendance WHERE store_id = %s AND staff_name = %s AND date = %s", (store_id, staff_name, date))
        existing = cur.fetchone()
        if existing:
            if check_in is not None:
                cur.execute("UPDATE attendance SET check_in = %s WHERE store_id = %s AND staff_name = %s AND date = %s", (check_in, store_id, staff_name, date))
            if check_out is not None:
                cur.execute("UPDATE attendance SET check_out = %s WHERE store_id = %s AND staff_name = %s AND date = %s", (check_out, store_id, staff_name, date))
        else:
            cur.execute("INSERT INTO attendance (store_id, staff_name, date, check_in, check_out) VALUES (%s, %s, %s, %s, %s)", (store_id, staff_name, date, check_in, check_out))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 출근 기록 추가/수정 오류: {e}")
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500

@app.route("/api/attendance/<int:aid>", methods=["PUT", "DELETE"])
def api_attendance_detail(aid):
    conn = get_db()
    cur = g.cursor
    if request.method == "DELETE":
        try:
            cur.execute("DELETE FROM attendance WHERE id = %s", (aid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            print(f"❌ 출근 기록 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500

    data = request.get_json(force=True)
    check_in = data.get("check_in")
    check_out = data.get("check_out")
    try:
        if check_in is not None:
            cur.execute("UPDATE attendance SET check_in = %s WHERE id = %s", (check_in, aid))
        if check_out is not None:
            cur.execute("UPDATE attendance SET check_out = %s WHERE id = %s", (check_out, aid))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ 출근 기록 수정 오류: {e}")
        return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 판매 등록 (Sales)
# ---------------------------------------------------------------------------

@app.route("/api/sales", methods=["POST"])
def api_sales():
    conn = get_db()
    cur = g.cursor
    data = request.get_json(force=True)
    store_id = data.get("store_id")
    payment_method = data.get("payment_method")
    staff = data.get("staff") or ""
    memo = data.get("memo") or None
    items = data.get("items") or []

    if not store_id or payment_method not in ["현금", "카드", "계좌이체"]:
        return jsonify({"error": "매장과 결제수단을 확인해주세요."}), 400
    if not items:
        return jsonify({"error": "장바구니가 비어있습니다."}), 400

    for item in items:
        pid = item.get("product_id")
        qty = int(item.get("quantity") or 0)
        unit_price_override = item.get("unit_price")
        if qty <= 0:
            return jsonify({"error": "수량은 1 이상이어야 합니다."}), 400
        cur.execute("SELECT qty FROM store_stock WHERE store_id=%s AND product_id=%s", (store_id, pid))
        stock = cur.fetchone()
        current_qty = stock["qty"] if stock else 0
        if qty > current_qty:
            cur.execute("SELECT name FROM products WHERE id=%s", (pid,))
            product = cur.fetchone()
            pname = product["name"] if product else f"#{pid}"
            return jsonify({"error": f"'{pname}' 재고가 부족합니다. (현재 재고 {current_qty}, 요청 {qty})"}), 400

    created_ids = []
    for item in items:
        pid = item.get("product_id")
        qty = int(item.get("quantity") or 0)
        unit_price_override = item.get("unit_price")
        cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
        product = cur.fetchone()
        _apply_stock_delta(conn, store_id, pid, "판매출고", qty)

        if unit_price_override is not None and unit_price_override >= 0:
            unit_price = int(unit_price_override)
        else:
            unit_price = product["sale_price"] or 0

        cur.execute(
            """INSERT INTO stock_transactions
            (product_id, store_id, type, quantity, unit_cost, unit_price, payment_method, staff, memo)
            VALUES (%s, %s, '판매출고', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (pid, store_id, qty, product["cost_price"], unit_price,
             payment_method, staff, memo)
        )
        new_id = cur.fetchone()["id"]
        created_ids.append(new_id)
    conn.commit()
    return jsonify({"ok": True, "transaction_ids": created_ids})


# ---------------------------------------------------------------------------
# API - 판매 실적
# ---------------------------------------------------------------------------

@app.route("/api/performance")
def api_performance():
    conn = get_db()
    cur = g.cursor
    try:
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        sort = request.args.get("sort", "qty")
        order = request.args.get("order", "desc")

        sql = """
        SELECT
            p.id as product_id,
            p.name as product_name,
            b.name as brand_name,
            b.color as brand_color,
            c.name as category_name,
            c.color as category_color,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                     WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) as sold_qty,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                     WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END) as revenue,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                     WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END) as profit
        FROM stock_transactions t
        JOIN products p ON p.id = t.product_id
        LEFT JOIN brands b ON b.id = p.brand_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE t.type IN ('판매출고', '판매취소', '선결예약')
          AND ((t.memo NOT LIKE %s AND t.memo NOT LIKE %s) OR t.memo IS NULL)
        """
        # 주의: memo LIKE 패턴은 반드시 파라미터(%s)로 넘겨야 한다.
        # SQL 문자열 안에 '%이동%'처럼 % 를 직접 박아 넣으면, psycopg2가 뒤에서
        # 전달하는 다른 파라미터(start_date/end_date)와 substitution을 하다가
        # 예외가 나고, 이 함수는 그 예외를 그대로 삼켜 빈 배열을 돌려주기 때문에
        # 화면에는 그냥 "실적 없음"으로만 보인다.
        params = ["%이동%", "%교환%"]
        if start_date:
            sql += " AND date(t.date_time) >= date(%s)"
            params.append(start_date)
        if end_date:
            sql += " AND date(t.date_time) <= date(%s)"
            params.append(end_date)
        sql += " GROUP BY p.id, p.name, b.name, b.color, c.name, c.color HAVING SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) != 0"

        sort_col = "profit" if sort == "profit" else ("revenue" if sort == "revenue" else "sold_qty")
        order_sql = "DESC" if order.lower() == "desc" else "ASC"
        sql += f" ORDER BY {sort_col} {order_sql}"

        cur.execute(sql, params)
        rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"❌ 판매 실적 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# API - 매출 통계
# ---------------------------------------------------------------------------

@app.route("/api/statistics")
def api_statistics():
    conn = get_db()
    cur = g.cursor
    try:
        period = request.args.get("period", "day")
        if period not in ["day", "week", "month", "year"]:
            period = "day"
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        if not end_date:
            end_date = now_kst().strftime("%Y-%m-%d")
        if not start_date:
            if period == "day":
                start_date = (now_kst() - timedelta(days=14)).strftime("%Y-%m-%d")
            elif period == "week":
                start_date = (now_kst() - timedelta(days=56)).strftime("%Y-%m-%d")
            elif period == "month":
                start_date = (now_kst() - timedelta(days=365)).strftime("%Y-%m-%d")
            else:
                start_date = (now_kst() - timedelta(days=365*5)).strftime("%Y-%m-%d")

        fmt_map = {
            "day": "YYYY-MM-DD",
            "week": "YYYY-WW",
            "month": "YYYY-MM",
            "year": "YYYY"
        }
        fmt = fmt_map[period]
        sql = f"""
        SELECT
            to_char(date_time, '{fmt}') as period_key,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0)
                     WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) ELSE 0 END) as sold_qty,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                     WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END) as revenue,
            SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                     WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END) as profit
        FROM stock_transactions t
        WHERE t.type IN ('판매출고', '판매취소', '선결예약')
          AND date(t.date_time) >= date(%s)
          AND date(t.date_time) <= date(%s)
        GROUP BY period_key
        ORDER BY period_key ASC
        """
        cur.execute(sql, (start_date, end_date))
        rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"❌ 매출 통계 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# API - 대시보드
# ---------------------------------------------------------------------------

def get_settings_cache():
    if not hasattr(g, '_settings_cache'):
        cur = g.cursor
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
        g._settings_cache = {r["key"]: r["value"] for r in rows}
    return g._settings_cache

@app.route("/api/dashboard")
def api_dashboard():
    try:
        conn = get_db()
        cur = g.cursor
        today = now_kst().date()
        month_start = today.replace(day=1)
        if month_start.month == 1:
            last_month_start = today.replace(year=today.year-1, month=12, day=1)
        else:
            last_month_start = today.replace(month=today.month-1, day=1)
        last_month_today = last_month_start.replace(day=min(today.day, 28))

        store_id = request.args.get("store_id")
        if store_id:
            try:
                store_id = int(store_id)
            except:
                store_id = None

        settings = get_settings_cache()
        monthly_target = int(settings.get('monthly_target_revenue', 0)) if settings else 0

        import calendar
        total_days = calendar.monthrange(today.year, today.month)[1]
        days_elapsed = today.day

        default_response = {
            "today": {"qty": 0, "revenue": 0, "profit": 0},
            "month": {"qty": 0, "revenue": 0, "profit": 0},
            "stock_warning": [],
            "recent_transactions": [],
            "category_sales": [],
            "stock_value": {"total_cost_value": 0, "total_sale_value": 0, "total_profit_potential": 0},
            "category_comparison": {
                "this_month_quantity": {},
                "last_month_quantity": {},
                "this_month_brand_quantity": {},
                "last_month_brand_quantity": {},
                "this_month_start": month_start.strftime("%Y-%m-%d"),
                "this_month_end": today.strftime("%Y-%m-%d"),
                "last_month_start": last_month_start.strftime("%Y-%m-%d"),
                "last_month_end": last_month_today.strftime("%Y-%m-%d")
            },
            "forecast": {
                "total_days": 0,
                "days_elapsed": 0,
                "current_revenue": 0,
                "forecast_revenue": 0,
                "current_profit": 0,
                "forecast_profit": 0
            },
            "monthly_target": {
                "target": monthly_target,
                "total_days": total_days,
                "days_elapsed": days_elapsed,
                "remaining_days": total_days - days_elapsed,
                "current_revenue": 0,
                "daily_avg_needed": 0,
                "remaining_amount": 0,
                "progress_percent": 0
            }
        }

        # ---------- 오늘 매출 ----------
        try:
            today_q = """
                SELECT
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                                  WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END), 0) as qty,
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                                  WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END), 0) as revenue,
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                                  WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END), 0) as profit
                FROM stock_transactions t
                WHERE t.type IN ('판매출고', '판매취소', '선결예약')
                  AND date(t.date_time) = date(%s)
            """
            params = [today.strftime("%Y-%m-%d")]
            if store_id:
                today_q += " AND t.store_id = %s"
                params.append(store_id)
            cur.execute(today_q, params)
            today_sales = cur.fetchone()
            default_response["today"] = dict(today_sales) if today_sales else default_response["today"]

            if store_id:
                cur.execute("SELECT override_amount FROM daily_revenue_override WHERE store_id = %s AND target_date = %s", (store_id, today.strftime("%Y-%m-%d")))
                override = cur.fetchone()
                if override and override["override_amount"] is not None:
                    default_response["today"]["revenue"] = override["override_amount"]
        except Exception as e:
            print(f"❌ 오늘 매출 조회 오류: {e}")

        # ---------- 이번달 매출 ----------
        try:
            month_q = """
                SELECT
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                                  WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END), 0) as qty,
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                                  WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END), 0) as revenue,
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                                  WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END), 0) as profit
                FROM stock_transactions t
                WHERE t.type IN ('판매출고', '판매취소', '선결예약')
                  AND date(t.date_time) >= date(%s)
            """
            params = [month_start.strftime("%Y-%m-%d")]
            if store_id:
                month_q += " AND t.store_id = %s"
                params.append(store_id)
            cur.execute(month_q, params)
            month_sales = cur.fetchone()
            month_revenue = month_sales["revenue"] if month_sales else 0
            default_response["month"] = dict(month_sales) if month_sales else default_response["month"]

            if store_id:
                cur.execute("SELECT target_date, override_amount FROM daily_revenue_override WHERE store_id = %s AND target_date >= %s AND target_date <= %s", (store_id, month_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")))
                overrides = cur.fetchall()
                override_total = sum(o["override_amount"] for o in overrides)
                if override_total > 0:
                    default_response["month"]["revenue"] = override_total
                    if default_response["month"]["revenue"] > 0 and default_response["month"]["profit"] > 0:
                        ratio = default_response["month"]["profit"] / default_response["month"]["revenue"]
                        default_response["month"]["profit"] = int(override_total * ratio)
                    else:
                        default_response["month"]["profit"] = 0

            if monthly_target > 0:
                remaining = monthly_target - month_revenue
                daily_avg_needed = remaining / (total_days - days_elapsed) if (total_days - days_elapsed) > 0 else 0
                progress = (month_revenue / monthly_target * 100) if monthly_target > 0 else 0
                default_response["monthly_target"].update({
                    "current_revenue": month_revenue,
                    "remaining_amount": remaining if remaining > 0 else 0,
                    "daily_avg_needed": daily_avg_needed if daily_avg_needed > 0 else 0,
                    "progress_percent": min(progress, 100),
                    "is_achieved": month_revenue >= monthly_target
                })
        except Exception as e:
            print(f"❌ 이번달 매출 조회 오류: {e}")

        # ---------- 재고부족 ----------
        try:
            warning_q = """
                SELECT p.id, p.name, ss.qty, ss.min_qty, c.name as category_name, c.color as category_color
                FROM store_stock ss
                JOIN products p ON p.id = ss.product_id
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE p.is_active = 1 AND ss.qty <= ss.min_qty AND ss.min_qty > 0
            """
            params = []
            if store_id:
                warning_q += " AND ss.store_id = %s"
                params.append(store_id)
            warning_q += " ORDER BY (ss.qty / NULLIF(ss.min_qty, 0)) ASC LIMIT 10"
            cur.execute(warning_q, params)
            stock_warning = cur.fetchall()
            default_response["stock_warning"] = [dict(r) for r in stock_warning]
        except Exception as e:
            print(f"❌ 재고부족 조회 오류: {e}")

        # ---------- 최근 입출고 ----------
        try:
            recent_q = """
                SELECT t.*, p.name as product_name, s.name as store_name
                FROM stock_transactions t
                JOIN products p ON p.id = t.product_id
                JOIN stores s ON s.id = t.store_id
                WHERE 1=1
            """
            params = []
            if store_id:
                recent_q += " AND t.store_id = %s"
                params.append(store_id)
            recent_q += " ORDER BY t.date_time DESC LIMIT 5"
            cur.execute(recent_q, params)
            recent_trans = cur.fetchall()
            default_response["recent_transactions"] = [dict(r) for r in recent_trans]
        except Exception as e:
            print(f"❌ 최근 거래 조회 오류: {e}")

        # ---------- 카테고리 매출 비중 ----------
        try:
            cat_q = """
                SELECT
                    c.name as category_name,
                    c.color as category_color,
                    COALESCE(SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                                  WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END), 0) as revenue
                FROM stock_transactions t
                JOIN products p ON p.id = t.product_id
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE t.type IN ('판매출고', '판매취소', '선결예약')
                  AND date(t.date_time) >= date(%s)
            """
            params = [month_start.strftime("%Y-%m-%d")]
            if store_id:
                cat_q += " AND t.store_id = %s"
                params.append(store_id)
            cat_q += " GROUP BY c.id ORDER BY revenue DESC"
            cur.execute(cat_q, params)
            category_sales = cur.fetchall()
            cat_list = [dict(r) for r in category_sales]
            total_revenue = sum(r["revenue"] for r in cat_list)
            for r in cat_list:
                r["percent"] = round(r["revenue"] / total_revenue * 100, 1) if total_revenue > 0 else 0
            default_response["category_sales"] = cat_list
        except Exception as e:
            print(f"❌ 카테고리 매출 조회 오류: {e}")

        # ---------- 재고 가치 ----------
        try:
            stock_q = """
                SELECT
                    COALESCE(SUM(ss.qty * p.cost_price), 0) as total_cost_value,
                    COALESCE(SUM(ss.qty * p.sale_price), 0) as total_sale_value,
                    COALESCE(SUM(ss.qty * (p.sale_price - p.cost_price)), 0) as total_profit_potential
                FROM store_stock ss
                JOIN products p ON p.id = ss.product_id
                WHERE p.is_active = 1
            """
            params = []
            if store_id:
                stock_q += " AND ss.store_id = %s"
                params.append(store_id)
            cur.execute(stock_q, params)
            stock_value = cur.fetchone()
            default_response["stock_value"] = dict(stock_value) if stock_value else default_response["stock_value"]
        except Exception as e:
            print(f"❌ 재고 가치 조회 오류: {e}")

        # ---------- 카테고리 비교 ----------
        try:
            def get_category_quantity(start_date, end_date):
                cat_q = """
                    SELECT
                        c.name as category_name,
                        COALESCE(SUM(CASE WHEN t.type='판매출고' THEN t.quantity ELSE 0 END), 0) as quantity
                    FROM stock_transactions t
                    JOIN products p ON p.id = t.product_id
                    LEFT JOIN categories c ON c.id = p.category_id
                    WHERE t.type IN ('판매출고')
                      AND date(t.date_time) >= date(%s)
                      AND date(t.date_time) <= date(%s)
                      AND c.name IN ('일회용', '기기', '액상')
                """
                params = [start_date, end_date]
                if store_id:
                    cat_q += " AND t.store_id = %s"
                    params.append(store_id)
                cat_q += " GROUP BY c.id"
                cur.execute(cat_q, params)
                rows = cur.fetchall()
                return {r["category_name"]: r["quantity"] for r in rows}

            this_month_qty = get_category_quantity(month_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
            last_month_qty = get_category_quantity(last_month_start.strftime("%Y-%m-%d"), last_month_today.strftime("%Y-%m-%d"))
            default_response["category_comparison"]["this_month_quantity"] = this_month_qty
            default_response["category_comparison"]["last_month_quantity"] = last_month_qty

            # ---- 일회용 브랜드별(플릭/엘프바/칠렉스 바이브/카오린) 이번달 vs 저번달 출고 수량 ----
            def get_brand_quantity(start_date, end_date, brand_names):
                placeholders = ",".join(["%s"] * len(brand_names))
                brand_q = f"""
                    SELECT COALESCE(SUM(t.quantity), 0) as quantity
                    FROM stock_transactions t
                    JOIN products p ON p.id = t.product_id
                    JOIN brands b ON b.id = p.brand_id
                    WHERE t.type = '판매출고'
                      AND date(t.date_time) >= date(%s)
                      AND date(t.date_time) <= date(%s)
                      AND b.name IN ({placeholders})
                """
                params = [start_date, end_date] + list(brand_names)
                if store_id:
                    brand_q += " AND t.store_id = %s"
                    params.append(store_id)
                cur.execute(brand_q, params)
                row = cur.fetchone()
                return row["quantity"] if row else 0

            disposable_brand_groups = {
                "플릭": ["플릭 슬림"],
                "엘프바": ["엘프바 25K 아이스킹", "엘프바 조인원"],
                "칠렉스 바이브": ["칠렉스 바이브 킷", "칠렉스 바이브 팟"],
                "카오린": ["카오린"],
            }
            this_month_brand_qty = {}
            last_month_brand_qty = {}
            for label, names in disposable_brand_groups.items():
                this_month_brand_qty[label] = get_brand_quantity(
                    month_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), names)
                last_month_brand_qty[label] = get_brand_quantity(
                    last_month_start.strftime("%Y-%m-%d"), last_month_today.strftime("%Y-%m-%d"), names)
            default_response["category_comparison"]["this_month_brand_quantity"] = this_month_brand_qty
            default_response["category_comparison"]["last_month_brand_quantity"] = last_month_brand_qty

            total_days = calendar.monthrange(today.year, today.month)[1]
            days_elapsed = today.day
            month_total_revenue = default_response["month"]["revenue"]
            forecast_revenue = int(month_total_revenue / days_elapsed * total_days) if days_elapsed > 0 else 0
            month_total_profit = default_response["month"]["profit"]
            forecast_profit = int(month_total_profit / days_elapsed * total_days) if days_elapsed > 0 else 0

            default_response["forecast"] = {
                "total_days": total_days,
                "days_elapsed": days_elapsed,
                "current_revenue": month_total_revenue,
                "forecast_revenue": forecast_revenue,
                "current_profit": month_total_profit,
                "forecast_profit": forecast_profit
            }
        except Exception as e:
            print(f"❌ 카테고리 비교/예상 매출 오류: {e}")

        return jsonify(default_response)
    except Exception as e:
        print(f"❌ 대시보드 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "today": {"qty": 0, "revenue": 0, "profit": 0},
            "month": {"qty": 0, "revenue": 0, "profit": 0},
            "stock_warning": [],
            "recent_transactions": [],
            "category_sales": [],
            "stock_value": {"total_cost_value": 0, "total_sale_value": 0, "total_profit_potential": 0},
            "category_comparison": {"this_month_quantity": {}, "last_month_quantity": {}, "this_month_brand_quantity": {}, "last_month_brand_quantity": {}},
            "forecast": {"total_days": 0, "days_elapsed": 0, "current_revenue": 0, "forecast_revenue": 0, "current_profit": 0, "forecast_profit": 0},
            "monthly_target": {"target": 0, "total_days": 0, "days_elapsed": 0, "remaining_days": 0, "current_revenue": 0, "daily_avg_needed": 0, "remaining_amount": 0, "progress_percent": 0}
        })


# ---------------------------------------------------------------------------
# API - 카테고리 집계
# ---------------------------------------------------------------------------

@app.route("/api/category_stats")
def api_category_stats():
    conn = get_db()
    cur = g.cursor
    try:
        cur.execute("""
            SELECT
                c.id,
                c.name,
                c.color,
                COUNT(p.id) as product_count,
                AVG(p.cost_price) as avg_cost,
                AVG(p.sale_price) as avg_sale,
                COALESCE(SUM(ss.qty * p.cost_price), 0) as stock_value
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id AND p.is_active = 1
            LEFT JOIN store_stock ss ON ss.product_id = p.id
            GROUP BY c.id
            ORDER BY c.id
        """)
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["avg_cost"] = round(d["avg_cost"], 1) if d["avg_cost"] else 0
            d["avg_sale"] = round(d["avg_sale"], 1) if d["avg_sale"] else 0
            result.append(d)
        return jsonify(result)
    except Exception as e:
        print(f"❌ 카테고리 집계 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# API - 고객 관리 (CRM)
# ---------------------------------------------------------------------------

@app.route("/api/customers", methods=["GET"])
def api_customers():
    get_db()
    cur = g.cursor
    try:
        search = normalize_search(request.args.get("search", ""))
        vip_only = request.args.get("vip_only") == "1"

        sql = """
            SELECT c.id, c.name, c.phone, c.address, c.memo, c.is_vip, c.created_at,
                   COUNT(po.id) as order_count,
                   COALESCE(SUM(po.total_amount), 0) as total_spent,
                   MAX(po.created_at) as last_order_at
            FROM customers c
            LEFT JOIN pre_orders po ON po.customer_id = c.id
        """
        where = []
        params = []
        if vip_only:
            where.append("c.is_vip = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY c.id ORDER BY c.is_vip DESC, MAX(po.created_at) DESC NULLS LAST, c.id DESC"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

        if search:
            rows = [r for r in rows
                    if search in normalize_search(r.get("name") or "")
                    or search in normalize_search(r.get("phone") or "")]

        # 3회 이상 구매하거나 VIP로 지정된 고객은 "단골"로 표시
        for r in rows:
            r["is_regular"] = bool(r["is_vip"]) or (r["order_count"] or 0) >= 3
        return jsonify(rows)
    except Exception as e:
        print(f"❌ 고객 목록 조회 오류: {e}")
        return jsonify([])


@app.route("/api/customers/<int:customer_id>", methods=["GET", "PUT", "DELETE"])
def api_customer_detail(customer_id):
    conn = get_db()
    cur = g.cursor

    if request.method == "DELETE":
        try:
            cur.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            conn.rollback()
            print(f"❌ 고객 삭제 오류: {e}")
            return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500

    if request.method == "PUT":
        try:
            data = request.get_json(force=True)
            fields = {}
            for key in ("name", "phone", "address", "memo"):
                if key in data:
                    fields[key] = (data[key] or "").strip() or None
            if "is_vip" in data:
                fields["is_vip"] = 1 if data["is_vip"] else 0
            if not fields:
                return jsonify({"error": "변경할 내용이 없습니다."}), 400
            set_clause = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = CURRENT_TIMESTAMP"
            params = list(fields.values()) + [customer_id]
            cur.execute(f"UPDATE customers SET {set_clause} WHERE id = %s", params)
            conn.commit()
            return jsonify({"ok": True})
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return jsonify({"error": "이미 등록된 연락처입니다."}), 400
        except Exception as e:
            conn.rollback()
            print(f"❌ 고객 수정 오류: {e}")
            return jsonify({"error": "수정 중 오류가 발생했습니다."}), 500

    # GET: 상세 - 구매 이력 + 카테고리별 재구매 주기 예측
    try:
        cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
        customer = cur.fetchone()
        if not customer:
            return jsonify({"error": "고객을 찾을 수 없습니다."}), 404
        customer = dict(customer)

        cur.execute("""
            SELECT po.id, po.created_at, po.status, po.total_amount, po.payment_method,
                   string_agg(p.name || ' x' || poi.quantity, ', ' ORDER BY poi.id) as items_summary,
                   array_agg(DISTINCT c.name) FILTER (WHERE c.name IS NOT NULL) as categories
            FROM pre_orders po
            JOIN pre_order_items poi ON poi.pre_order_id = po.id
            JOIN products p ON p.id = poi.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE po.customer_id = %s
            GROUP BY po.id
            ORDER BY po.created_at DESC
        """, (customer_id,))
        orders = [dict(r) for r in cur.fetchall()]
        customer["orders"] = orders

        # 카테고리별 주문 날짜 목록 -> 평균 재구매 간격(일) -> 다음 예상 구매일
        cur.execute("""
            SELECT c.name as category_name, po.created_at::date as order_date
            FROM pre_orders po
            JOIN pre_order_items poi ON poi.pre_order_id = po.id
            JOIN products p ON p.id = poi.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE po.customer_id = %s AND c.name IS NOT NULL
            GROUP BY c.name, po.created_at::date
            ORDER BY c.name, po.created_at::date
        """, (customer_id,))
        by_category = defaultdict(list)
        for r in cur.fetchall():
            by_category[r["category_name"]].append(r["order_date"])

        predictions = []
        today = now_kst().date()
        for cat_name, dates in by_category.items():
            if len(dates) < 2:
                continue
            intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            avg_interval = round(sum(intervals) / len(intervals))
            if avg_interval <= 0:
                continue
            last_date = dates[-1]
            predicted_next = last_date + timedelta(days=avg_interval)
            days_until = (predicted_next - today).days
            if days_until < 0:
                status = "지남"
            elif days_until <= 3:
                status = "임박"
            else:
                status = "여유"
            predictions.append({
                "category_name": cat_name,
                "avg_interval_days": avg_interval,
                "last_order_date": last_date.isoformat(),
                "predicted_next_date": predicted_next.isoformat(),
                "days_until": days_until,
                "status": status,
            })
        customer["repurchase_predictions"] = predictions

        return jsonify(customer)
    except Exception as e:
        print(f"❌ 고객 상세 조회 오류: {e}")
        return jsonify({"error": "조회 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 재고 실사
# ---------------------------------------------------------------------------

@app.route("/api/stocktake", methods=["GET", "POST"])
def api_stocktake():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        store_id = request.args.get("store_id")
        if not store_id:
            return jsonify({"error": "매장을 선택해주세요."}), 400
        try:
            cur.execute("""
                SELECT p.id as product_id, p.name as product_name,
                       c.name as category_name, b.name as brand_name,
                       ss.qty as system_qty, ss.qty as actual_qty, ss.min_qty,
                       COALESCE(sales.sold_qty, 0) as recent_sold_qty,
                       COALESCE(sales.sold_qty, 0) * p.sale_price as recent_revenue
                FROM products p
                JOIN store_stock ss ON ss.product_id = p.id
                LEFT JOIN categories c ON c.id = p.category_id
                LEFT JOIN brands b ON b.id = p.brand_id
                LEFT JOIN (
                    SELECT product_id,
                           SUM(CASE WHEN type IN ('판매출고', '선결예약') THEN quantity
                                    WHEN type = '판매취소' THEN -quantity ELSE 0 END) as sold_qty
                    FROM stock_transactions
                    WHERE store_id = %s AND type IN ('판매출고', '판매취소', '선결예약')
                          AND date_time >= CURRENT_DATE - INTERVAL '30 days'
                    GROUP BY product_id
                ) sales ON sales.product_id = p.id
                WHERE p.is_active = 1 AND ss.store_id = %s
                ORDER BY COALESCE(c.name, ''), COALESCE(b.name, ''), p.name
            """, (store_id, store_id))
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            print(f"❌ 재고 실사 조회 오류: {e}")
            return jsonify([])

    data = request.get_json(force=True)
    store_id = data.get("store_id")
    items = data.get("items") or []
    staff = data.get("staff") or ""
    if not store_id:
        return jsonify({"error": "매장을 선택해주세요."}), 400
    try:
        result = []
        for item in items:
            product_id = item.get("product_id")
            actual_qty = int(item.get("actual_qty") or 0)
            cur.execute("SELECT qty FROM store_stock WHERE store_id=%s AND product_id=%s", (store_id, product_id))
            stock = cur.fetchone()
            system_qty = stock["qty"] if stock else 0
            if actual_qty != system_qty:
                cur.execute("UPDATE store_stock SET qty=%s WHERE store_id=%s AND product_id=%s", (actual_qty, store_id, product_id))
                cur.execute(
                    """INSERT INTO stock_transactions
                    (product_id, store_id, type, quantity, before_qty, after_qty, staff, memo)
                    VALUES (%s, %s, '실사조정', %s, %s, %s, %s, %s)""",
                    (product_id, store_id, abs(actual_qty - system_qty), system_qty, actual_qty, staff, "재고실사")
                )
                result.append({"product_id": product_id, "system_qty": system_qty, "actual_qty": actual_qty, "diff": actual_qty - system_qty})
        conn.commit()
        return jsonify({"ok": True, "changes": result})
    except Exception as e:
        print(f"❌ 재고 실사 저장 오류: {e}")
        return jsonify({"error": "저장 중 오류가 발생했습니다."}), 500


@app.route("/api/stocktake/export")
def api_stocktake_export():
    get_db()
    cur = g.cursor
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "매장을 선택해주세요."}), 400
    cur.execute("""
        SELECT p.id as product_id, c.name as category_name, b.name as brand_name, p.name as product_name, ss.qty as qty
        FROM products p
        JOIN store_stock ss ON ss.product_id = p.id
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN brands b ON b.id = p.brand_id
        WHERE p.is_active = 1 AND ss.store_id = %s
        ORDER BY COALESCE(c.name, ''), COALESCE(b.name, ''), p.name
    """, (store_id,))
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    # ID 컬럼을 포함시켜, 같은 이름의 제품이 여러 개 있어도(브랜드가 다르거나 등록 실수로
    # 이름이 겹치는 경우) 업로드 시 "제품명"이 아니라 "ID"로 정확히 매칭되도록 한다.
    # (예전에는 제품명만으로 매칭해서, 이름이 겹치는 제품이 있으면 엉뚱한 제품에
    #  같은 수량이 잘못 채워지는 문제가 있었다.)
    writer.writerow(['ID', '카테고리', '브랜드', '제품명', '갯수'])
    for r in rows:
        writer.writerow([r['product_id'], r['category_name'] or '', r['brand_name'] or '', r['product_name'], r['qty']])
    output.seek(0)
    resp = send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='실사표.csv')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


# ---------------------------------------------------------------------------
# API - 카테고리/브랜드별 재고회전율
# ---------------------------------------------------------------------------

@app.route("/api/turnover_rate")
def api_turnover_rate():
    get_db()
    cur = g.cursor
    try:
        group_by = request.args.get("group_by", "category")
        if group_by not in ("category", "brand", "product"):
            group_by = "category"
        days = int(request.args.get("days", 30))
        store_id = request.args.get("store_id")
        sort_by = request.args.get("sort_by", "turnover")
        if sort_by not in ("turnover", "revenue", "profit"):
            sort_by = "turnover"

        stock_where = "p.is_active = 1"
        stock_params = []
        if store_id:
            stock_where += " AND ss.store_id = %s"
            stock_params.append(int(store_id))

        sale_where = "t.type IN ('판매출고', '판매취소', '선결예약') AND t.date_time >= CURRENT_DATE - INTERVAL '%s days'"
        sale_params = [days]
        if store_id:
            sale_where += " AND t.store_id = %s"
            sale_params.append(int(store_id))

        if group_by == "category":
            group_col, group_color, group_key = "c.name", "c.color", "stock.category_id"
        elif group_by == "brand":
            group_col, group_color, group_key = "b.name", "b.color", "stock.brand_id"
        else:  # product
            group_col, group_color, group_key = "stock.product_name", "b.color", "stock.product_id"

        sql = f"""
            WITH stock AS (
                SELECT p.id as product_id, p.name as product_name, p.category_id, p.brand_id,
                       p.sale_price, p.cost_price, SUM(ss.qty) as qty
                FROM products p
                JOIN store_stock ss ON ss.product_id = p.id
                WHERE {stock_where}
                GROUP BY p.id, p.name, p.category_id, p.brand_id, p.sale_price, p.cost_price
            ),
            sales AS (
                SELECT t.product_id,
                    SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                             WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) as sold_qty
                FROM stock_transactions t
                WHERE {sale_where}
                GROUP BY t.product_id
            )
            SELECT {group_col} as group_name, {group_color} as group_color,
                   COALESCE(SUM(stock.qty), 0) as current_qty,
                   COALESCE(SUM(sales.sold_qty), 0) as total_sold,
                   COALESCE(SUM(sales.sold_qty * stock.sale_price), 0) as revenue,
                   COALESCE(SUM(sales.sold_qty * (stock.sale_price - stock.cost_price)), 0) as profit
            FROM stock
            LEFT JOIN sales ON sales.product_id = stock.product_id
            LEFT JOIN categories c ON c.id = stock.category_id
            LEFT JOIN brands b ON b.id = stock.brand_id
            GROUP BY {group_col}, {group_color}, {group_key}
            HAVING {group_col} IS NOT NULL
        """
        cur.execute(sql, stock_params + sale_params)
        rows = cur.fetchall()

        result = []
        for r in rows:
            current_qty = r["current_qty"] or 0
            total_sold = max(r["total_sold"] or 0, 0)
            revenue = max(r["revenue"] or 0, 0)
            profit = max(r["profit"] or 0, 0)
            avg_daily = total_sold / days if days > 0 else 0
            days_to_deplete = round(current_qty / avg_daily, 1) if avg_daily > 0 else None
            turnover_count = round(total_sold / current_qty, 2) if current_qty > 0 else None
            result.append({
                "group_name": r["group_name"],
                "group_color": r["group_color"],
                "current_qty": current_qty,
                "total_sold": total_sold,
                "revenue": revenue,
                "profit": profit,
                "avg_daily_sales": round(avg_daily, 2),
                "days_to_deplete": days_to_deplete,
                "turnover_count": turnover_count,
            })

        if sort_by == "revenue":
            result.sort(key=lambda x: x["revenue"], reverse=True)
        elif sort_by == "profit":
            result.sort(key=lambda x: x["profit"], reverse=True)
        else:  # turnover: 회전 횟수 높은 순 (없는 항목은 맨 뒤로)
            result.sort(key=lambda x: (x["turnover_count"] is None, -(x["turnover_count"] or 0)))
        return jsonify(result)
    except Exception as e:
        print(f"❌ 재고회전율 조회 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# API - 백업
# ---------------------------------------------------------------------------

@app.route("/api/backup", methods=["GET", "POST"])
def api_backup():
    if request.method == "GET":
        backups = []
        if os.path.exists(BACKUP_DIR):
            for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
                if f.endswith(".db"):
                    stat = os.stat(os.path.join(BACKUP_DIR, f))
                    backups.append({"name": f, "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
        return jsonify(backups)
    return jsonify({"ok": True, "message": "PostgreSQL은 Aiven이 자동 백업합니다."})


# ---------------------------------------------------------------------------
# API - 엑셀 다운로드 (Export)
# ---------------------------------------------------------------------------

@app.route("/api/export/<path:store_id>/products")
def api_export_products(store_id):
    conn = get_db()
    cur = g.cursor
    # 화면(제품 관리 목록)은 기본적으로 활성 제품만 보여주므로, 다운로드도 같은 기준을 따라야
    # "화면에 보이는 재고"와 "다운로드한 재고"가 일치한다. show_inactive=1을 넘기면 비활성(단종)
    # 제품도 포함해서 내려준다 (화면의 "비활성 포함" 체크박스와 동일한 동작).
    show_inactive = request.args.get("show_inactive", "0") == "1"
    active_filter = "" if show_inactive else "WHERE p.is_active = 1"
    active_filter_and = "" if show_inactive else "AND p.is_active = 1"

    if store_id == 'all' or store_id == '':
        cur.execute(f"""
            SELECT p.id, p.name, b.name as brand_name, c.name as category_name, p.cost_price, p.card_cost_price, p.sale_price,
                   COALESCE(SUM(ss.qty), 0) as qty,
                   COALESCE(SUM(ss.min_qty), 0) as min_qty,
                   CASE WHEN p.is_active THEN '활성' ELSE '비활성' END as status
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN store_stock ss ON ss.product_id = p.id
            {active_filter}
            GROUP BY p.id, p.name, b.name, c.name, p.cost_price, p.card_cost_price, p.sale_price, p.is_active
            ORDER BY p.id
        """)
    else:
        try:
            store_id_int = int(store_id)
        except:
            return jsonify({"error": "올바른 매장 ID가 아닙니다."}), 400
        cur.execute(f"""
            SELECT p.id, p.name, b.name as brand_name, c.name as category_name, p.cost_price, p.card_cost_price, p.sale_price,
                   ss.qty, ss.min_qty,
                   CASE WHEN p.is_active THEN '활성' ELSE '비활성' END as status
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            JOIN store_stock ss ON ss.product_id = p.id
            WHERE ss.store_id = %s {active_filter_and}
            ORDER BY p.id
        """, (store_id_int,))

    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID','제품명','브랜드','카테고리','원가','카드원가','판매가','재고수량','최소재고','상태'])
    for r in rows:
        writer.writerow([r['id'], r['name'], r['brand_name'] or '', r['category_name'] or '', r['cost_price'], r['card_cost_price'], r['sale_price'], r['qty'], r['min_qty'], r['status']])
    output.seek(0)
    resp = send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='재고목록.csv')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp

@app.route("/api/export/transactions")
def api_export_transactions():
    conn = get_db()
    cur = g.cursor
    cur.execute("""
        SELECT t.date_time, p.name as product_name, s.name as store_name, t.type, t.quantity, t.staff, t.memo
        FROM stock_transactions t
        JOIN products p ON p.id = t.product_id
        JOIN stores s ON s.id = t.store_id
        ORDER BY t.date_time DESC
        LIMIT 1000
    """)
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['일시','제품명','매장','유형','수량','담당자','메모'])
    for r in rows:
        writer.writerow([r['date_time'], r['product_name'], r['store_name'], r['type'], r['quantity'], r['staff'] or '', r['memo'] or ''])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='입출고내역.csv')

@app.route("/api/export/performance")
def api_export_performance():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    conn = get_db()
    cur = g.cursor
    sql = """
        SELECT p.name as product_name, c.name as category_name,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                        WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) as sold_qty,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                        WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END) as revenue,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                        WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END) as profit
        FROM stock_transactions t
        JOIN products p ON p.id = t.product_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE t.type IN ('판매출고', '판매취소', '선결예약')
    """
    params = []
    if start_date:
        sql += " AND date(t.date_time) >= date(%s)"
        params.append(start_date)
    if end_date:
        sql += " AND date(t.date_time) <= date(%s)"
        params.append(end_date)
    sql += """ GROUP BY p.id, p.name, c.name
        HAVING SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                        WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) != 0"""
    cur.execute(sql, params)
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['제품명','카테고리','판매량','순매출','순이익'])
    for r in rows:
        writer.writerow([r['product_name'], r['category_name'] or '', r['sold_qty'], r['revenue'], r['profit']])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='판매실적.csv')

@app.route("/api/export/statistics")
def api_export_statistics():
    period = request.args.get("period", "day")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if period not in ["day", "week", "month", "year"]:
        period = "day"
    if not end_date:
        end_date = now_kst().strftime("%Y-%m-%d")
    if not start_date:
        if period == "day":
            start_date = (now_kst() - timedelta(days=14)).strftime("%Y-%m-%d")
        elif period == "week":
            start_date = (now_kst() - timedelta(days=56)).strftime("%Y-%m-%d")
        elif period == "month":
            start_date = (now_kst() - timedelta(days=365)).strftime("%Y-%m-%d")
        else:
            start_date = (now_kst() - timedelta(days=365*5)).strftime("%Y-%m-%d")

    fmt_map = {
        "day": "YYYY-MM-DD",
        "week": "YYYY-WW",
        "month": "YYYY-MM",
        "year": "YYYY"
    }
    fmt = fmt_map[period]
    conn = get_db()
    cur = g.cursor
    sql = f"""
        SELECT to_char(date_time, '{fmt}') as period_key,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN t.quantity
                        WHEN t.type = '판매취소' THEN -t.quantity ELSE 0 END) as sold_qty,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0)
                        WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END) as revenue,
               SUM(CASE WHEN t.type IN ('판매출고', '선결예약') THEN COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0))
                        WHEN t.type = '판매취소' THEN -COALESCE(t.quantity, 0) * (COALESCE(t.unit_price, 0) - COALESCE(t.unit_cost, 0)) ELSE 0 END) as profit
        FROM stock_transactions t
        WHERE t.type IN ('판매출고', '판매취소', '선결예약')
          AND date(t.date_time) >= date(%s)
          AND date(t.date_time) <= date(%s)
        GROUP BY period_key
        ORDER BY period_key ASC
    """
    cur.execute(sql, (start_date, end_date))
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['기간','판매량','순매출','순이익'])
    for r in rows:
        writer.writerow([r['period_key'], r['sold_qty'], r['revenue'], r['profit']])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='매출통계.csv')


# ---------------------------------------------------------------------------
# API - 엑셀 업로드 (Import Products)
# ---------------------------------------------------------------------------

@app.route("/api/import/products", methods=["POST"])
def api_import_products():
    conn = get_db()
    cur = g.cursor
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "파일이 선택되지 않았습니다."}), 400

    store_id = request.form.get("store_id")
    if store_id:
        try:
            store_id = int(store_id)
            cur.execute("SELECT id FROM stores WHERE id = %s", (store_id,))
            store_exists = cur.fetchone()
            if not store_exists:
                return jsonify({"error": f"매장 ID {store_id}가 존재하지 않습니다."}), 400
        except:
            return jsonify({"error": "올바른 매장 ID가 아닙니다."}), 400

    try:
        content = file.stream.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        results = {"success": 0, "failed": 0, "errors": []}

        def parse_number(val):
            if not val or str(val).strip() == '':
                return 0
            cleaned = str(val).replace(',', '').strip()
            try:
                return int(float(cleaned))
            except:
                return 0

        if store_id is None:
            cur.execute("SELECT id FROM stores ORDER BY id")
            stores = cur.fetchall()
            if not stores:
                cur.execute("INSERT INTO stores (name) VALUES ('본점') RETURNING id")
                new_id = cur.fetchone()["id"]
                conn.commit()
                stores = [{"id": new_id}]
                print("✅ 기본 매장 '본점'이 생성되었습니다.")

        for row_num, row in enumerate(reader, start=2):
            try:
                name = row.get("제품명", "").strip()
                if not name:
                    results["errors"].append(f"{row_num}행: 제품명이 비어있습니다.")
                    results["failed"] += 1
                    continue

                brand_name = row.get("브랜드", "").strip()
                brand_id = None
                if brand_name:
                    cur.execute("SELECT id FROM brands WHERE name = %s", (brand_name,))
                    brand_row = cur.fetchone()
                    if brand_row:
                        brand_id = brand_row["id"]
                    else:
                        cur.execute("INSERT INTO brands (name, status) VALUES (%s, 'pending') RETURNING id", (brand_name,))
                        brand_id = cur.fetchone()["id"]
                        print(f"✅ 새 브랜드 생성: {brand_name} (ID: {brand_id})")

                category_name = row.get("카테고리", "").strip()
                category_id = None
                if category_name:
                    cur.execute("SELECT id FROM categories WHERE name = %s", (category_name,))
                    cat_row = cur.fetchone()
                    if cat_row:
                        category_id = cat_row["id"]
                    else:
                        cur.execute("INSERT INTO categories (name, color) VALUES (%s, '#8a8f98') RETURNING id", (category_name,))
                        category_id = cur.fetchone()["id"]
                        print(f"✅ 새 카테고리 생성: {category_name} (ID: {category_id})")

                cost_price = parse_number(row.get("원가", 0))
                card_cost_price = parse_number(row.get("카드원가", 0))
                sale_price = parse_number(row.get("판매가", 0))
                min_qty = parse_number(row.get("최소재고", 0))
                initial_qty = parse_number(row.get("초기재고", 0))

                product_id = row.get("ID", "").strip()

                if product_id and product_id.isdigit():
                    cur.execute(
                        """UPDATE products SET
                            name=%s, brand_id=%s, category_id=%s, cost_price=%s, card_cost_price=%s,
                            sale_price=%s, updated_at=CURRENT_TIMESTAMP
                           WHERE id=%s AND is_active=1""",
                        (name, brand_id, category_id, cost_price, card_cost_price, sale_price, int(product_id))
                    )
                    if cur.rowcount == 0:
                        results["errors"].append(f"{row_num}행: ID {product_id}를 찾을 수 없습니다.")
                        results["failed"] += 1
                        continue
                    if store_id:
                        cur.execute(
                            """INSERT INTO store_stock (store_id, product_id, qty, min_qty)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT(store_id, product_id) DO UPDATE SET qty = excluded.qty, min_qty = excluded.min_qty""",
                            (store_id, int(product_id), initial_qty, min_qty)
                        )
                    else:
                        for s in stores:
                            cur.execute(
                                """INSERT INTO store_stock (store_id, product_id, qty, min_qty)
                                   VALUES (%s, %s, %s, %s)
                                   ON CONFLICT(store_id, product_id) DO UPDATE SET qty = excluded.qty, min_qty = excluded.min_qty""",
                                (s["id"], int(product_id), initial_qty, min_qty)
                            )
                else:
                    cur.execute(
                        """INSERT INTO products (name, brand_id, category_id, cost_price, card_cost_price, sale_price)
                           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                        (name, brand_id, category_id, cost_price, card_cost_price, sale_price)
                    )
                    product_id = cur.fetchone()["id"]

                    if store_id:
                        cur.execute(
                            """INSERT INTO store_stock (store_id, product_id, qty, min_qty)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT(store_id, product_id) DO UPDATE SET qty = excluded.qty, min_qty = excluded.min_qty""",
                            (store_id, product_id, initial_qty, min_qty)
                        )
                    else:
                        for idx, s in enumerate(stores):
                            qty = initial_qty if idx == 0 else 0
                            cur.execute(
                                """INSERT INTO store_stock (store_id, product_id, qty, min_qty)
                                   VALUES (%s, %s, %s, %s)
                                   ON CONFLICT(store_id, product_id) DO UPDATE SET qty = excluded.qty, min_qty = excluded.min_qty""",
                                (s["id"], product_id, qty, min_qty if idx == 0 else 0)
                            )

                results["success"] += 1

            except psycopg2.IntegrityError as e:
                error_msg = str(e)
                if "FOREIGN KEY" in error_msg:
                    error_msg = f"외래 키 오류: 브랜드나 카테고리가 유효하지 않을 수 있습니다. (행 {row_num})"
                results["errors"].append(f"{row_num}행: {error_msg}")
                results["failed"] += 1
            except Exception as e:
                results["errors"].append(f"{row_num}행: {str(e)}")
                results["failed"] += 1

        conn.commit()
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": f"파일 처리 중 오류: {str(e)}"}), 400

@app.route("/api/import/template")
def api_import_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID','제품명','브랜드','카테고리','원가','카드원가','판매가','최소재고','초기재고'])
    writer.writerow(['','예시: 레몬에이드','네스티','일회용','3000','3300','4500','5','10'])
    writer.writerow(['','예시: 팟','플릭','액상','5000','5500','8000','3','0'])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name='제품_업로드_템플릿.csv')


# ---------------------------------------------------------------------------
# API - 출고 내역 일괄 업로드
# ---------------------------------------------------------------------------

@app.route("/api/import/transactions", methods=["POST"])
def api_import_transactions():
    conn = get_db()
    cur = g.cursor
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "파일이 선택되지 않았습니다."}), 400

    adjust_stock = request.form.get('adjust_stock', 'false') == 'true'

    store_id = request.form.get('store_id')
    if store_id:
        try:
            store_id = int(store_id)
            cur.execute("SELECT id FROM stores WHERE id = %s", (store_id,))
            store_exists = cur.fetchone()
            if not store_exists:
                return jsonify({"error": f"매장 ID {store_id}가 존재하지 않습니다."}), 400
        except:
            return jsonify({"error": "올바른 매장 ID가 아닙니다."}), 400
    else:
        cur.execute("SELECT id FROM stores ORDER BY id LIMIT 1")
        store = cur.fetchone()
        if not store:
            cur.execute("INSERT INTO stores (name) VALUES ('본점') RETURNING id")
            store_id = cur.fetchone()["id"]
            conn.commit()
        else:
            store_id = store["id"]

    try:
        content = file.stream.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        results = {"success": 0, "failed": 0, "errors": [], "rows": []}

        expected_fields = ['일시', '제품명', '브랜드', '카테고리', '수량', '비고']
        actual_fields = [f.strip() for f in reader.fieldnames] if reader.fieldnames else []
        missing = [f for f in expected_fields if f not in actual_fields]
        if missing:
            return jsonify({
                "error": f"필드명이 올바르지 않습니다. 누락된 필드: {', '.join(missing)}. 템플릿을 다운로드하여 사용하세요.",
                "actual_fields": actual_fields,
                "expected_fields": expected_fields
            }), 400

        for row_num, row in enumerate(reader, start=2):
            row_info = {"row": row_num, "status": "success", "message": ""}
            try:
                product_name = row.get("제품명", "").strip()
                brand_name = row.get("브랜드", "").strip()
                category = row.get("카테고리", "").strip()
                qty_str = row.get("수량", "").strip()
                date = row.get("일시", "").strip()
                memo = row.get("비고", "").strip() or ("일괄출고" if adjust_stock else "일괄기록")

                if not product_name:
                    raise ValueError("제품명이 비어있습니다.")
                if not qty_str:
                    raise ValueError("수량이 비어있습니다.")
                try:
                    qty = int(qty_str.replace(',', ''))
                except:
                    raise ValueError(f"수량이 올바르지 않습니다. ('{qty_str}')")
                if qty <= 0:
                    raise ValueError("수량은 1 이상이어야 합니다.")

                if date:
                    try:
                        datetime.strptime(date, "%Y-%m-%d")
                    except:
                        raise ValueError(f"날짜 형식이 올바르지 않습니다. ('{date}') YYYY-MM-DD 형식으로 입력하세요.")
                else:
                    date = now_kst().strftime("%Y-%m-%d")

                if brand_name:
                    cur.execute(
                        """SELECT p.id, p.cost_price, p.sale_price, p.name
                           FROM products p
                           LEFT JOIN brands b ON b.id = p.brand_id
                           WHERE p.name = %s AND b.name = %s""",
                        (product_name, brand_name)
                    )
                    product = cur.fetchone()
                    if not product:
                        cur.execute("SELECT id, cost_price, sale_price, name FROM products WHERE name = %s", (product_name,))
                        product = cur.fetchone()
                else:
                    cur.execute("SELECT id, cost_price, sale_price, name FROM products WHERE name = %s", (product_name,))
                    product = cur.fetchone()

                if not product:
                    brand_id = None
                    category_id = None

                    if brand_name:
                        brand_id = get_brand_id_from_name(brand_name)
                        if not category:
                            cat_id = get_category_id_from_brand(brand_name)
                            if cat_id:
                                category_id = cat_id

                    if not brand_id:
                        brand_list = get_brand_list_from_db()
                        extracted_brand, _ = extract_brand_from_name(product_name, brand_list)
                        if extracted_brand:
                            brand_id = get_brand_id_from_name(extracted_brand)
                            if not category:
                                cat_id = get_category_id_from_brand(extracted_brand)
                                if cat_id:
                                    category_id = cat_id

                    if not category_id and category:
                        cur.execute("SELECT id FROM categories WHERE name = %s", (category,))
                        cat = cur.fetchone()
                        if cat:
                            category_id = cat["id"]
                        else:
                            cur.execute("INSERT INTO categories (name, color) VALUES (%s, '#8a8f98') RETURNING id", (category,))
                            category_id = cur.fetchone()["id"]

                    if not category_id:
                        cur.execute("SELECT id FROM categories WHERE name = '일회용'")
                        default_cat = cur.fetchone()
                        category_id = default_cat["id"] if default_cat else None

                    cur.execute(
                        """INSERT INTO products (name, brand_id, category_id, cost_price, card_cost_price, sale_price, is_active)
                           VALUES (%s, %s, %s, 0, 0, 0, 1) RETURNING id""",
                        (product_name, brand_id, category_id)
                    )
                    product_id = cur.fetchone()["id"]
                    cur.execute("SELECT id FROM stores")
                    stores = cur.fetchall()
                    for s in stores:
                        cur.execute("INSERT INTO store_stock (store_id, product_id, qty, min_qty) VALUES (%s, %s, 0, 0) ON CONFLICT DO NOTHING", (s["id"], product_id))
                    unit_cost = 0
                    unit_price = 0
                else:
                    product_id = product["id"]
                    unit_cost = product["cost_price"] or 0
                    unit_price = product["sale_price"] or 0

                if product_id is None:
                    raise ValueError("제품 생성에 실패했습니다.")

                cur.execute("SELECT qty FROM store_stock WHERE store_id=%s AND product_id=%s", (store_id, product_id))
                stock_check = cur.fetchone()
                current_stock = stock_check["qty"] if stock_check else 0

                if adjust_stock:
                    err = _apply_stock_delta(conn, store_id, product_id, "판매출고", qty)
                    if err:
                        raise ValueError(err)
                    trans_type = "판매출고"
                else:
                    trans_type = "판매출고"

                cur.execute(
                    """INSERT INTO stock_transactions
                       (product_id, store_id, type, quantity, unit_cost, unit_price, staff, memo, date_time)
                       VALUES (%s, %s, %s, %s, %s, %s, '일괄업로드', %s, %s)""",
                    (product_id, store_id, trans_type, qty, unit_cost, unit_price, memo, date)
                )

                results["success"] += 1
                row_info["status"] = "success"
                row_info["message"] = f"{product_name} {qty}개 등록 완료 (재고변동: {'있음' if adjust_stock else '없음'})"

            except ValueError as ve:
                results["failed"] += 1
                row_info["status"] = "failed"
                row_info["message"] = str(ve)
                results["errors"].append(f"{row_num}행: {str(ve)}")
            except Exception as e:
                results["failed"] += 1
                row_info["status"] = "failed"
                row_info["message"] = f"오류: {str(e)}"
                results["errors"].append(f"{row_num}행: {str(e)}")

            results["rows"].append(row_info)

        conn.commit()
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": f"파일 처리 중 오류: {str(e)}"}), 400

@app.route("/api/import/transactions_template")
def api_import_transactions_template():
    adjust = request.args.get('adjust', 'false') == 'true'

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['일시', '제품명', '브랜드', '카테고리', '수량', '비고'])

    if adjust:
        writer.writerow(['2026-07-01', '예시 제품A', '네스티', '일회용', '5', '출고'])
        writer.writerow(['2026-07-02', '예시 제품B', '플릭', '액상', '2', '고객 주문'])
        filename = '출고내역_업로드_템플릿_출고용.csv'
    else:
        writer.writerow(['2026-07-01', '예시 제품A', '네스티', '일회용', '5', '단순 기록'])
        writer.writerow(['2026-07-02', '예시 제품B', '플릭', '액상', '2', '테스트'])
        filename = '출고내역_업로드_템플릿_기록용.csv'

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=filename
    )


# ---------------------------------------------------------------------------
# API - CSV 브랜드 업로드
# ---------------------------------------------------------------------------

@app.route("/api/import/brands", methods=["POST"])
def api_import_brands():
    conn = get_db()
    cur = g.cursor
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "파일이 선택되지 않았습니다."}), 400

    try:
        content = file.stream.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        results = {"success": 0, "failed": 0, "errors": []}

        for row_num, row in enumerate(reader, start=2):
            try:
                name = row.get("브랜드명", "").strip()
                category_name = row.get("카테고리", "").strip()
                color = row.get("색상", "").strip() or "#8a8f98"
                status = row.get("상태", "").strip() or "approved"

                if not name:
                    results["errors"].append(f"{row_num}행: 브랜드명이 비어있습니다.")
                    results["failed"] += 1
                    continue

                category_id = None
                if category_name:
                    cur.execute("SELECT id FROM categories WHERE name = %s", (category_name,))
                    cat = cur.fetchone()
                    if cat:
                        category_id = cat["id"]
                    else:
                        cur.execute("INSERT INTO categories (name) VALUES (%s) RETURNING id", (category_name,))
                        category_id = cur.fetchone()["id"]

                cur.execute("SELECT id FROM brands WHERE name = %s", (name,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        "UPDATE brands SET category_id = %s, color = %s, status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (category_id, color, status, existing["id"])
                    )
                else:
                    cur.execute(
                        "INSERT INTO brands (name, category_id, color, status) VALUES (%s, %s, %s, %s) RETURNING id",
                        (name, category_id, color, status)
                    )
                results["success"] += 1

            except Exception as e:
                results["errors"].append(f"{row_num}행: {str(e)}")
                results["failed"] += 1

        conn.commit()
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": f"파일 처리 중 오류: {str(e)}"}), 400

@app.route("/api/export/brands")
def api_export_brands():
    conn = get_db()
    cur = g.cursor
    cur.execute("""
        SELECT b.name as 브랜드명, c.name as 카테고리, b.color as 색상, b.status as 상태
        FROM brands b
        LEFT JOIN categories c ON c.id = b.category_id
        ORDER BY b.name
    """)
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['브랜드명', '카테고리', '색상', '상태'])
    for r in rows:
        writer.writerow([r['브랜드명'], r['카테고리'] or '', r['색상'] or '#8a8f98', r['상태'] or 'approved'])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name='브랜드_목록.csv'
    )


# ---------------------------------------------------------------------------
# API - 설정
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    conn = get_db()
    cur = g.cursor
    if request.method == "GET":
        try:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
            return jsonify({r["key"]: r["value"] for r in rows})
        except Exception as e:
            print(f"⚠️ 설정 조회 오류: {e}")
            return jsonify({})
    data = request.get_json(force=True)
    for key, value in data.items():
        try:
            cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, str(value)))
        except Exception as e:
            print(f"⚠️ 설정 저장 오류 ({key}): {e}")
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - 선결제 주문
# ---------------------------------------------------------------------------

@app.route("/api/pre_orders", methods=["GET", "POST"])
def api_pre_orders():
    conn = get_db()
    cur = g.cursor

    if request.method == "GET":
        store_id = request.args.get("store_id")
        status = request.args.get("status", "대기")
        if not store_id:
            return jsonify({"error": "매장을 선택해주세요."}), 400

        try:
            sql = """
                SELECT po.*, string_agg(poi.product_id || ':' || poi.quantity || ':' || poi.unit_price || ':' || poi.discount_amount, ',') as items_raw
                FROM pre_orders po
                LEFT JOIN pre_order_items poi ON poi.pre_order_id = po.id
                WHERE po.store_id = %s
            """
            params = [store_id]
            if status:
                sql += " AND po.status = %s"
                params.append(status)
            sql += " GROUP BY po.id ORDER BY po.created_at DESC"

            cur.execute(sql, params)
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                items = []
                if d.get('items_raw'):
                    for item_str in d['items_raw'].split(','):
                        parts = item_str.split(':')
                        if len(parts) == 4:
                            items.append({
                                'product_id': int(parts[0]),
                                'quantity': int(parts[1]),
                                'unit_price': int(parts[2]),
                                'discount_amount': int(parts[3])
                            })
                d['items'] = items
                d.pop('items_raw', None)
                result.append(d)
            return jsonify(result)
        except Exception as e:
            print(f"❌ 선결제 주문 조회 오류: {e}")
            return jsonify([])

    try:
        data = request.get_json(force=True)
        store_id = data.get("store_id")
        customer_name = (data.get("customer_name") or "").strip()
        customer_phone = (data.get("customer_phone") or "").strip()
        customer_address = (data.get("customer_address") or "").strip()
        request_memo = (data.get("request_memo") or "").strip()
        payment_method = data.get("payment_method")
        items = data.get("items", [])

        if not store_id:
            return jsonify({"error": "매장을 선택해주세요."}), 400
        try:
            store_id = int(store_id)
        except:
            return jsonify({"error": "매장 ID가 올바르지 않습니다."}), 400

        if not payment_method:
            return jsonify({"error": "결제수단을 선택해주세요."}), 400
        if payment_method not in ("현금", "카드", "계좌이체"):
            return jsonify({"error": "결제수단은 '현금', '카드', '계좌이체'만 가능합니다."}), 400

        if not items or len(items) == 0:
            return jsonify({"error": "주문 항목이 없습니다."}), 400

        total_amount = 0
        validated_items = []
        for idx, item in enumerate(items):
            product_id = item.get("product_id")
            qty = int(item.get("quantity") or 0)
            unit_price = int(item.get("unit_price") or 0)
            discount = int(item.get("discount_amount") or 0)

            if not product_id:
                return jsonify({"error": f"{idx+1}번째 항목: 제품 ID가 없습니다."}), 400
            if qty <= 0:
                return jsonify({"error": f"{idx+1}번째 항목: 수량은 1 이상이어야 합니다."}), 400
            if unit_price < 0:
                return jsonify({"error": f"{idx+1}번째 항목: 단가는 0 이상이어야 합니다."}), 400
            if discount < 0:
                return jsonify({"error": f"{idx+1}번째 항목: 할인금액은 0 이상이어야 합니다."}), 400

            cur.execute("SELECT id, cost_price FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            if not product:
                return jsonify({"error": f"제품 ID {product_id}가 존재하지 않습니다."}), 400

            final_unit_price = int((unit_price * qty - discount) / qty) if qty > 0 else 0
            total_amount += (unit_price * qty) - discount

            validated_items.append({
                "product_id": product_id,
                "quantity": qty,
                "unit_price": unit_price,
                "discount_amount": discount,
                "final_unit_price": final_unit_price,
                "cost_price": product["cost_price"] or 0
            })

        customer_id = upsert_customer(cur, customer_name, customer_phone, customer_address)

        cur.execute(
            """INSERT INTO pre_orders (store_id, customer_name, customer_phone, customer_address, request_memo, payment_method, total_amount, status, customer_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, '대기', %s) RETURNING id""",
            (store_id, customer_name, customer_phone, customer_address, request_memo, payment_method, total_amount, customer_id)
        )
        order_id = cur.fetchone()["id"]

        for item in validated_items:
            cur.execute(
                """INSERT INTO stock_transactions
                (product_id, store_id, type, quantity, unit_cost, unit_price, payment_method, staff, memo)
                VALUES (%s, %s, '선결예약', %s, %s, %s, %s, NULL, %s)""",
                (item["product_id"], store_id, item["quantity"], item["cost_price"], item["final_unit_price"],
                 payment_method, f"선결제 대기 #{order_id}")
            )

        for item in validated_items:
            cur.execute(
                """INSERT INTO pre_order_items (pre_order_id, product_id, quantity, unit_price, discount_amount)
                   VALUES (%s, %s, %s, %s, %s)""",
                (order_id, item["product_id"], item["quantity"], item["unit_price"], item["discount_amount"])
            )

        conn.commit()
        return jsonify({"id": order_id, "ok": True})

    except Exception as e:
        conn.rollback()
        print(f"❌ 선결제 주문 등록 오류: {e}")
        return jsonify({"error": f"서버 내부 오류: {str(e)}"}), 500

@app.route("/api/pre_orders/<int:order_id>/confirm", methods=["PUT"])
def api_pre_order_confirm(order_id):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM pre_orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        return jsonify({"error": "주문을 찾을 수 없습니다."}), 404
    if order["status"] != "대기":
        return jsonify({"error": "이미 처리된 주문입니다."}), 400

    cur.execute("SELECT * FROM pre_order_items WHERE pre_order_id = %s", (order_id,))
    items = cur.fetchall()

    for item in items:
        product_id = item["product_id"]
        quantity = item["quantity"]
        store_id = order["store_id"]
        err = _apply_stock_delta(conn, store_id, product_id, "판매출고", quantity)
        if err:
            return jsonify({"error": f"재고 부족: {err}"}), 400

    cur.execute("UPDATE pre_orders SET status = '출고완료', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (order_id,))
    conn.commit()
    return jsonify({"ok": True, "order_id": order_id})

@app.route("/api/pre_orders/<int:order_id>", methods=["DELETE"])
def api_pre_order_delete(order_id):
    conn = get_db()
    cur = g.cursor
    cur.execute("SELECT * FROM pre_orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        return jsonify({"error": "주문을 찾을 수 없습니다."}), 404
    if order["status"] != "대기":
        return jsonify({"error": "출고 완료된 주문은 삭제할 수 없습니다."}), 400

    try:
        cur.execute("DELETE FROM stock_transactions WHERE type='선결예약' AND memo LIKE %s", (f"%선결제 대기 #{order_id}%",))
        cur.execute("DELETE FROM pre_order_items WHERE pre_order_id = %s", (order_id,))
        cur.execute("DELETE FROM pre_orders WHERE id = %s", (order_id,))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "삭제 중 오류가 발생했습니다."}), 500


# ---------------------------------------------------------------------------
# API - 재고 확인
# ---------------------------------------------------------------------------

@app.route("/api/stock")
def api_stock():
    conn = get_db()
    cur = g.cursor
    product_id = request.args.get("product_id")
    store_id = request.args.get("store_id")
    if not product_id or not store_id:
        return jsonify({"error": "제품과 매장을 선택해주세요."}), 400
    try:
        cur.execute("SELECT qty FROM store_stock WHERE product_id=%s AND store_id=%s", (product_id, store_id))
        stock = cur.fetchone()
        return jsonify({"qty": stock["qty"] if stock else 0})
    except Exception as e:
        print(f"⚠️ 재고 확인 오류: {e}")
        return jsonify({"qty": 0})


# ---------------------------------------------------------------------------
# API - 카테고리 트렌드
# ---------------------------------------------------------------------------

@app.route("/api/category_trend")
def api_category_trend():
    conn = get_db()
    cur = g.cursor
    try:
        today = now_kst().date()
        month_start = today.replace(day=1)

        if month_start.month == 1:
            last_month_start = today.replace(year=today.year-1, month=12, day=1)
        else:
            last_month_start = today.replace(month=today.month-1, day=1)
        last_month_end = last_month_start.replace(day=28)

        def get_quantity(start_date, end_date):
            cur.execute("""
                SELECT
                    c.name as category_name,
                    COALESCE(SUM(CASE WHEN t.type='판매출고' THEN t.quantity ELSE 0 END), 0) as qty
                FROM stock_transactions t
                JOIN products p ON p.id = t.product_id
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE t.type IN ('판매출고')
                  AND date(t.date_time) >= date(%s)
                  AND date(t.date_time) <= date(%s)
                  AND c.name IN ('일회용', '기기', '액상')
                GROUP BY c.id
            """, (start_date, end_date))
            rows = cur.fetchall()
            return {r["category_name"]: r["qty"] for r in rows}

        this_month_qty = get_quantity(month_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
        last_month_qty = get_quantity(last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d"))

        result = []
        for name in ['일회용', '기기', '액상']:
            this_val = this_month_qty.get(name, 0)
            last_val = last_month_qty.get(name, 0)
            change = this_val - last_val
            percent = round((change / last_val * 100), 1) if last_val > 0 else (100 if this_val > 0 else 0)
            result.append({
                "category": name,
                "this_month": this_val,
                "last_month": last_val,
                "change": change,
                "percent": percent,
                "trend": "up" if change > 0 else "down" if change < 0 else "same"
            })
        return jsonify(result)
    except Exception as e:
        print(f"❌ 카테고리 트렌드 오류: {e}")
        return jsonify([])


# ---------- 베스트셀러 TOP 10 ----------
@app.route("/api/bestsellers")
def api_bestsellers():
    conn = get_db()
    cur = g.cursor
    try:
        period = request.args.get("period", "week")
        store_id = request.args.get("store_id")

        today = now_kst().date()
        if period == "day":
            start_date = today.strftime("%Y-%m-%d")
        elif period == "week":
            start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        else:  # month
            start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        sql = """
            SELECT
                p.id,
                p.name,
                b.name as brand_name,
                b.color as brand_color,
                COALESCE(SUM(CASE WHEN t.type='판매출고' THEN t.quantity ELSE 0 END), 0) as sold_qty,
                COALESCE(SUM(CASE WHEN t.type='판매출고' THEN COALESCE(t.quantity, 0) * COALESCE(t.unit_price, 0) ELSE 0 END), 0) as revenue
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            INNER JOIN stock_transactions t ON t.product_id = p.id
                AND t.type IN ('판매출고')
                AND date(t.date_time) >= date(%s)
                AND date(t.date_time) <= date(%s)
                AND ((t.memo NOT LIKE %s AND t.memo NOT LIKE %s) OR t.memo IS NULL)
            WHERE p.is_active = 1
        """
        # memo LIKE 패턴은 파라미터로 넘긴다 (SQL 문자열에 '%'를 직접 넣으면
        # psycopg2 파라미터 치환 과정에서 예외가 나서 베스트셀러가 항상 빈 목록으로 나온다)
        params = [start_date, end_date, "%이동%", "%교환%"]
        if store_id:
            sql += " AND t.store_id = %s"
            params.append(store_id)

        sql += """
            GROUP BY p.id, b.name, b.color
            HAVING COALESCE(SUM(CASE WHEN t.type='판매출고' THEN t.quantity ELSE 0 END), 0) > 0
            ORDER BY sold_qty DESC
            LIMIT 10
        """

        cur.execute(sql, params)
        rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"❌ 베스트셀러 오류: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        init_db()
    except Exception as e:
        print(f"⚠️ 초기화 중 오류 발생 (계속 실행): {e}")
    print("=" * 60)
    print(" 🚀 재고·판매관리 프로그램 (PostgreSQL) 시작됨")
    print(" 🌐 로컬 접속: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)