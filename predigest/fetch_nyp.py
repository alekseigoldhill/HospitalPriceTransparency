# ============================================================
# fetch_nyp.py
# Downloads and processes NewYork-Presbyterian pricing data.
# Uses batch inserts for speed.
# TEST_LIMIT controls how many procedures to process.
# Set to None to process the entire file.
# ============================================================

import requests
import json
import psycopg2
import psycopg2.extras
import os
import zipfile
import io
import urllib.parse
from datetime import date

# ---- SETTINGS -----------------------------------------------
TEST_LIMIT = 500  # Change to None to process the full file

# ---- DATABASE CONNECTION ------------------------------------
db_url = urllib.parse.urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(
    host=db_url.hostname,
    port=db_url.port,
    database=db_url.path[1:],
    user=db_url.username,
    password=urllib.parse.unquote(db_url.password)
)
cur = conn.cursor()

# ---- HOSPITAL RECORD ----------------------------------------
cur.execute("""
    INSERT INTO hospitals (name, cms_id, city, borough, state, source_url, last_updated)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (cms_id) DO UPDATE SET last_updated = EXCLUDED.last_updated
    RETURNING id
""", (
    "NewYork-Presbyterian Hospital",
    "330101",
    "New York",
    "Manhattan",
    "NY",
    "https://nyp.widen.net/content/hisgjrgpuk/original/133957095_NewYork-Presbyterian-Hospital_standardcharges.json.zip?u=n8xzey&download=true",
    date.today()
))
hospital_id = cur.fetchone()[0]
print(f"Hospital ID: {hospital_id}")

# ---- DOWNLOAD PRICING FILE ----------------------------------
url = "https://nyp.widen.net/content/hisgjrgpuk/original/133957095_NewYork-Presbyterian-Hospital_standardcharges.json.zip?u=n8xzey&download=true"
print("Downloading NewYork-Presbyterian pricing file...")
response = requests.get(url, timeout=120)

with zipfile.ZipFile(io.BytesIO(response.content)) as z:
    json_filename = [f for f in z.namelist() if f.endswith('.json')][0]
    with z.open(json_filename) as f:
        data = json.load(f)

print("File downloaded. Processing records...")

# ---- GET CHARGE ITEMS ---------------------------------------
hospital_data = data[0] if isinstance(data, list) else data
charge_items = hospital_data.get('standard_charge_information', [])
print(f"Found {len(charge_items)} total charge items.")

if TEST_LIMIT:
    charge_items = charge_items[:TEST_LIMIT]
    print(f"TEST MODE: Processing first {TEST_LIMIT} items only.")

# ---- PROCESS IN BATCHES -------------------------------------
inserted = 0
skipped = 0
price_batch = []
BATCH_SIZE = 500

for item in charge_items:
    try:
        description = item.get('description', '')
        if not description:
            skipped += 1
            continue

        code_info = item.get('code_information', [])
        code = code_info[0].get('code', '') if code_info else ''
        code_type = code_info[0].get('type', '') if code_info else ''

        # Insert procedure
        cur.execute("""
            INSERT INTO procedures (code, code_type, description)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (code, code_type, description))

        proc_result = cur.fetchone()
        if proc_result:
            procedure_id = proc_result[0]
        else:
            cur.execute("""
                SELECT id FROM procedures WHERE description = %s
            """, (description,))
            row = cur.fetchone()
            if not row:
                skipped += 1
                continue
            procedure_id = row[0]

        for charge in item.get('standard_charges', []):

            # Cash price
            cash_price = charge.get('discounted_cash')
            if cash_price:
                price_batch.append((hospital_id, procedure_id, None, 'cash', float(cash_price), url, date.today()))

            # Chargemaster price
            gross = charge.get('gross_charge')
            if gross:
                price_batch.append((hospital_id, procedure_id, None, 'chargemaster', float(gross), url, date.today()))

            # Negotiated rates
            for payer_info in charge.get('payers_information', []):
                payer_name = payer_info.get('payer_name', '')
                plan_name = payer_info.get('plan_name', '')
                price = payer_info.get('standard_charge_dollar')

                if not payer_name or not price:
                    continue

                cur.execute("""
                    INSERT INTO payers (name, plan_name)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """, (payer_name, plan_name))

                payer_result = cur.fetchone()
                if payer_result:
                    payer_id = payer_result[0]
                else:
                    cur.execute("""
                        SELECT id FROM payers WHERE name = %s AND plan_name = %s
                    """, (payer_name, plan_name))
                    payer_id = cur.fetchone()[0]

                price_batch.append((hospital_id, procedure_id, payer_id, 'negotiated', float(price), url, date.today()))

        # Flush batch every 500 rows
        if len(price_batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO prices (hospital_id, procedure_id, payer_id, price_type, price, source_file_url, recorded_at)
                VALUES %s
            """, price_batch)
            price_batch = []

        inserted += 1

    except Exception as e:
        print(f"Skipped record due to error: {e}")
        skipped += 1
        continue

# Insert any remaining rows
if price_batch:
    psycopg2.extras.execute_values(cur, """
        INSERT INTO prices (hospital_id, procedure_id, payer_id, price_type, price, source_file_url, recorded_at)
        VALUES %s
    """, price_batch)

conn.commit()
cur.close()
conn.close()

print(f"Done. {inserted} procedures inserted, {skipped} skipped.")
