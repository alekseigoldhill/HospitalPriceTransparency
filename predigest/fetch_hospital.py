import os, json, zipfile, io, requests, psycopg2, psycopg2.extras
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone

BATCH_SIZE = 10000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def get_conn():
    p = urlparse(os.environ["DATABASE_URL"])
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username,
        password=unquote(p.password), sslmode="require",
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )

def download_file(url, fmt):
    print(f"  Downloading ({fmt})...")
    r = requests.get(url, timeout=300, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    downloaded = 0
    for chunk in r.iter_content(chunk_size=1024 * 1024):
        buf.write(chunk)
        downloaded += len(chunk)
        print(f"\r    {downloaded / 1_000_000:.1f} MB", end="", flush=True)
    print()
    buf.seek(0)
    if fmt == "json_zip":
        with zipfile.ZipFile(buf) as z:
            with z.open(z.namelist()[0]) as f:
                data = json.load(f)
    else:
        data = json.load(buf)
    hospital_data = data[0] if isinstance(data, list) else data
    charges = hospital_data.get("standard_charge_information", [])
    print(f"  Loaded {len(charges):,} charge items")
    return hospital_data, charges

def upsert_hospital(cur, hospital_data, config):
    name = config["name"]
    filename = config["url"].split("/")[-1].split("?")[0]
    for suffix in ["_standardcharges.json.zip", "_standardcharges.json", ".json.zip", ".json"]:
        if filename.endswith(suffix):
            filename = filename[:-len(suffix)]
            break
    cms_id = filename
    cur.execute("""
        INSERT INTO hospitals (name, cms_id, state, city)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (cms_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (name, cms_id, config["state"], config["city"]))
    hospital_id = cur.fetchone()[0]
    print(f"  Hospital: {name} (ID {hospital_id})")
    return hospital_id

def collect_entities(charges):
    procedures = {}
    payers = set()
    for item in charges:
        desc = item.get("description", "").strip()
        if not desc:
            continue
        codes = item.get("code_information", [])
        code = codes[0].get("code", "") if codes else ""
        code_type = codes[0].get("type", "") if codes else ""
        if desc not in procedures:
            procedures[desc] = (code, code_type)
        for sc in item.get("standard_charges", []):
            for pi in sc.get("payers_information", []):
                pname = pi.get("payer_name", "").strip()
                plan = pi.get("plan_name", "").strip() or None
                if pname:
                    payers.add((pname, plan))
    print(f"  {len(procedures):,} procedures | {len(payers):,} payers")
    return procedures, payers

def bulk_insert_procedures(cur, procedures):
    rows = [(desc, code, code_type) for desc, (code, code_type) in procedures.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO procedures (description, code, code_type) VALUES %s
        ON CONFLICT (description) DO NOTHING
    """, rows)
    cur.execute("SELECT description, id FROM procedures")
    return {desc: pid for desc, pid in cur.fetchall()}

def bulk_insert_payers(cur, payers):
    psycopg2.extras.execute_values(cur, """
        INSERT INTO payers (name, plan_name) VALUES %s
        ON CONFLICT (name, plan_name) DO NOTHING
    """, list(payers))
    cur.execute("SELECT name, plan_name, id FROM payers")
    return {(name, plan): pid for name, plan, pid in cur.fetchall()}

def build_price_rows(charges, hospital_id, proc_cache, payer_cache):
    for item in charges:
        desc = item.get("description", "").strip()
        if not desc or desc not in proc_cache:
            continue
        proc_id = proc_cache[desc]
        std_charges = item.get("standard_charges", [])
        top = std_charges[0] if std_charges else {}
        cash = top.get("discounted_cash") or top.get("gross_charge")
        if cash is not None:
            yield (hospital_id, proc_id, None, "cash", cash)
        min_neg = top.get("minimum")
        if min_neg is not None:
            yield (hospital_id, proc_id, None, "min", min_neg)
        max_neg = top.get("maximum")
        if max_neg is not None:
            yield (hospital_id, proc_id, None, "max", max_neg)
        for sc in std_charges:
            for pi in sc.get("payers_information", []):
                pname = pi.get("payer_name", "").strip()
                plan = pi.get("plan_name", "").strip() or None
                rate = (pi.get("negotiated_dollar") or
                        pi.get("negotiated_rate") or
                        pi.get("standard_charge_dollar") or
                        pi.get("estimated_amount"))
                if not pname or rate is None:
                    continue
                payer_id = payer_cache.get((pname, plan))
                if payer_id:
                    yield (hospital_id, proc_id, payer_id, "negotiated", rate)

def process_hospital(config):
    print(f"\n{'='*60}")
    print(f"Processing: {config['name']}")
    start = datetime.now(timezone.utc)
    conn = get_conn()
    cur = conn.cursor()
    hospital_id = None
    log_id = None
    try:
        hospital_data, charges = download_file(config["url"], config["format"])
        hospital_id = upsert_hospital(cur, hospital_data, config)
        conn.commit()
        procedures, payers = collect_entities(charges)
        proc_cache = bulk_insert_procedures(cur, procedures)
        payer_cache = bulk_insert_payers(cur, payers)
        conn.commit()
        cur.execute("""
            INSERT INTO import_log (hospital_id, status, procedures_total, started_at)
            VALUES (%s, 'running', %s, %s) RETURNING id
        """, (hospital_id, len(charges), start))
        log_id = cur.fetchone()[0]
        conn.commit()
        batch, total = [], 0
        for row in build_price_rows(charges, hospital_id, proc_cache, payer_cache):
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO prices
                      (hospital_id, procedure_id, payer_id, price_type, price)
                    VALUES %s
                """, batch)
                total += len(batch)
                batch = []
                conn.commit()
                elapsed = int((datetime.now(timezone.utc) - start).total_seconds() // 60)
                print(f"    [{elapsed} min] {total:,} rows inserted")
                cur.execute("UPDATE import_log SET procedures_done=%s WHERE id=%s", (total, log_id))
                conn.commit()
        if batch:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO prices
                  (hospital_id, procedure_id, payer_id, price_type, price)
                VALUES %s
            """, batch)
            total += len(batch)
            conn.commit()
        elapsed = int((datetime.now(timezone.utc) - start).total_seconds() // 60)
        cur.execute("""
            UPDATE import_log SET status='done', procedures_done=%s, finished_at=%s WHERE id=%s
        """, (len(charges), datetime.now(timezone.utc), log_id))
        conn.commit()
        print(f"  Done! {total:,} rows in {elapsed} minutes.")
    except Exception as e:
        print(f"  ERROR: {e}")
        if log_id:
            try:
                cur.execute("UPDATE import_log SET status='error', error_message=%s WHERE id=%s", (str(e), log_id))
                conn.commit()
            except:
                pass
        raise
    finally:
        cur.close()
        conn.close()

def main():
    hospitals_file = os.path.join(SCRIPT_DIR, "hospitals.json")
    with open(hospitals_file) as f:
        hospitals = json.load(f)
    print(f"Found {len(hospitals)} hospitals to process")
    failed = []
    for config in hospitals:
        try:
            process_hospital(config)
        except Exception as e:
            print(f"  SKIPPING {config['name']}: {e}")
            failed.append(config['name'])
    if failed:
        print(f"\nFailed: {failed}")
    else:
        print("\nAll hospitals processed successfully.")

if __name__ == "__main__":
    main()
