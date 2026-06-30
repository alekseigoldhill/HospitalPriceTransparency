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

url = "https://www.nyp.org/patientbill/NYP_standardcharges.json"
response = requests.get(url, timeout=120)
data = response.json()

print(f"File downloaded. Processing records...")

# ---- PROCESS PRICES -----------------------------------------
inserted = 0
skipped = 0

for item in data:
    try:
        # Get procedure details
        code = item.get("code", "")
        code_type = item.get("code_type", "")
        description = item.get("description", "")

        if not description:
            skipped += 1
            continue

        # Insert procedure if it doesn't exist yet
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
                WHERE code = %s AND code_type = %s
            """, (code, code_type))
            procedure_id = cur.fetchone()[0]

        # Insert cash price
        cash_price = item.get("cash_price")
        if cash_price:
            cur.execute("""
                INSERT INTO prices
                (hospital_id, procedure_id, payer_id, price_type, price,
                 source_file_url, recorded_at)
                VALUES (%s, %s, NULL, 'cash', %s, %s, %s)
            """, (hospital_id, procedure_id, float(cash_price), url, date.today()))

        # Insert negotiated rates by payer
        payer_rates = item.get("payer_specific_negotiated_rates", [])
        for rate in payer_rates:
            payer_name = rate.get("payer_name", "")
            plan_name = rate.get("plan_name", "")
            price = rate.get("negotiated_rate")

            if not payer_name or not price:
                continue

            # Insert payer if new
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

            # Insert negotiated price
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

print(f"Done. {inserted} procedures inserted, {skipped} skipped.")
