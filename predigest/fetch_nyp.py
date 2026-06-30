# ============================================================
# fetch_nyp.py
# Downloads and processes NewYork-Presbyterian pricing data
# from their publicly published machine-readable file.
# ============================================================

import requests
import json
import psycopg2
import os
from datetime import date

# ---- DATABASE CONNECTION ------------------------------------
# This reads your database credentials from environment variables.
# You never hardcode passwords into code.
import urllib.parse
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
# First, make sure NewYork-Presbyterian exists in our hospitals table.
# If it's already there, do nothing. If not, insert it.
cur.execute("""
    INSERT INTO hospitals (name, cms_id, city, borough, state, source_url, last_updated)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (cms_id) DO NOTHING
    RETURNING id
""", (
    "NewYork-Presbyterian Hospital",
    "330101",                          # Official CMS certification number
    "New York",
    "Manhattan",
    "NY",
    "https://www.nyp.org/patients-visitors/billing/billing-pricing-transparency",
    date.today()
))

result = cur.fetchone()
if result:
    hospital_id = result[0]
else:
    cur.execute("SELECT id FROM hospitals WHERE cms_id = '330101'")
    hospital_id = cur.fetchone()[0]

print(f"Hospital ID: {hospital_id}")

# ---- DOWNLOAD PRICING FILE ----------------------------------
# This is the official machine-readable file NYP is required
# by federal law to publish.
print("Downloading NewYork-Presbyterian pricing file...")

import zipfile
import io

url = "https://nyp.widen.net/content/hisgjrgpuk/original/133957095_NewYork-Presbyterian-Hospital_standardcharges.json.zip?u=n8xzey&download=true"
print("Downloading NewYork-Presbyterian pricing file...")
response = requests.get(url, timeout=120)

# The file is a ZIP — extract the JSON inside it
with zipfile.ZipFile(io.BytesIO(response.content)) as z:
    json_filename = [f for f in z.namelist() if f.endswith('.json')][0]
    with z.open(json_filename) as f:
        data = json.load(f)

print(f"File downloaded. Processing records...")
if data:
    print(f"First record sample: {list(data[0].keys()) if isinstance(data, list) else list(data.keys())}")

# ---- PROCESS PRICES -----------------------------------------
# NYP uses the new CMS 2024 format.
# Pricing data lives inside data[0]['standard_charge_information']
inserted = 0
skipped = 0

charge_items = data[0].get('standard_charge_information', [])
print(f"Found {len(charge_items)} charge items to process.")

for item in charge_items:
    try:
        description = item.get('description', '')
        if not description:
            skipped += 1
            continue

        # Get procedure code from code_information list
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
                SELECT id FROM procedures
                WHERE description = %s
            """, (description,))
            row = cur.fetchone()
            if not row:
                skipped += 1
                continue
            procedure_id = row[0]

        # Process each standard charge
        for charge in item.get('standard_charges', []):

            # Cash price
            cash_price = charge.get('discounted_cash')
            if cash_price:
                cur.execute("""
                    INSERT INTO prices
                    (hospital_id, procedure_id, payer_id, price_type, price,
                     source_file_url, recorded_at)
                    VALUES (%s, %s, NULL, 'cash', %s, %s, %s)
                """, (hospital_id, procedure_id, float(cash_price), url, date.today()))

            # Chargemaster (gross) price
            gross = charge.get('gross_charge')
            if gross:
                cur.execute("""
                    INSERT INTO prices
                    (hospital_id, procedure_id, payer_id, price_type, price,
                     source_file_url, recorded_at)
                    VALUES (%s, %s, NULL, 'chargemaster', %s, %s, %s)
                """, (hospital_id, procedure_id, float(gross), url, date.today()))

            # Negotiated rates by payer
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
                        SELECT id FROM payers
                        WHERE name = %s AND plan_name = %s
                    """, (payer_name, plan_name))
                    payer_id = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO prices
                    (hospital_id, procedure_id, payer_id, price_type, price,
                     source_file_url, recorded_at)
                    VALUES (%s, %s, %s, 'negotiated', %s, %s, %s)
                """, (hospital_id, procedure_id, payer_id, float(price), url, date.today()))

        inserted += 1

    except Exception as e:
        print(f"Skipped record due to error: {e}")
        skipped += 1
        continue

# ---- SAVE EVERYTHING ----------------------------------------
conn.commit()
cur.close()
conn.close()

print(f"Done. {inserted} procedures inserted, {skipped} skipped.")d.")
