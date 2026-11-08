# -*- coding: utf-8 -*-
"""로그인 / 로그아웃 / 계정 관리.

구조
- 매장당 계정 1개(role='store') + 어드민(role='admin').
- 비밀번호는 해시로만 저장한다. 어드민이 직접 정했거나 "초기화"로 발급한 비밀번호는
  그 순간 화면에 한 번 보여준다(저장된 해시에서 원문을 되살릴 수는 없다).
- 로그인하지 않으면 로그인 화면으로 보낸다(전역 가드, 아래 require_login_everywhere).
- 어드민: 모든 매장의 모든 기능/데이터.
- 매장 계정: 대시보드의 기능은 모두 쓸 수 있지만, 서버가 모든 요청을 자기 매장으로 고정한다
  (store_id를 다른 값으로 보내도 무시하고 자기 매장으로 바꾼다 → 다른 매장 조회/변경 불가).
  매장 목록·고객·메모 같은 곳도 자기 매장 몫만 돌려준다. 자세한 규칙은 _enforce_store_scope 참고.
- 로그인 실패 잠금, 비밀번호 규칙은 두지 않았다(요청 사항).

app.py 쪽 연결은 두 줄이다:
    auth.init_auth(app, get_db)      # 라우트 등록
    auth.ensure_schema()             # users 테이블 + 최초 어드민 계정
"""
import hashlib
import os
import re
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import quote

import psycopg2
from flask import g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

_get_db = None

# 최초 기본값. 환경변수(ADMIN_USERNAME / ADMIN_INITIAL_PASSWORD)가 있으면 그쪽이 우선한다.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "1234"

# 매장 계정 초기 세팅: (매장 이름, 아이디). 비밀번호는 모두 아래 값으로 시작한다.
STORE_ACCOUNT_SEED = [
    ("서초점", "kingkongsc"),
    ("논현점", "kingkongnh"),
    ("강남역점", "kingkong"),
    ("신사점", "kingkongblack"),
]
STORE_ACCOUNT_INITIAL_PASSWORD = "1234"

# 이미 만들어진 어드민 계정의 비밀번호를 1234로 한 번만 되돌리는 표시(auth_seed_done.name).
# 한 번 적용된 뒤에는 어드민이 비밀번호를 바꿔도 다시 덮어쓰지 않는다.
ADMIN_PASSWORD_RESET_FLAG = "admin_password_1234_v1"
ADMIN_RESET_PASSWORD = "1234"

USERNAME_RE = re.compile(r"^\S{1,50}$")


def _db():
    _get_db()
    return g.db, g.cursor


def _ts(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def _clean(value, limit):
    return (value or "").strip()[:limit]


def _temp_password():
    """직원이 바로 칠 수 있는 6자리 숫자."""
    return "".join(secrets.choice("0123456789") for _ in range(6))


# ---------------------------------------------------------------------------
# 테이블 / 최초 어드민
# ---------------------------------------------------------------------------

def ensure_schema():
    conn, cur = _db()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'store',
                store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL,
                display_name TEXT,
                phone TEXT,
                email TEXT,
                must_change_pw INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_store ON users(store_id);")
        conn.commit()

        cur.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
        if cur.fetchone() is None:
            username = (os.environ.get("ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
            password = os.environ.get("ADMIN_INITIAL_PASSWORD") or DEFAULT_ADMIN_PASSWORD
            cur.execute(
                """INSERT INTO users (username, password_hash, role, display_name, must_change_pw)
                   VALUES (%s, %s, 'admin', '대표', 0)
                   ON CONFLICT (username) DO NOTHING""",
                (username, generate_password_hash(password)),
            )
            created = cur.rowcount
            conn.commit()
            if created:
                print(f"🔐 어드민 계정을 만들었습니다. 아이디: {username}")

        _seed_store_accounts(conn, cur)
        _reset_admin_password_once(conn, cur)
    except Exception as e:
        conn.rollback()
        print(f"❌ users 테이블 준비 오류: {e}")


def _reset_admin_password_once(conn, cur):
    """어드민 계정(들)의 비밀번호를 1234로 바꾼다. 딱 한 번만 실행한다.
    (기본값만 바꾸면 이미 만들어진 계정에는 적용되지 않기 때문에 별도로 한 번 덮어쓴다.)
    비밀번호가 바뀌면 기존 어드민 로그인은 풀리므로 1234로 다시 로그인해야 한다."""
    cur.execute("CREATE TABLE IF NOT EXISTS auth_seed_done (name TEXT PRIMARY KEY, done_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    # 동시에 뜬 워커 중 한쪽만 진행하도록 먼저 표시를 남긴다.
    cur.execute("INSERT INTO auth_seed_done (name) VALUES (%s) ON CONFLICT DO NOTHING", (ADMIN_PASSWORD_RESET_FLAG,))
    if cur.rowcount == 0:
        conn.commit()
        return
    cur.execute(
        "UPDATE users SET password_hash = %s, must_change_pw = 0 WHERE role = 'admin'",
        (generate_password_hash(ADMIN_RESET_PASSWORD),),
    )
    changed = cur.rowcount
    conn.commit()
    print(f"🔐 어드민 비밀번호를 1234로 설정했습니다 (계정 {changed}개)")


def _seed_store_accounts(conn, cur):
    """매장 4곳의 첫 계정을 만든다. 딱 한 번만 실행한다(이후 어드민이 지우거나 바꾼 계정은 되살리지 않는다).
    매장 이름이 정확히 같은 곳을 우선 찾고, 없으면 '서초'처럼 앞부분이 같은 매장을 쓴다.
    해당 매장이 DB에 없으면 그 매장은 건너뛴다(매장을 새로 만들지는 않는다)."""
    cur.execute("CREATE TABLE IF NOT EXISTS auth_seed_done (name TEXT PRIMARY KEY, done_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("SELECT 1 FROM auth_seed_done WHERE name = 'store_accounts_v1'")
    if cur.fetchone():
        conn.commit()
        return
    # 동시에 뜬 워커 중 한쪽만 진행하도록 먼저 표시를 남긴다.
    cur.execute("INSERT INTO auth_seed_done (name) VALUES ('store_accounts_v1') ON CONFLICT DO NOTHING")
    if cur.rowcount == 0:
        conn.commit()
        return
    pw_hash = generate_password_hash(STORE_ACCOUNT_INITIAL_PASSWORD)
    made, skipped = [], []
    for store_name, username in STORE_ACCOUNT_SEED:
        cur.execute("SELECT id, name FROM stores WHERE name = %s", (store_name,))
        store = cur.fetchone()
        if store is None:
            stem = store_name[:-1] if store_name.endswith("점") else store_name
            cur.execute("SELECT id, name FROM stores WHERE name LIKE %s ORDER BY id LIMIT 1", (stem + "%",))
            store = cur.fetchone()
        if store is None:
            skipped.append(f"{store_name}(매장 없음)")
            continue
        cur.execute("SELECT 1 FROM users WHERE role = 'store' AND store_id = %s", (store["id"],))
        if cur.fetchone():
            skipped.append(f"{store_name}(이미 계정 있음)")
            continue
        cur.execute(
            """INSERT INTO users (username, password_hash, role, store_id, display_name, must_change_pw)
               VALUES (%s, %s, 'store', %s, %s, 0)
               ON CONFLICT (username) DO NOTHING""",
            (username, pw_hash, store["id"], store["name"]),
        )
        (made if cur.rowcount else skipped).append(
            f"{store['name']}:{username}" if cur.rowcount else f"{store_name}(아이디 중복)")
    conn.commit()
    print(f"🔐 매장 계정 초기 세팅 - 생성: {', '.join(made) or '없음'}"
          + (f" / 건너뜀: {', '.join(skipped)}" if skipped else ""))


# ---------------------------------------------------------------------------
# 세션 / 데코레이터
# ---------------------------------------------------------------------------

def _pw_fp(password_hash):
    """비밀번호 해시의 지문. 세션에 넣어 두었다가 비교한다 → 비밀번호가 바뀌면 이전 로그인은 모두 무효."""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:20]


def _start_session(row):
    session.clear()
    session.permanent = True
    session["pwf"] = _pw_fp(row["password_hash"])
    session["uid"] = row["id"]
    session["username"] = row["username"]
    session["role"] = row["role"]
    session["name"] = row.get("display_name") or row["username"]
    session["store_name"] = row.get("store_name")


def _load_user():
    """DB 기준으로 현재 로그인 사용자를 확인한다(요청당 1회). 중지/삭제된 계정이면 세션을 비운다."""
    if "_auth_user" in g:
        return g._auth_user
    user = None
    uid = session.get("uid")
    if uid:
        _, cur = _db()
        cur.execute(
            """SELECT u.*, s.name AS store_name
               FROM users u LEFT JOIN stores s ON s.id = u.store_id
               WHERE u.id = %s""",
            (uid,),
        )
        row = cur.fetchone()
        # 중지/삭제된 계정, 또는 로그인한 뒤 비밀번호가 바뀐 계정이면 로그인 풀림
        if row and row["is_active"] and session.get("pwf") == _pw_fp(row["password_hash"]):
            user = dict(row)
        else:
            session.clear()
    g._auth_user = user
    return user


def current_user_is_admin():
    """지금 요청의 사용자가 (중지·삭제되지 않은) 어드민인지 DB 기준으로 확인한다."""
    try:
        user = _load_user()
    except Exception:
        return False
    return bool(user) and user["role"] == "admin"


def _need_login():
    if request.path.startswith("/api/"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    target = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    return redirect("/login?next=" + quote(target, safe=""))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _load_user()
        if not user:
            return _need_login()
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _load_user()
        if not user:
            return _need_login()
        if user["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "어드민만 사용할 수 있습니다."}), 403
            return redirect("/my-stock")
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _home_for(role):
    # 어드민/매장 계정 모두 로그인하면 대시보드로 간다.
    return "/dashboard"


def _json():
    return request.get_json(silent=True) or {}


def _fail(message, status=400):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------------------
# 매장 계정의 범위 제한 (서버에서 강제한다 - 화면을 숨기는 것만으로는 막히지 않는다)
# ---------------------------------------------------------------------------
#
# 매장 계정이 할 수 있는 것: 대시보드/판매/재고/분석 등 메뉴의 기능 전부 (자기 매장 데이터로).
# 막는 것:
#   1) 다른 매장의 조회·변경 - 모든 /api/ 요청의 store_id(쿼리, JSON, 폼)를 자기 매장으로 덮어쓴다.
#      id로 가리키는 항목(거래, 선결제, 출근 기록, 이동, 고객)은 자기 매장 것인지 확인한다.
#   2) 전체 매장에 영향을 주는 관리 기능 - 설정, 백업, API 키, 공유 링크, 계정 관리,
#      매장 추가/삭제, 제품 삭제·병합(모든 매장의 기록이 같이 지워진다)
#   3) 인수인계 메모는 전역 설정이 아니라 매장별로 따로 저장한다.
# 재고 이동은 "내 매장 → 다른 매장" 방향만 가능하다(도착 매장은 이름만 보인다).

_STORE_SELF_PATHS = {"/account", "/my-stock", "/logout"}
_STORE_SELF_PREFIXES = ("/api/account", "/api/my/")
_STORE_BLOCKED_PAGES = {"/settings"}
_STORE_BLOCKED_PAGE_PREFIXES = ("/admin/", "/maintenance/")
_STORE_BLOCKED_API_PREFIXES = (
    "/api/admin/", "/api/backup", "/api/api_keys", "/api/webhook_logs",
    "/api/share_links", "/api/maintenance/", "/api/products/duplicates", "/api/products/merge",
)

# id로 가리키는 항목: (경로 패턴, 그 항목이 속한 매장을 알려주는 쿼리)
_OWNED_BY_ID = (
    (re.compile(r"^/api/transactions/(\d+)(?:/(?:cancel|undo))?$"),
     "SELECT store_id FROM stock_transactions WHERE id = %s"),
    (re.compile(r"^/api/pre_orders/(\d+)(?:/(?:confirm|cancel|cancel_any))?$"),
     "SELECT store_id FROM pre_orders WHERE id = %s"),
    (re.compile(r"^/api/attendance/(\d+)$"),
     "SELECT store_id FROM attendance WHERE id = %s"),
    # 이동 취소는 출발 매장(= 이동을 시킨 매장)만 할 수 있다.
    (re.compile(r"^/api/transfer/(\d+)/cancel$"),
     "SELECT from_store_id AS store_id FROM stock_movements WHERE id = %s"),
)
# 선택한 여러 항목(JSON {"ids": [...]})을 한 번에 처리하는 곳
_OWNED_BATCH = {
    "/api/transactions/cancel_batch":
        "SELECT 1 FROM stock_transactions WHERE id = ANY(%s) AND store_id IS DISTINCT FROM %s LIMIT 1",
    "/api/transactions/batch_status":
        "SELECT 1 FROM stock_transactions WHERE id = ANY(%s) AND store_id IS DISTINCT FROM %s LIMIT 1",
    "/api/movements/cancel_batch":
        "SELECT 1 FROM stock_movements WHERE id = ANY(%s) AND from_store_id IS DISTINCT FROM %s LIMIT 1",
}


def store_scope_id():
    """지금 요청의 사용자가 매장 계정이면 그 매장 id(연결된 매장이 없으면 0),
    어드민이거나 로그인하지 않았으면 None. app.py가 '자기 매장 것만' 돌려줄 때 쓴다."""
    try:
        user = _load_user()
    except Exception:
        return None
    if user and user["role"] == "store":
        return user.get("store_id") or 0
    return None


def _scope_deny(message, is_api, redirect_to="/dashboard"):
    if is_api:
        return jsonify({"error": message}), 403
    return redirect(redirect_to)


def _enforce_store_scope(user):
    """매장 계정의 요청을 검사한다. 막아야 하면 응답을, 통과면 None을 돌려준다."""
    path = request.path
    if path in _STORE_SELF_PATHS or path.startswith(_STORE_SELF_PREFIXES):
        return None
    is_api = path.startswith("/api/")
    sid = user.get("store_id")
    if not sid:
        return _scope_deny("연결된 매장이 없습니다. 어드민에게 문의하세요.", is_api, "/my-stock")

    if (path in _STORE_BLOCKED_PAGES or path.startswith(_STORE_BLOCKED_PAGE_PREFIXES)
            or path.startswith(_STORE_BLOCKED_API_PREFIXES)):
        return _scope_deny("어드민만 사용할 수 있습니다.", is_api)
    if not is_api:
        return None
    return _enforce_store_api(sid, path, request.method)


def _enforce_store_api(sid, path, method):
    # --- 전체 매장에 영향을 주는 동작은 어드민만 ---
    if method == "DELETE" and re.fullmatch(r"/api/products/\d+", path):
        return _fail("제품 삭제는 어드민만 할 수 있습니다. (모든 매장의 입출고 기록이 함께 지워집니다)", 403)
    if path == "/api/stores" and method != "GET":
        return _fail("매장 추가는 어드민만 할 수 있습니다.", 403)
    m = re.fullmatch(r"/api/stores/(\d+)", path)
    if m:
        if method == "DELETE":
            return _fail("매장 삭제는 어드민만 할 수 있습니다.", 403)
        if int(m.group(1)) != sid:
            return _fail("다른 매장 정보는 볼 수 없습니다.", 403)

    # --- 설정: 인수인계 메모만, 매장별로 따로 ---
    if path == "/api/settings":
        return _store_settings(sid, method)

    # --- 주소 안에 매장 번호가 들어 있는 곳 ---
    m = re.fullmatch(r"/api/export/([^/]+)/products", path)
    if m and m.group(1) != str(sid):
        return _fail("다른 매장 정보는 내보낼 수 없습니다.", 403)

    # --- id로 가리키는 항목이 내 매장 것인지 ---
    denied = _check_store_ownership(sid, path, method)
    if denied is not None:
        return denied

    # --- 마지막으로, 요청에 실린 매장 값을 모두 내 매장으로 덮어쓴다 ---
    return _force_store_params(sid, path, method)


def _store_settings(sid, method):
    """매장 계정이 쓰는 설정은 인수인계 메모뿐이고, 그 메모는 매장마다 따로 저장한다."""
    conn, cur = _db()
    key = f"handover_memo_store_{sid}"
    if method == "GET":
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return jsonify({"handover_memo": (row["value"] if row else "") or ""})
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not data or set(data) - {"handover_memo"}:
        return _fail("매장 계정은 인수인계 메모만 저장할 수 있습니다.", 403)
    try:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, str(data.get("handover_memo") or "")),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return _fail(f"저장 실패: {e}", 500)
    return jsonify({"ok": True})


def _check_store_ownership(sid, path, method):
    for pattern, query in _OWNED_BY_ID:
        m = pattern.match(path)
        if m:
            _, cur = _db()
            cur.execute(query, (int(m.group(1)),))
            row = cur.fetchone()
            # 없는 항목이면 해당 화면의 "찾을 수 없음" 응답에 맡긴다.
            if row is not None and row["store_id"] != sid:
                return _fail("다른 매장의 기록은 건드릴 수 없습니다.", 403)
            return None

    query = _OWNED_BATCH.get(path)
    if query and method == "POST":
        data = request.get_json(force=True, silent=True)
        ids = data.get("ids") if isinstance(data, dict) else None
        try:
            ids = [int(i) for i in (ids or [])]
        except (TypeError, ValueError):
            return None  # 잘못된 값은 해당 화면의 검증이 처리한다.
        if ids:
            _, cur = _db()
            cur.execute(query, (ids, sid))
            if cur.fetchone() is not None:
                return _fail("다른 매장의 기록이 섞여 있어 처리할 수 없습니다.", 403)
        return None

    # 고객은 주문(선결제)을 통해서만 매장과 연결된다.
    m = re.fullmatch(r"/api/customers/(\d+)", path)
    if m:
        cid = int(m.group(1))
        _, cur = _db()
        cur.execute(
            """SELECT EXISTS (SELECT 1 FROM pre_orders WHERE customer_id = %s AND store_id = %s) AS mine,
                      EXISTS (SELECT 1 FROM pre_orders WHERE customer_id = %s AND store_id <> %s) AS other""",
            (cid, sid, cid, sid),
        )
        row = cur.fetchone()
        if not row["mine"]:
            return _fail("우리 매장 고객이 아닙니다.", 403)
        if method == "DELETE" and row["other"]:
            return _fail("다른 매장에서도 주문한 고객이라 삭제할 수 없습니다.", 403)
    return None


def _force_store_params(sid, path, method):
    """쿼리스트링·JSON·폼에 들어 있는 store_id를 전부 내 매장으로 바꾼다.
    (값이 아예 없는 요청도 '전체 매장'이 아니라 내 매장으로 처리되게 채워 넣는다.)"""
    args = request.args.copy()
    args["store_id"] = str(sid)
    request.args = args

    if method in ("GET", "HEAD"):
        return None

    if request.mimetype in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = request.form.copy()
        form["store_id"] = str(sid)
        request.form = form
        return None

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return None
    data["store_id"] = sid
    filters = data.get("filters")
    if isinstance(filters, dict):          # 커스텀 리포트
        filters["store_id"] = sid
    if path == "/api/transfer":
        # 출발은 항상 내 매장. 도착이 내 매장이면 이동이 아니다.
        data["from_store_id"] = sid
        try:
            data["to_store_id"] = int(data.get("to_store_id"))
        except (TypeError, ValueError):
            pass
        if data.get("to_store_id") == sid:
            return _fail("출발 매장과 도착 매장이 같습니다.")
    # 요청 본문을 다시 읽는 쪽(get_json)이 바뀐 값을 보도록 캐시를 갈아 끼운다.
    request._cached_json = (data, data)
    return None


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def init_auth(app, get_db):
    global _get_db
    _get_db = get_db

    # 한 번 로그인한 브라우저는 사실상 계속 유지(10년, 요청할 때마다 연장). 브라우저/컴퓨터를 껐다 켜도 유지된다.
    # 풀리는 경우: 비밀번호 변경·초기화, 계정 중지/삭제, 로그아웃 버튼, 쿠키 삭제.
    app.permanent_session_lifetime = timedelta(days=3650)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Render(HTTPS)에서만 Secure. 로컬 http 개발에서는 끈다.
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    )

    # 로그인 없이 열 수 있는 곳: 로그인/로그아웃, 헬스체크(/health), 정적 파일, 서비스워커·매니페스트,
    # API 키로 보호되는 /api/webhook/*, 토큰으로 여는 공유 링크 /share/*
    _OPEN_PATHS = {"/login", "/logout", "/sw.js", "/manifest.json", "/favicon.ico", "/health", "/healthz"}
    _OPEN_PREFIXES = ("/static/", "/api/webhook/", "/share/")

    @app.before_request
    def require_login_everywhere():
        path = request.path
        if request.method == "OPTIONS" or path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            return None
        user = _load_user()
        if not user:
            return _need_login()
        if user["role"] == "store":
            return _enforce_store_scope(user)
        return None

    @app.context_processor
    def inject_current_user():
        # 화면(사이드바)용. 세션에 든 값만 쓰므로 DB를 건드리지 않는다.
        if not session.get("uid"):
            return {"current_user": None, "is_store_user": False}
        # before_request에서 이미 DB로 확인한 사용자(g._auth_user)가 있으면 그 값을 쓴다
        # (예전에 로그인해 둔 세션에는 store_id가 들어 있지 않기 때문).
        db_user = g.get("_auth_user") or {}
        cu = {
            "username": session.get("username"),
            "role": session.get("role"),
            "name": session.get("name"),
            "store_name": db_user.get("store_name") or session.get("store_name"),
            "store_id": db_user.get("store_id"),
        }
        return {"current_user": cu, "is_store_user": cu["role"] == "store"}

    # ----- 로그인 / 로그아웃 -----------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        nxt = _safe_next(request.values.get("next"))
        if request.method == "GET":
            if session.get("uid") and _load_user():
                return redirect(nxt or _home_for(session.get("role")))
            return render_template("login.html", error=None, next=nxt or "", username="")

        username = _clean(request.form.get("username"), 50)
        password = request.form.get("password") or ""
        conn, cur = _db()
        cur.execute(
            """SELECT u.*, s.name AS store_name
               FROM users u LEFT JOIN stores s ON s.id = u.store_id
               WHERE u.username = %s""",
            (username,),
        )
        row = cur.fetchone()
        ok = bool(row) and bool(row["is_active"]) and check_password_hash(row["password_hash"], password)
        if not ok:
            return render_template(
                "login.html", error="아이디 또는 비밀번호가 올바르지 않습니다.",
                next=nxt or "", username=username,
            ), 401

        cur.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s", (row["id"],))
        conn.commit()
        _start_session(dict(row))
        if row["must_change_pw"]:
            return redirect("/account?first=1")
        return redirect(nxt or _home_for(row["role"]))

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect("/login")

    # ----- 재고 이동의 도착 매장 목록 (이름만) ---------------------------------
    @app.route("/api/transfer_targets")
    @login_required
    def api_transfer_targets():
        user = g.user
        _, cur = _db()
        cur.execute("SELECT id, name FROM stores ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        if user["role"] == "store":
            rows = [r for r in rows if r["id"] != user["store_id"]]
        return jsonify(rows)

    # ----- 매장용 재고 조회 (읽기 전용) ---------------------------------------

    @app.route("/my-stock")
    @login_required
    def my_stock_page():
        return render_template("my_stock.html", active="my_stock")

    @app.route("/api/my/stock")
    @login_required
    def api_my_stock():
        user = g.user
        _, cur = _db()
        cur.execute("SELECT id, name FROM stores ORDER BY id")
        stores = [dict(r) for r in cur.fetchall()]
        is_admin = user["role"] == "admin"

        if is_admin:
            store_id = request.args.get("store_id", type=int) or (stores[0]["id"] if stores else None)
        else:
            # 매장 계정은 요청에 무엇이 오든 자기 매장만 본다.
            store_id = user["store_id"]
        if not store_id:
            return _fail("연결된 매장이 없습니다. 어드민에게 문의하세요.")
        store = next((s for s in stores if s["id"] == store_id), None)
        if store is None:
            return _fail("매장을 찾을 수 없습니다.", 404)

        cur.execute(
            """
            SELECT p.id, p.name, b.name AS brand, c.name AS category,
                   COALESCE(ss.qty, 0) AS qty, COALESCE(ss.min_qty, 0) AS min_qty,
                   COALESCE(spc.cost_price, p.cost_price, 0) AS cost_price,
                   COALESCE(p.cost_price, 0) AS global_cost_price,
                   (spc.cost_price IS NOT NULL) AS cost_custom,
                   COALESCE(p.card_cost_price, 0) AS card_cost_price,
                   COALESCE(p.sale_price, 0) AS sale_price
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN store_stock ss ON ss.product_id = p.id AND ss.store_id = %s
            LEFT JOIN store_product_cost spc ON spc.product_id = p.id AND spc.store_id = %s
            WHERE COALESCE(p.is_active, 1) = 1
            ORDER BY c.name NULLS LAST, b.name NULLS LAST, p.name
            """,
            (store_id, store_id),
        )
        items = [dict(r) for r in cur.fetchall()]
        return jsonify({
            "store": store,
            "stores": stores if is_admin else [],
            "is_admin": is_admin,
            "items": items,
        })

    @app.route("/api/my/cost", methods=["PUT"])
    @login_required
    def api_my_cost():
        """매장 원가 수정. 매장 계정은 자기 매장만, 어드민은 store_id로 지정한 매장.
        cost_price가 비어 있으면(null/"") 매장 값을 지우고 전체 공통 원가로 되돌린다."""
        user = g.user
        data = _json()
        if user["role"] == "admin":
            try:
                store_id = int(data.get("store_id"))
            except (TypeError, ValueError):
                return _fail("매장을 선택하세요.")
        else:
            store_id = user["store_id"]
            if not store_id:
                return _fail("연결된 매장이 없습니다. 어드민에게 문의하세요.")
        try:
            product_id = int(data.get("product_id"))
        except (TypeError, ValueError):
            return _fail("제품이 올바르지 않습니다.")

        raw = data.get("cost_price")
        reset = raw is None or str(raw).strip() == ""
        if not reset:
            try:
                cost = int(float(str(raw).replace(",", "")))
            except ValueError:
                return _fail("원가는 숫자로 입력하세요.")
            if cost < 0 or cost > 100000000:
                return _fail("원가는 0 이상의 올바른 금액이어야 합니다.")

        conn, cur = _db()
        cur.execute("SELECT cost_price FROM products WHERE id = %s", (product_id,))
        prod = cur.fetchone()
        if prod is None:
            return _fail("제품을 찾을 수 없습니다.", 404)
        cur.execute("SELECT 1 FROM stores WHERE id = %s", (store_id,))
        if cur.fetchone() is None:
            return _fail("매장을 찾을 수 없습니다.", 404)
        try:
            if reset:
                cur.execute("DELETE FROM store_product_cost WHERE store_id = %s AND product_id = %s",
                            (store_id, product_id))
            else:
                cur.execute(
                    """INSERT INTO store_product_cost (store_id, product_id, cost_price, updated_by)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (store_id, product_id)
                       DO UPDATE SET cost_price = EXCLUDED.cost_price,
                                     updated_at = CURRENT_TIMESTAMP, updated_by = EXCLUDED.updated_by""",
                    (store_id, product_id, cost, user["username"]),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return _fail(f"저장 실패: {e}", 500)
        return jsonify({
            "ok": True,
            "cost_price": prod["cost_price"] or 0 if reset else cost,
            "global_cost_price": prod["cost_price"] or 0,
            "cost_custom": not reset,
        })

    # ----- 내 정보 ---------------------------------------------------------

    @app.route("/account")
    @login_required
    def account_page():
        return render_template("account.html", active="account", first=request.args.get("first") == "1")

    @app.route("/api/account")
    @login_required
    def api_account():
        u = g.user
        return jsonify({
            "username": u["username"], "role": u["role"], "store_name": u.get("store_name"),
            "display_name": u.get("display_name") or "", "phone": u.get("phone") or "",
            "email": u.get("email") or "", "must_change_pw": bool(u["must_change_pw"]),
        })

    @app.route("/api/account/profile", methods=["PUT"])
    @login_required
    def api_account_profile():
        data = _json()
        name = _clean(data.get("display_name"), 50)
        phone = _clean(data.get("phone"), 30)
        email = _clean(data.get("email"), 100)
        conn, cur = _db()
        try:
            cur.execute(
                "UPDATE users SET display_name=%s, phone=%s, email=%s WHERE id=%s",
                (name or None, phone or None, email or None, g.user["id"]),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return _fail(f"저장 실패: {e}", 500)
        session["name"] = name or g.user["username"]
        return jsonify({"ok": True})

    @app.route("/api/account/password", methods=["POST"])
    @login_required
    def api_account_password():
        data = _json()
        current = data.get("current_password") or ""
        new = data.get("new_password") or ""
        if not check_password_hash(g.user["password_hash"], current):
            return _fail("현재 비밀번호가 맞지 않습니다.")
        if not new:
            return _fail("새 비밀번호를 입력하세요.")
        if len(new) > 128:
            return _fail("비밀번호가 너무 깁니다.")
        conn, cur = _db()
        try:
            # 임시 비밀번호와 같은 값을 써도 된다(요청 사항). 변경 화면을 거쳤다는 표시만 해제한다.
            cur.execute(
                "UPDATE users SET password_hash=%s, must_change_pw=0 WHERE id=%s",
                (generate_password_hash(new), g.user["id"]),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return _fail(f"저장 실패: {e}", 500)
        # 비밀번호를 바꾸면 이 기기를 포함한 모든 기기에서 다시 로그인해야 한다.
        session.clear()
        return jsonify({"ok": True, "relogin": True})

    @app.route("/api/account/username", methods=["POST"])
    @login_required
    def api_account_username():
        data = _json()
        current = data.get("current_password") or ""
        new = _clean(data.get("new_username"), 50)
        if not check_password_hash(g.user["password_hash"], current):
            return _fail("현재 비밀번호가 맞지 않습니다.")
        if not USERNAME_RE.match(new):
            return _fail("아이디는 공백 없이 1~50자로 입력하세요.")
        conn, cur = _db()
        try:
            cur.execute("UPDATE users SET username=%s WHERE id=%s", (new, g.user["id"]))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
            return _fail("이미 사용 중인 아이디입니다.", 409)
        except Exception as e:
            conn.rollback()
            return _fail(f"저장 실패: {e}", 500)
        session["username"] = new
        return jsonify({"ok": True, "username": new})

    # ----- 어드민: 계정 관리 -------------------------------------------------

    @app.route("/admin/users")
    @admin_required
    def admin_users_page():
        return render_template("admin_users.html", active="admin_users")

    @app.route("/api/admin/users", methods=["GET"])
    @admin_required
    def api_admin_users():
        _, cur = _db()
        cur.execute(
            """SELECT u.id, u.username, u.role, u.store_id, s.name AS store_name,
                      u.display_name, u.phone, u.email, u.must_change_pw, u.is_active,
                      u.created_at, u.last_login_at
               FROM users u LEFT JOIN stores s ON s.id = u.store_id
               ORDER BY (u.role = 'admin') DESC, s.name NULLS LAST, u.username"""
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["created_at"] = _ts(d["created_at"])
            d["last_login_at"] = _ts(d["last_login_at"])
            d["must_change_pw"] = bool(d["must_change_pw"])
            d["is_active"] = bool(d["is_active"])
            d["is_me"] = d["id"] == g.user["id"]
            rows.append(d)
        return jsonify(rows)

    def _store_taken(cur, store_id, except_user_id=None):
        cur.execute(
            "SELECT id FROM users WHERE role = 'store' AND store_id = %s AND (%s IS NULL OR id <> %s)",
            (store_id, except_user_id, except_user_id),
        )
        return cur.fetchone() is not None

    @app.route("/api/admin/users", methods=["POST"])
    @admin_required
    def api_admin_users_create():
        data = _json()
        username = _clean(data.get("username"), 50)
        role = data.get("role") or "store"
        if role not in ("store", "admin"):
            return _fail("구분이 올바르지 않습니다.")
        if not USERNAME_RE.match(username):
            return _fail("아이디는 공백 없이 1~50자로 입력하세요.")
        conn, cur = _db()

        store_id = None
        if role == "store":
            store_id = data.get("store_id")
            try:
                store_id = int(store_id)
            except (TypeError, ValueError):
                return _fail("매장을 선택하세요.")
            cur.execute("SELECT 1 FROM stores WHERE id = %s", (store_id,))
            if cur.fetchone() is None:
                return _fail("매장을 찾을 수 없습니다.", 404)
            if _store_taken(cur, store_id):
                return _fail("이 매장에는 이미 계정이 있습니다. (매장당 계정 1개)", 409)

        password = data.get("password") or ""
        if len(password) > 128:
            return _fail("비밀번호가 너무 깁니다.")
        if not password:
            password = _temp_password()
        must_change = 1 if data.get("must_change_pw", True) else 0
        try:
            cur.execute(
                """INSERT INTO users (username, password_hash, role, store_id, display_name, phone, email, must_change_pw)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (username, generate_password_hash(password), role, store_id,
                 _clean(data.get("display_name"), 50) or None,
                 _clean(data.get("phone"), 30) or None,
                 _clean(data.get("email"), 100) or None, must_change),
            )
            new_id = cur.fetchone()["id"]
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
            return _fail("이미 사용 중인 아이디입니다.", 409)
        except Exception as e:
            conn.rollback()
            return _fail(f"생성 실패: {e}", 500)
        return jsonify({"ok": True, "id": new_id, "username": username, "password": password})

    @app.route("/api/admin/users/<int:uid>", methods=["PUT"])
    @admin_required
    def api_admin_users_update(uid):
        data = _json()
        conn, cur = _db()
        cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
        target = cur.fetchone()
        if target is None:
            return _fail("계정을 찾을 수 없습니다.", 404)

        sets, params = [], []
        issued_password = None

        if "username" in data:
            username = _clean(data.get("username"), 50)
            if not USERNAME_RE.match(username):
                return _fail("아이디는 공백 없이 1~50자로 입력하세요.")
            sets.append("username = %s"); params.append(username)
        for key, limit in (("display_name", 50), ("phone", 30), ("email", 100)):
            if key in data:
                sets.append(f"{key} = %s"); params.append(_clean(data.get(key), limit) or None)
        if "store_id" in data and target["role"] == "store":
            try:
                new_store = int(data.get("store_id"))
            except (TypeError, ValueError):
                return _fail("매장을 선택하세요.")
            cur.execute("SELECT 1 FROM stores WHERE id = %s", (new_store,))
            if cur.fetchone() is None:
                return _fail("매장을 찾을 수 없습니다.", 404)
            if _store_taken(cur, new_store, uid):
                return _fail("그 매장에는 이미 계정이 있습니다. (매장당 계정 1개)", 409)
            sets.append("store_id = %s"); params.append(new_store)
        if "is_active" in data:
            active = 1 if data.get("is_active") else 0
            if uid == g.user["id"] and not active:
                return _fail("내 계정은 중지할 수 없습니다.")
            sets.append("is_active = %s"); params.append(active)
        if data.get("password"):
            pw = str(data.get("password"))
            if len(pw) > 128:
                return _fail("비밀번호가 너무 깁니다.")
            sets.append("password_hash = %s"); params.append(generate_password_hash(pw))
            issued_password = pw
        if "must_change_pw" in data:
            sets.append("must_change_pw = %s"); params.append(1 if data.get("must_change_pw") else 0)

        if not sets:
            return _fail("바꿀 내용이 없습니다.")
        try:
            cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", params + [uid])
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
            return _fail("이미 사용 중인 아이디입니다.", 409)
        except Exception as e:
            conn.rollback()
            return _fail(f"저장 실패: {e}", 500)

        if uid == g.user["id"]:
            if "username" in data:
                session["username"] = _clean(data.get("username"), 50)
            if "display_name" in data:
                session["name"] = _clean(data.get("display_name"), 50) or session.get("username")
        return jsonify({"ok": True, "password": issued_password})

    @app.route("/api/admin/users/<int:uid>/reset", methods=["POST"])
    @admin_required
    def api_admin_users_reset(uid):
        conn, cur = _db()
        temp = _temp_password()
        try:
            cur.execute(
                "UPDATE users SET password_hash = %s, must_change_pw = 1 WHERE id = %s RETURNING username",
                (generate_password_hash(temp), uid),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return _fail("계정을 찾을 수 없습니다.", 404)
            conn.commit()
        except Exception as e:
            conn.rollback()
            return _fail(f"초기화 실패: {e}", 500)
        return jsonify({"ok": True, "username": row["username"], "password": temp})

    @app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
    @admin_required
    def api_admin_users_delete(uid):
        if uid == g.user["id"]:
            return _fail("내 계정은 삭제할 수 없습니다.")
        conn, cur = _db()
        try:
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))
            deleted = cur.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            return _fail(f"삭제 실패: {e}", 500)
        if not deleted:
            return _fail("계정을 찾을 수 없습니다.", 404)
        return jsonify({"ok": True})
