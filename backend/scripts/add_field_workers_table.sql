-- =============================================================
-- BhumiDrishti — Field Workers Table
-- Tracks dispatch availability for assignment workflows.
-- =============================================================

CREATE TABLE IF NOT EXISTS field_workers (
  id                    BIGSERIAL PRIMARY KEY,
  name                  VARCHAR(100) NOT NULL,
  status                VARCHAR(20)  NOT NULL DEFAULT 'available',
  current_assessment_id VARCHAR(50),
  current_site_name     VARCHAR(200),
  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_field_workers_status CHECK (status IN ('available', 'busy'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_field_workers_name_unique
  ON field_workers (LOWER(name));

CREATE INDEX IF NOT EXISTS idx_field_workers_status
  ON field_workers (status);

CREATE OR REPLACE FUNCTION update_field_workers_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_field_workers_updated_at ON field_workers;
CREATE TRIGGER trigger_field_workers_updated_at
  BEFORE UPDATE ON field_workers
  FOR EACH ROW
  EXECUTE FUNCTION update_field_workers_updated_at();

-- Backfill from existing assessment worker names.
INSERT INTO field_workers (name, status)
SELECT DISTINCT TRIM(worker_name), 'available'
FROM assessments
WHERE worker_name IS NOT NULL
  AND TRIM(worker_name) <> ''
ON CONFLICT ((LOWER(name))) DO NOTHING;
