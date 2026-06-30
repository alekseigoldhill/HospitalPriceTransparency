-- ============================================================
-- Migration 001: Initial Schema
-- Hospital Price Transparency Database
-- ============================================================

-- HOSPITALS
-- One row per hospital. Source URL tracks where we got the data.
CREATE TABLE hospitals (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  cms_id          TEXT UNIQUE,        -- Federal CMS certification number
  address         TEXT,
  city            TEXT,
  borough         TEXT,               -- NYC-specific (Manhattan, Brooklyn, etc.)
  state           TEXT DEFAULT 'NY',
  zip             TEXT,
  latitude        NUMERIC,
  longitude       NUMERIC,
  source_url      TEXT,               -- URL of the hospital's published price file
  last_updated    DATE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- PAYERS
-- One row per insurance company and plan.
CREATE TABLE payers (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL,      -- e.g. "Aetna"
  plan_name       TEXT,               -- e.g. "Aetna PPO Gold"
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- HOSPITAL PAYERS
-- Which insurance plans are accepted at which hospital.
CREATE TABLE hospital_payers (
  id              SERIAL PRIMARY KEY,
  hospital_id     INT REFERENCES hospitals(id),
  payer_id        INT REFERENCES payers(id),
  UNIQUE(hospital_id, payer_id)
);

-- PROCEDURES
-- Medical services stored with both official code and plain English.
CREATE TABLE procedures (
  id              SERIAL PRIMARY KEY,
  code            TEXT,               -- e.g. CPT "71046" or MS-DRG "470"
  code_type       TEXT,               -- 'CPT', 'MS-DRG', 'RC', etc.
  description     TEXT,               -- e.g. "Chest X-Ray, 2 views"
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- PRICES
-- Every price ever recorded. New import = new row. History is preserved.
CREATE TABLE prices (
  id              SERIAL PRIMARY KEY,
  hospital_id     INT REFERENCES hospitals(id),
  procedure_id    INT REFERENCES procedures(id),
  payer_id        INT REFERENCES payers(id),  -- NULL means cash price
  price_type      TEXT,               -- 'cash', 'negotiated', 'chargemaster', 'min', 'max'
  price           NUMERIC,
  source_file_url TEXT,               -- exact file this price came from
  recorded_at     DATE,               -- when the hospital published this price
  imported_at     TIMESTAMPTZ DEFAULT NOW()
);

-- HOSPITAL QUALITY
-- Overall hospital ratings. One row per hospital per source per year.
CREATE TABLE hospital_quality (
  id              SERIAL PRIMARY KEY,
  hospital_id     INT REFERENCES hospitals(id),
  source          TEXT NOT NULL,      -- 'CMS' or 'Leapfrog'
  rating_type     TEXT NOT NULL,      -- 'star_rating' or 'safety_grade'
  rating_value    TEXT NOT NULL,      -- e.g. '4' for CMS stars, 'A' for Leapfrog
  rating_year     INT,
  accreditations  TEXT[],             -- e.g. ['Joint Commission', 'Magnet']
  certifications  TEXT[],             -- e.g. ['Level 1 Trauma Center']
  recorded_at     DATE,
  imported_at     TIMESTAMPTZ DEFAULT NOW()
);

-- PROCEDURE QUALITY
-- Procedure-specific metrics tied to a hospital.
CREATE TABLE procedure_quality (
  id              SERIAL PRIMARY KEY,
  hospital_id     INT REFERENCES hospitals(id),
  procedure_id    INT REFERENCES procedures(id),
  source          TEXT NOT NULL,      -- 'CMS', 'Leapfrog', etc.
  metric          TEXT NOT NULL,      -- e.g. 'complication_rate', 'readmission_rate'
  value           TEXT NOT NULL,      -- the actual score or rate
  rating_year     INT,
  recorded_at     DATE,
  imported_at     TIMESTAMPTZ DEFAULT NOW()
);
