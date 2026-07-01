import os, json, zipfile, io, requests, psycopg2, psycopg2.extras
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone

NYP_URL = (
    "https://nyp.widen.net/content/hisgjrgpuk/original/"
    "133957095_NewYork-Presbyterian-Hospital_standardcharges.json.zip"
    "?u=n8xzey&download=true"
)
BATCH_SIZE = 5000
TEST_LIMIT = None

def get_conn():
    p = urlparse(os.environ["DATABASE_URL"])
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username,
        password=unquote(p.password), sslmode="require",
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )

def download_and_parse(url):
    print("Downloading NYP file...")
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    print(f"Downloaded {len(r.content)/1_000_000:.1f} MB")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(z.namelist()[0]) as f:
            data = json.load(f)
    hospital_data = data[0] if isinstance(data, list) else data
    charges = hospital_data.get("standard_charge_information", [])
    print(f"Found {len(charges):,} charge items")
    return hospital_data, charges

def upsert_hospital(cur, hospital_data):
    name = hospital_data.get("hospital_name", "New York-Presbyterian Hospital")
    locations = hospital_data.get("hospital_location", [{}])
    cms_id = str(locations[0].get("hospital_id", "330101")) if isinstance(locations, list) else "330101"
    cur.execute("""
        INSERT INTO hospitals (name, cms_id, state, city)
        VALUES (%s, %s, 'NY', 'New York')
        ON CONFLICT (cms_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (name, cms_id))
    hospital_id = cur.fetchone()[0]
    print(f"Hospital: {name} (ID {hospital_id})")
    return hospital_id

def load_caches(cur):
    print("Loading caches...")
    cur.execute("SELECT description, id FROM procedures")
    proc_cache = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT name, plan_name, id FROM payers")
    payer_cache = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    print(f"  {len(proc_cache):,} procedures | {len(payer_cache):,} payers cached")
    return proc_cache, payer_cache

def get_or_create_procedure(cur, cache, desc, code, code_type):
    if desc in cache:
        return cache[desc]
    cur.execute("""
        INSERT INTO procedures (description, code, code_type)
        VALUES (%s, %s, %s)
        ON CONFLICT (description) DO UPDATE SET code = EXCLUDED.code
        RETURNING id
    """, (desc, code, code_type))
    pid = cur.fetchone()[0]
    cache[desc] = pid
    return pid

def get_or_create_payer(cur, cache, name, plan):
    key = (name, plan)
    if key in cache:
        return cache[key]
    cur.execute("""
        INSERT INTO payers (name, plan_name)
        VALUES (%s, %s)
        ON CONFLICT (name, plan_name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (name, plan))
    pid = cur.fetchone()[0]
    cache[key] = pid
    return pid

def main():
    start = datetime.now(timezone.utc)
    conn = get_conn()
    cur = conn.cursor()

    hospital_data, charges = download_and_parse(NYP_URL)
    hospital_id = upsert_hospital(cur, hospital_data)
    conn.commit()

    proc_cache, payer_cache = load_caches(cur)

    if TEST_LIMIT:
        charges = charges[:TEST_LIMIT]
        print(f"TEST MODE: {TEST_LIMIT} items")

    cur.execute("""
        INSERT INTO import_log (hospital_id, status, procedures_total, started_at)
        VALUES (%s, 'running', %s, %s) RETURNING id
    """, (hospital_id, len(charges), start))
    log_id = cur.fetchone()[0]
    conn.commit()

    batch, total = [], 0

    for i, item in enumerate(charges):
        desc = item.get("description", "").strip()
        if not desc:
            continue

        codes = item.get("code_information", [])
        code = codes[0].get("code", "") if codes else ""
        code_type = codes[0].get("type", "") if codes else ""
        proc_id = get_or_create_procedure(cur, proc_cache, desc, code, code_type)

        std_charges = item.get("standard_charges", [])
        top = std_charges[0] if std_charges else {}
        cash = top.get("discounted_cash") or top.get("gross_charge")
        min_neg = top.get("minimum")
        max_neg = top.get("maximum")

        batch.append((hospital_id, proc_id, None, cash, None, min_neg, max_neg))

        for sc in std_charges:
            for pi in sc.get("payers_information", []):
                pname = pi.get("payer_name", "").strip()
                plan = pi.get("plan_name", "").strip() or None
                rate = pi.get("negotiated_rate") or pi.get("negotiated_dollar")
                if not pname or rate is None:
                    continue
                payer_id = get_or_create_payer(cur, payer_cache, pname, plan)
                batch.append((hospital_id, proc_id, payer_id, None, rate, None, None))

        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO prices
                  (hospital_id, procedure_id, payer_id, cash_price, negotiated_rate, min_negotiated, max_negotiated)
                VALUES %s
            """, batch)
            total += len(batch)
            batch = []
            conn.commit()
            elapsed = int((datetime.now(timezone.utc) - start).total_seconds() // 60)
            print(f"  [{elapsed} min] {i+1:,}/{len(charges):,} items | {total:,} rows inserted")
            cur.execute("UPDATE import_log SET procedures_done=%s WHERE id=%s", (i+1, log_id))
            conn.commit()

    if batch:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO prices
              (hospital_id, procedure_id, payer_id, cash_price, negotiated_rate, min_negotiated, max_negotiated)
            VALUES %s
        """, batch)
        total += len(batch)
        conn.commit()

    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() // 60)
    cur.execute("""
        UPDATE import_log SET status='done', procedures_done=%s, finished_at=%s WHERE id=%s
    """, (len(charges), datetime.now(timezone.utc), log_id))
    conn.commit()
    print(f"\nDone! {total:,} rows in {elapsed} minutes.")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
