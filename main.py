"""DB 연결 확인용 스크립트.

접속 문자열은 코드에 적지 않고 환경 변수 DATABASE_URL에서 읽는다.
(예전에는 아이디/비밀번호가 그대로 적혀 있어서, 저장소를 공개하면 DB 접속 정보가
 그대로 노출되는 상태였다.)

사용 예 (PowerShell):
    $env:DATABASE_URL = "postgres://사용자:비밀번호@호스트:포트/DB이름?sslmode=require"
    python main.py
"""
import os
import sys

import psycopg2


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("❌ 환경 변수 DATABASE_URL이 설정되어 있지 않습니다.")
        print('   예) $env:DATABASE_URL = "postgres://사용자:비밀번호@호스트:포트/DB?sslmode=require"')
        sys.exit(1)

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                print(cur.fetchone()[0])
    except Exception as e:
        print(f"❌ DB 접속 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
