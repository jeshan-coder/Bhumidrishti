-- =============================================================
-- BhumiDrishti — Sites Table
-- Tracks user-defined analysis sites and their lifecycle state.
-- =============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS sites (
  id                BIGSERIAL PRIMARY KEY,
  name              VARCHAR(200) NOT NULL,
  boundary          GEOMETRY(Geometry, 4326),
  total_buildings   INTEGER NOT NULL DEFAULT 0,
  status            VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_sites_status CHECK (status IN ('active', 'processing', 'completed'))
);

-- Keep one canonical row per human site name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sites_name_unique
  ON sites (LOWER(name));

CREATE INDEX IF NOT EXISTS idx_sites_status
  ON sites(status);

CREATE INDEX IF NOT EXISTS idx_sites_boundary
  ON sites USING GIST(boundary);

-- Auto-update updated_at.
CREATE OR REPLACE FUNCTION update_sites_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_sites_updated_at ON sites;
CREATE TRIGGER trigger_sites_updated_at
  BEFORE UPDATE ON sites
  FOR EACH ROW
  EXECUTE FUNCTION update_sites_updated_at();

DO $$
BEGIN
  -- Backfill from existing batch names if this is run on old DBs.
  IF to_regclass('public.batches') IS NOT NULL THEN
    INSERT INTO sites (name, boundary, total_buildings, status)
    SELECT
      b.site_name,
      b.area_polygon,
      MAX(COALESCE(b.total_buildings, 0)) AS total_buildings,
      CASE
        WHEN bool_or(b.status = 'processing') OR bool_or(b.status = 'queued') THEN 'processing'
        WHEN bool_or(b.status = 'complete') THEN 'completed'
        ELSE 'active'
      END AS status
    FROM batches b
    WHERE b.site_name IS NOT NULL
    GROUP BY b.site_name
    ON CONFLICT ((LOWER(name))) DO UPDATE
    SET boundary = COALESCE(EXCLUDED.boundary, sites.boundary),
        total_buildings = GREATEST(sites.total_buildings, EXCLUDED.total_buildings),
        status = EXCLUDED.status,
        updated_at = NOW();

    ALTER TABLE batches
      ADD COLUMN IF NOT EXISTS site_id BIGINT;

    UPDATE batches b
    SET site_id = s.id
    FROM sites s
    WHERE b.site_id IS NULL
      AND LOWER(b.site_name) = LOWER(s.name);
  END IF;

  IF to_regclass('public.assessments') IS NOT NULL THEN
    ALTER TABLE assessments
      ADD COLUMN IF NOT EXISTS site_id BIGINT;

    IF to_regclass('public.batches') IS NOT NULL THEN
      UPDATE assessments a
      SET site_id = b.site_id
      FROM batches b
      WHERE a.site_id IS NULL
        AND a.batch_id IS NOT NULL
        AND a.batch_id = b.id;
    END IF;
  END IF;
END $$;

-- Add/refresh FK and indexes after backfill.
DO $$
BEGIN
  IF to_regclass('public.batches') IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE constraint_name = 'fk_batches_site'
      AND table_name = 'batches'
  ) THEN
    ALTER TABLE batches
      ADD CONSTRAINT fk_batches_site
      FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE SET NULL;
  END IF;

  IF to_regclass('public.assessments') IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE constraint_name = 'fk_assessments_site'
      AND table_name = 'assessments'
  ) THEN
    ALTER TABLE assessments
      ADD CONSTRAINT fk_assessments_site
      FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE SET NULL;
  END IF;
END $$;

DO $$
BEGIN
  IF to_regclass('public.batches') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS idx_batches_site_id
      ON batches(site_id);
  END IF;
  IF to_regclass('public.assessments') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS idx_assessments_site_id
      ON assessments(site_id);
  END IF;
END $$;
