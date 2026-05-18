-- =============================================================
-- BhumiDrishti — Add orthophoto-specific columns to assessments
-- Run after create_assessments_table.sql
-- =============================================================

ALTER TABLE assessments
  ADD COLUMN IF NOT EXISTS site_name         VARCHAR(200),
  ADD COLUMN IF NOT EXISTS pre_chip_path     VARCHAR(500),
  ADD COLUMN IF NOT EXISTS building_area_m2  FLOAT,
  ADD COLUMN IF NOT EXISTS building_width_m  FLOAT,
  ADD COLUMN IF NOT EXISTS building_height_m FLOAT;

COMMENT ON COLUMN assessments.site_name         IS 'batch site name from field worker';
COMMENT ON COLUMN assessments.pre_chip_path     IS 'path to pre-earthquake chip with building outline overlay';
COMMENT ON COLUMN assessments.building_area_m2  IS 'building footprint area in m² from PostGIS';
COMMENT ON COLUMN assessments.building_width_m  IS 'approximate building bounding box width in metres';
COMMENT ON COLUMN assessments.building_height_m IS 'approximate building bounding box height in metres';

ALTER TABLE assessments
  ALTER COLUMN geom TYPE GEOMETRY(Geometry, 4326)
  USING ST_SetSRID(geom, 4326);

CREATE OR REPLACE FUNCTION update_assessment_geom()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.geom IS NULL AND NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL THEN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326);
  ELSIF NEW.geom IS NOT NULL AND ST_SRID(NEW.geom) = 0 THEN
    NEW.geom = ST_SetSRID(NEW.geom, 4326);
  END IF;

  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
