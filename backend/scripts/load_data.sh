#!/bin/bash

# =============================================================
# BhumiDrishti — GIS Data Loader
# Loads all vector GIS data into PostGIS
#
# Tables created:
#   turkey_lines         — roads + waterways (Adiyaman + Hatay)
#   turkey_points        — amenities + facilities (Adiyaman + Hatay)
#   turkey_provinces     — all 81 Turkey provinces
#   turkey_districts_pts — Turkey district point data
#   destroyed_buildings  — earthquake destroyed buildings (Adiyaman + Hatay)
#   turkey_buildings     — OSM building polygons (Adiyaman + Hatay)
#   flood_zones          — derived from waterway buffer (300m)
#   assessments          — empty table, app writes here at runtime
#
# ALL original attributes preserved — nothing deleted or changed
# province column added to turkey_lines, turkey_points,
# destroyed_buildings, and turkey_buildings
# =============================================================

set -euo pipefail

# ── Database config ───────────────────────────────────────────
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-bhumidrishti}"
DB_USER="${DB_USER:-bhumidrishti}"
DB_PASS="${DB_PASSWORD:-bhumidrishti}"
PG_CONN="PG:host=${DB_HOST} port=${DB_PORT} dbname=${DB_NAME} user=${DB_USER} password=${DB_PASS}"
OGR_SOURCE_ENCODING="${OGR_SOURCE_ENCODING:-CP1254}"

# ── Data paths ────────────────────────────────────────────────
DATA_ROOT="/app/data/turkey_data"
DATA_BASE="/app/data"

ADIYAMAN_LINES="${DATA_ROOT}/Adiyaman/gis_data/line_data_adiyaman.shp"
ADIYAMAN_POINTS="${DATA_ROOT}/Adiyaman/gis_data/points_adiyaman.shp"
ADIYAMAN_DESTROYED="${DATA_ROOT}/Adiyaman/destroyed_buildings/adiyaman_destroyed_buildings.shp"
ADIYAMAN_BUILDINGS="${DATA_ROOT}/Adiyaman/buildings/buildings.shp"

HATAY_LINES="${DATA_ROOT}/Hatay/gis_data/line_data.shp"
HATAY_POINTS="${DATA_ROOT}/Hatay/gis_data/point_data.shp"
HATAY_DESTROYED="${DATA_ROOT}/Hatay/destroyed_buildings/hatay_destroyed_buildings.shp"
HATAY_BUILDINGS="${DATA_ROOT}/Hatay/buildings/buildings.shp"

TURKEY_PROVINCES="${DATA_BASE}/turkey_provinces.shp"
TURKEY_DISTRICTS_PTS="${DATA_BASE}/turkey_districts_pts.shp"

# ── Colors ────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "============================================================"
echo "  BhumiDrishti — Loading GIS Data into PostGIS"
echo ""
echo "  turkey_lines         — roads + waterways"
echo "  turkey_points        — amenities + facilities"
echo "  turkey_provinces     — 81 provinces"
echo "  turkey_districts_pts — district points"
echo "  destroyed_buildings  — earthquake destroyed buildings"
echo "  turkey_buildings     — OSM building polygons"
echo "  flood_zones          — derived from waterway buffer"
echo "  assessments          — empty, app writes here"
echo ""
echo "  All original attributes preserved"
echo "============================================================"

# ── Step 0: Verify all source files exist ────────────────────
echo ""
echo "[0/12] Verifying source files..."

for f in \
  "$ADIYAMAN_LINES" \
  "$ADIYAMAN_POINTS" \
  "$ADIYAMAN_DESTROYED" \
  "$ADIYAMAN_BUILDINGS" \
  "$HATAY_LINES" \
  "$HATAY_POINTS" \
  "$HATAY_DESTROYED" \
  "$HATAY_BUILDINGS" \
  "$TURKEY_PROVINCES" \
  "$TURKEY_DISTRICTS_PTS"; do
  if [ ! -f "$f" ]; then
    echo -e "${RED}  ERROR: File not found: $f${NC}"
    exit 1
  else
    echo "  FOUND: $(basename "$f") [$(dirname "$f" | xargs basename)]"
  fi
done

# ── Step 1: Verify PostGIS connection ────────────────────────
echo ""
echo "[1/12] Verifying PostGIS connection..."

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "SELECT PostGIS_Version();" > /dev/null 2>&1 && \
  echo -e "  ${GREEN}PostGIS OK${NC}" || \
  { echo -e "${RED}  ERROR: PostGIS not reachable. Run: docker compose up -d postgres${NC}"; exit 1; }

# ── Step 2: Drop existing tables ─────────────────────────────
echo ""
echo "[2/12] Dropping existing tables for clean reload..."

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "DROP TABLE IF EXISTS turkey_lines CASCADE;
      DROP TABLE IF EXISTS turkey_points CASCADE;
      DROP TABLE IF EXISTS turkey_provinces CASCADE;
      DROP TABLE IF EXISTS turkey_districts_pts CASCADE;
      DROP TABLE IF EXISTS destroyed_buildings CASCADE;
      DROP TABLE IF EXISTS turkey_buildings CASCADE;
      DROP TABLE IF EXISTS flood_zones CASCADE;"

echo "  Done — clean slate (assessments preserved)"

# ── Step 3a: Load Adiyaman lines (creates turkey_lines) ──────
echo ""
echo "[3/12] Loading line data..."
echo ""
echo "  [3a] Adiyaman lines → turkey_lines (creating table)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$ADIYAMAN_LINES" \
  -nln turkey_lines \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Adiyaman lines loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "ALTER TABLE turkey_lines
        ADD COLUMN IF NOT EXISTS province VARCHAR(50);
      UPDATE turkey_lines
        SET province = 'Adiyaman'
        WHERE province IS NULL;"

echo "  Province = Adiyaman set"

# ── Step 3b: Append Hatay lines ───────────────────────────────
echo ""
echo "  [3b] Hatay lines → turkey_lines (appending)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$HATAY_LINES" \
  -nln turkey_lines \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -append \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Hatay lines loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "UPDATE turkey_lines
        SET province = 'Hatay'
        WHERE province IS NULL;"

echo "  Province = Hatay set"

# ── Step 4a: Load Adiyaman points (creates turkey_points) ────
echo ""
echo "[4/12] Loading point data..."
echo ""
echo "  [4a] Adiyaman points → turkey_points (creating table)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$ADIYAMAN_POINTS" \
  -nln turkey_points \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Adiyaman points loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "ALTER TABLE turkey_points
        ADD COLUMN IF NOT EXISTS province VARCHAR(50);
      UPDATE turkey_points
        SET province = 'Adiyaman'
        WHERE province IS NULL;"

echo "  Province = Adiyaman set"

# ── Step 4b: Append Hatay points ──────────────────────────────
echo ""
echo "  [4b] Hatay points → turkey_points (appending)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$HATAY_POINTS" \
  -nln turkey_points \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -append \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Hatay points loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "UPDATE turkey_points
        SET province = 'Hatay'
        WHERE province IS NULL;"

echo "  Province = Hatay set"

# ── Step 5: Load turkey_provinces ────────────────────────────
echo ""
echo "[5/12] Loading turkey_provinces..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$TURKEY_PROVINCES" \
  -nln turkey_provinces \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}turkey_provinces loaded${NC}"

# ── Step 6: Load turkey_districts_pts ────────────────────────
echo ""
echo "[6/12] Loading turkey_districts_pts..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$TURKEY_DISTRICTS_PTS" \
  -nln turkey_districts_pts \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}turkey_districts_pts loaded${NC}"

# ── Step 7a: Load Adiyaman destroyed buildings ───────────────
# Note: shapefile column names are abbreviated (10-char limit)
#   destroyed_  = destroyed:building
#   damage_typ  = damage:type
#   damage_dat  = damage:date
#   damage_eve  = damage:event
# These abbreviated names are kept exactly as-is
echo ""
echo "[7/12] Loading destroyed buildings..."
echo ""
echo "  [7a] Adiyaman destroyed → destroyed_buildings (creating table)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$ADIYAMAN_DESTROYED" \
  -nln destroyed_buildings \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Adiyaman destroyed buildings loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "ALTER TABLE destroyed_buildings
        ADD COLUMN IF NOT EXISTS province VARCHAR(50);
      UPDATE destroyed_buildings
        SET province = 'Adiyaman'
        WHERE province IS NULL;"

echo "  Province = Adiyaman set"

# ── Step 7b: Append Hatay destroyed buildings ─────────────────
echo ""
echo "  [7b] Hatay destroyed → destroyed_buildings (appending)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$HATAY_DESTROYED" \
  -nln destroyed_buildings \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -append \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Hatay destroyed buildings loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "UPDATE destroyed_buildings
        SET province = 'Hatay'
        WHERE province IS NULL;"

echo "  Province = Hatay set"

# ── Step 8a: Load Adiyaman buildings (creates turkey_buildings)
echo ""
echo "[8/12] Loading building polygons..."
echo ""
echo "  [8a] Adiyaman buildings → turkey_buildings (creating table)..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$ADIYAMAN_BUILDINGS" \
  -nln turkey_buildings \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -overwrite \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Adiyaman buildings loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "ALTER TABLE turkey_buildings
        ADD COLUMN IF NOT EXISTS province VARCHAR(50);
      UPDATE turkey_buildings
        SET province = 'Adiyaman'
        WHERE province IS NULL;"

echo "  Province = Adiyaman set"

# ── Step 8b: Append Hatay buildings ───────────────────────────
echo ""
echo "  [8b] Hatay buildings → turkey_buildings (appending)..."
echo "  Note: Hatay has ~47,872 buildings — this may take a few minutes..."

ogr2ogr \
  -f "PostgreSQL" "$PG_CONN" \
  -oo ENCODING="$OGR_SOURCE_ENCODING" \
  "$HATAY_BUILDINGS" \
  -nln turkey_buildings \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco FID=id \
  -lco SPATIAL_INDEX=YES \
  -append \
  -progress \
  --config PG_USE_COPY YES

echo -e "  ${GREEN}Hatay buildings loaded${NC}"

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -c "UPDATE turkey_buildings
        SET province = 'Hatay'
        WHERE province IS NULL;"

echo "  Province = Hatay set"

# ── Step 9: Create all indexes ────────────────────────────────
echo ""
echo "[9/12] Creating indexes..."

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" << 'SQL'

-- turkey_lines
CREATE INDEX IF NOT EXISTS idx_turkey_lines_geom
  ON turkey_lines USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_turkey_lines_highway
  ON turkey_lines(highway);
CREATE INDEX IF NOT EXISTS idx_turkey_lines_waterway
  ON turkey_lines(waterway);
CREATE INDEX IF NOT EXISTS idx_turkey_lines_province
  ON turkey_lines(province);

-- turkey_points
CREATE INDEX IF NOT EXISTS idx_turkey_points_geom
  ON turkey_points USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_turkey_points_amenity
  ON turkey_points(amenity);
CREATE INDEX IF NOT EXISTS idx_turkey_points_shop
  ON turkey_points(shop);
CREATE INDEX IF NOT EXISTS idx_turkey_points_province
  ON turkey_points(province);

-- turkey_provinces
CREATE INDEX IF NOT EXISTS idx_turkey_provinces_geom
  ON turkey_provinces USING GIST(geom);

-- turkey_districts_pts
CREATE INDEX IF NOT EXISTS idx_turkey_districts_pts_geom
  ON turkey_districts_pts USING GIST(geom);

-- destroyed_buildings
CREATE INDEX IF NOT EXISTS idx_destroyed_buildings_geom
  ON destroyed_buildings USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_destroyed_buildings_province
  ON destroyed_buildings(province);
CREATE INDEX IF NOT EXISTS idx_destroyed_buildings_damage_typ
  ON destroyed_buildings(damage_typ);

-- turkey_buildings
CREATE INDEX IF NOT EXISTS idx_turkey_buildings_geom
  ON turkey_buildings USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_turkey_buildings_province
  ON turkey_buildings(province);
CREATE INDEX IF NOT EXISTS idx_turkey_buildings_building
  ON turkey_buildings(building);

SQL

echo -e "  ${GREEN}All indexes created${NC}"

# ── Step 10: Verify all columns preserved ────────────────────
echo ""
echo "[10/12] Verifying columns in all tables..."

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" << 'SQL'

SELECT
  table_name,
  COUNT(*) AS column_count,
  string_agg(column_name, ', ' ORDER BY ordinal_position) AS columns
FROM information_schema.columns
WHERE table_name IN (
  'turkey_lines',
  'turkey_points',
  'turkey_provinces',
  'turkey_districts_pts',
  'destroyed_buildings',
  'turkey_buildings'
)
AND table_schema = 'public'
GROUP BY table_name
ORDER BY table_name;

SQL

# ── Step 11: Create derived tables ───────────────────────────
echo ""
echo "[11/12] Creating derived tables..."
echo ""
echo "  Creating flood_zones from waterway buffer (300m)..."
echo "  Creating empty assessments table..."

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" << 'SQL'

-- flood_zones: buffer all waterways by 300 metres
-- geography cast ensures accurate metre-based distance
DROP TABLE IF EXISTS flood_zones CASCADE;

CREATE TABLE flood_zones AS
SELECT
  osm_id,
  waterway                                                    AS waterway_type,
  name                                                        AS waterway_name,
  province,
  ST_Buffer(geom::geography, 300)::geometry(Geometry, 4326)  AS geom
FROM turkey_lines
WHERE waterway IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_flood_zones_geom
  ON flood_zones USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_flood_zones_province
  ON flood_zones(province);

SQL

# This variable stores whether assessments table already exists.
ASSESSMENTS_EXISTS=$(PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" \
  -tAc "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='assessments');")

if [ "$ASSESSMENTS_EXISTS" = "t" ]; then
  echo "  Preserving existing assessments table and data..."
else
  echo "  assessments table missing — creating full schema..."
  PGPASSWORD="$DB_PASS" psql \
    -h "$DB_HOST" -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" \
    -f /app/scripts/create_assessments_table.sql
fi

# This migration keeps existing assessment rows while ensuring required columns exist.
PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" << 'SQL'

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS damage_description TEXT;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS structural_risk VARCHAR(50);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS building_floors VARCHAR(20);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS building_material VARCHAR(100);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS occupant_status VARCHAR(50);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS action_priority INTEGER;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS elevation_m FLOAT;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS slope_risk VARCHAR(20);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS shelter_type VARCHAR(50);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS nearest_road VARCHAR(200);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS road_distance_m FLOAT;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS province VARCHAR(100);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS district VARCHAR(100);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS address_note TEXT;

SQL

echo -e "  ${GREEN}flood_zones created${NC}"
echo -e "  ${GREEN}assessments table ready (preserved or created)${NC}"

# ── Step 12: Final summary ────────────────────────────────────
echo ""
echo "[12/12] Final summary..."
echo ""

PGPASSWORD="$DB_PASS" psql \
  -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" << 'SQL'

-- Row counts for all 8 tables
SELECT 'turkey_lines'         AS table_name, COUNT(*) AS rows FROM turkey_lines
UNION ALL
SELECT 'turkey_points'        AS table_name, COUNT(*) AS rows FROM turkey_points
UNION ALL
SELECT 'turkey_provinces'     AS table_name, COUNT(*) AS rows FROM turkey_provinces
UNION ALL
SELECT 'turkey_districts_pts' AS table_name, COUNT(*) AS rows FROM turkey_districts_pts
UNION ALL
SELECT 'destroyed_buildings'  AS table_name, COUNT(*) AS rows FROM destroyed_buildings
UNION ALL
SELECT 'turkey_buildings'     AS table_name, COUNT(*) AS rows FROM turkey_buildings
UNION ALL
SELECT 'flood_zones'          AS table_name, COUNT(*) AS rows FROM flood_zones
UNION ALL
SELECT 'assessments'          AS table_name, COUNT(*) AS rows FROM assessments
ORDER BY table_name;

-- Rows per province for all province-tagged tables
SELECT
  'turkey_lines'         AS table_name,
  province, COUNT(*)     AS rows
FROM turkey_lines GROUP BY province
UNION ALL
SELECT
  'turkey_points'        AS table_name,
  province, COUNT(*)     AS rows
FROM turkey_points GROUP BY province
UNION ALL
SELECT
  'destroyed_buildings'  AS table_name,
  province, COUNT(*)     AS rows
FROM destroyed_buildings GROUP BY province
UNION ALL
SELECT
  'turkey_buildings'     AS table_name,
  province, COUNT(*)     AS rows
FROM turkey_buildings GROUP BY province
ORDER BY table_name, province;

-- Building types summary
SELECT building, COUNT(*) AS count
FROM turkey_buildings
WHERE building IS NOT NULL
GROUP BY building
ORDER BY count DESC
LIMIT 10;

-- Flood zones summary
SELECT province, COUNT(*) AS waterway_segments
FROM flood_zones
GROUP BY province;

SQL

echo ""
echo "============================================================"
echo -e "  ${GREEN}SUCCESS — Database is fully loaded and ready${NC}"
echo ""
echo "  turkey_lines         — roads + waterways"
echo "  turkey_points        — amenities + facilities"
echo "  turkey_provinces     — 81 Turkey provinces"
echo "  turkey_districts_pts — district point data"
echo "  destroyed_buildings  — earthquake damaged buildings"
echo "  turkey_buildings     — all OSM building polygons"
echo "  flood_zones          — 300m waterway buffer zones"
echo "  assessments          — empty, app writes here"
echo ""
echo "  All original attributes preserved"
echo "  province column on all data tables"
echo ""
echo "  DATABASE IS READY — start coding the backend"
echo "============================================================"
echo ""