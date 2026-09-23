import psycopg2

def unlock():
    conn = psycopg2.connect(dbname='fitness_master', user='postgres', password='', host='localhost', port=5432)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname in ('test_fitness_master', 'test_fitness_tenant') AND pid != pg_backend_pid();")
    rows = cur.fetchall()
    print("Terminated connections:", rows)
    conn.close()

if __name__ == '__main__':
    unlock()
