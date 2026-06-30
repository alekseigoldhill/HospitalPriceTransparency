-- Migration 003: Import Log
-- Tracks progress of hospital data imports
CREATE TABLE IF NOT EXISTS import_log (
  id               SERIAL PRIMARY KEY,
  hospital_id      INT REFERENCES hospitals(id),
  status           TEXT NOT NULL,
  procedures_total INT,
  procedures_done  INT DEFAULT 0,
  started_at       TIMESTAMPTZ DEFAULT NOW(),
  finished_at      TIMESTAMPTZ,
  error_message    TEXT
);
