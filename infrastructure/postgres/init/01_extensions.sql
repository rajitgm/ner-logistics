-- Runs once, the first time the postgres container initialises an empty data
-- directory. Two jobs only.
--
-- 1. Create the extensions. The Alembic revision also creates them with
--    IF NOT EXISTS so a native (non-Docker) database works the same way, but
--    doing it here means the migration does not need a superuser role.
--
-- 2. Nothing else. Tables, enums and indexes belong to Alembic; a schema
--    defined in two places drifts in two directions.

\echo 'NER Logistics: enabling PostGIS and pgcrypto'

-- Geometry types, spatial indexing and the ST_* functions the risk and routing
-- engines rely on.
CREATE EXTENSION IF NOT EXISTS postgis;

-- gen_random_uuid(), the server-side default for every primary key. UUIDs are
-- what let a field device mint an id offline and sync it later without
-- collision. On PostgreSQL 13+ pgcrypto is not strictly required for
-- gen_random_uuid(), but declaring it keeps the dependency explicit.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Trigram indexes for the "find a road or district by partial name" search in
-- the command centre. Cheap to enable now, awkward to add mid-demo.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Deliberately NOT enabled:
--   postgis_raster    -- no raster analysis in scope; large and unused
--   postgis_topology  -- the road network is modelled as segments, not topology
--   pgrouting         -- routing runs in the application over provider output,
--                        so scoring can weigh reliability and risk rather than
--                        distance alone; an in-database Dijkstra would have to
--                        be reimplemented to do that anyway

SELECT
    current_database() AS database,
    postgis_full_version() AS postgis;
