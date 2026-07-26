import psycopg2


def main():
    conn = psycopg2.connect('postgres://avnadmin:<redacted>@pg-10887be1-kingkongvape.h.aivencloud.com:27313/defaultdb?sslmode=require')

    query_sql = 'SELECT VERSION()'

    cur = conn.cursor()
    cur.execute(query_sql)

    version = cur.fetchone()[0]
    print(version)


if __name__ == "__main__":
    main()