-- Migration 002: Performance Indexes
CREATE INDEX IF NOT EXISTS idx_procedures_description ON procedures(description);
CREATE INDEX IF NOT EXISTS idx_procedures_code ON procedures(code, code_type);
CREATE INDEX IF NOT EXISTS idx_payers_name ON payers(name, plan_name);
CREATE INDEX IF NOT EXISTS idx_prices_hospital ON prices(hospital_id);
