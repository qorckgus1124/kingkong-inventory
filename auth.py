# -*- coding: utf-8 -*-
"""로그인 / 로그아웃 / 계정 관리.

구조
- 매장당 계정 1개(role='store') + 어드민(role='admin').
- 비밀번호는 해시로만 저장한다. 어드민이 직접 정했거나 "초기화"로 발급한 비밀번호는
  그 순간 화면에 한 번 보여준다(저장된 해시에서 원문을 되살릴 수는 없다).
- 사이트 전체를 잠그는 전역 가드는 두지 않았다(내부 사용 전제).
  로그인이 필요한 곳은 이 파일이 만든 화면/API(/my-stock, /account, /admin/*)뿐이다.
- 로그인 실패 잠금, 비밀번호 규칙은 두지 않았다(요청 사항).

app.py 쪽 연결은 두 줄이다:
    auth.init_auth(app, get_db)      # 라우트 등록
    auth.ensure_schema()             # users 테이블 + 최초 어드민 계정
"""
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
            username = (os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
            password = os.environ.get("ADMIN_INITIAL_PASSWORD")
            generated = not password
            if generated:
                password = secrets.token_urlsafe(9)
            cur.execute(
                """INSERT INTO users (username, password_hash, role, display_name, must_change_pw)
                   VALUES (%s, %s, 'admin', '대표', %s)
                   ON CONFLICT (username) DO NOTHING""",
                (username, generate_password_hash(password), 1 if generated else 0),
            )
            conn.commit()
            if generated and cur.rowcount:
                # 두 워커가 동시에 뜨면 한쪽만 insert 된다. 만든 쪽만 로그에 남는다.
                print(f"🔐 어드민 계정을 새로 만들었습니다. 아이디: {username} / 임시 비밀번호: {password}"
                      " (Render 로그에서 확인하고, 로그인 후 바꾸세요)")
            elif cur.rowcount:
                print(f"🔐 어드민 계정을 만들었습니다. 아이디: {username} (ADMIN_INITIAL_PASSWORD 값)")
    except Exception as e:
        conn.rollback()
        print(f"❌ users 테이블 준비 오류: {e}")


# ---------------------------------------------------------------------------
# 세션 / 데코레이터
# ---------------------------------------------------------------------------

def _start_session(row):
    session.clear()
    session.permanent = True
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
        if row and row["is_active"]:
            user = dict(row)
        else:
            session.clear()
    g._auth_user = user
    return user


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
    return "/dashboard" if role == "admin" else "/my-stock"


def _json():
    return request.get_json(silent=True) or {}


def _fail(message, status=400):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def init_auth(app, get_db):
    global _get_db
    _get_db = get_db

    app.permanent_session_lifetime = timedelta(days=30)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Render(HTTPS)에서만 Secure. 로컬 http 개발에서는 끈다.
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    )

    @app.context_processor
    def inject_current_user():
        # 화면(사이드바)용. 세션에 든 값만 쓰므로 DB를 건드리지 않는다.
        if not session.get("uid"):
            return {"current_user": None, "is_store_user": False}
        cu = {
            "username": session.get("username"),
            "role": session.get("role"),
            "name": session.get("name"),
            "store_name": session.get("store_name"),
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
                   COALESCE(p.cost_price, 0) AS cost_price,
                   COALESCE(p.card_cost_price, 0) AS card_cost_price,
                   COALESCE(p.sale_price, 0) AS sale_price
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN store_stock ss ON ss.product_id = p.id AND ss.store_id = %s
            WHERE COALESCE(p.is_active, 1) = 1
            ORDER BY c.name NULLS LAST, b.name NULLS LAST, p.name
            """,
            (store_id,),
        )
        items = [dict(r) for r in cur.fetchall()]
        return jsonify({
            "store": store,
            "stores": stores if is_admin else [],
            "is_admin": is_admin,
            "items": items,
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
        return jsonify({"ok": True})

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
