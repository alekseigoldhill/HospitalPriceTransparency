import os, json, zipfile, io, requests, psycopg2, psycopg2.extras
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone

NYP_URL = (
    "https://nyp.widen.net/content/hisgjrgpuk/original/"
    "133957095_NewYork-Presbyterian-Hospital_standardcharges.json.zip"
    "?u=n8xzey&download=true"
)
BATCH_SIZE = 10000
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
    print("Downloading...")
    r = requests.get(url, timeout=300, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    downloaded = 0
    for chunk in r.iter_content(chunk_size=1024 * 1024):
        buf.write(chunk)
        downloaded += len(chunk)
        print(f"\r  {downloaded / 1_000_000:.1f} MB", end="", flush=True)
    print()
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        with z.open(z.namelist()[0]) as f:
            data = json.load(f)
    hospital_data = data[0] if isinstance(data, list) else data
    charges = hospital_data.get("standard_charge_information", [])
    print(f"Loaded {len(charges):,} charge items")
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

def collect_entities(charges):
    print("Pass 1: collecting unique procedures and payers...")
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
    print(f"  {len(procedures):,} unique procedures")
    print(f"  {len(payers):,} unique payers")
    return procedures, payers

def bulk_insert_procedures(cur, procedures):
    rows = [(desc, code, code_type) for desc, (code, code_type) in procedures.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO procedures (description, code, code_type) VALUES %s
        ON CONFLICT (description) DO NOTHING
    """, rows)
    cur.execute("SELECT description, id FROM procedures")
    print(f"  {len(rows):,} procedures inserted")
    return {desc: pid for desc, pid in cur.fetchall()}

def bulk_insert_payers(cur, payers):
    psycopg2.extras.execute_values(cur, """
        INSERT INTO payers (name, plan_name) VALUES %s
        ON CONFLICT (name, plan_name) DO NOTHING
    """, list(payers))
    cur.execute("SELECT name, plan_name, id FROM payers")
    print(f"  {len(payers):,} payers inserted")
    return {(name, plan): pid for name, plan, pid in cur.fetchall()}

def build
