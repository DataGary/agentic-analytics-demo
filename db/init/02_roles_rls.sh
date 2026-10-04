#!/usr/bin/env bash
# Creates the least-privilege roles and the row-level security policies.
# A .sh (not .sql) so passwords come from the environment instead of being committed.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v mcp_reader_pw="$MCP_READER_PASSWORD" \
  -v cube_reader_pw="$CUBE_READER_PASSWORD" \
  -v audit_writer_pw="$AUDIT_WRITER_PASSWORD" <<'SQL'

-- mcp_reader: used by the run_sql tool. Cannot bypass RLS, read-only, only sees analytics.
CREATE ROLE mcp_reader LOGIN NOBYPASSRLS PASSWORD :'mcp_reader_pw';
-- cube_reader: used by Cube. Cube enforces scope itself (cube.js queryRewrite) because it pools
-- connections across users and can't set per-user session variables.
CREATE ROLE cube_reader LOGIN NOBYPASSRLS PASSWORD :'cube_reader_pw';
-- audit_writer: the MCP server writes audit rows with this role, INSERT only.
CREATE ROLE audit_writer LOGIN PASSWORD :'audit_writer_pw';

GRANT USAGE ON SCHEMA analytics TO mcp_reader, cube_reader;
GRANT SELECT ON analytics.regions, analytics.stores, analytics.products, analytics.sales
  TO mcp_reader, cube_reader;
-- Raw customers (with email) is NOT granted to mcp_reader; it only gets the masked view below.
GRANT SELECT ON analytics.customers TO cube_reader;

ALTER ROLE mcp_reader SET default_transaction_read_only = on;
ALTER ROLE mcp_reader SET statement_timeout = '5s';
ALTER ROLE mcp_reader SET search_path = analytics;
ALTER ROLE cube_reader SET default_transaction_read_only = on;

-- ---------------------------------------------------------------------------
-- Row-level security.
-- The MCP server sets these per transaction from the verified JWT:
--   SET LOCAL app.row_scope = 'all' | 'region'
--   SET LOCAL app.region_id = '<id>'
--   SET LOCAL app.mask_pii  = 'true' | 'false'
-- If they're missing, current_setting(..., true) returns NULL and the policy returns no rows (fails closed).
-- ---------------------------------------------------------------------------
CREATE FUNCTION analytics.row_visible(row_region int) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT current_setting('app.row_scope', true) = 'all'
      OR (current_setting('app.row_scope', true) = 'region'
          AND row_region = nullif(current_setting('app.region_id', true), '')::int)
$$;

ALTER TABLE analytics.sales     ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.stores    ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.customers ENABLE ROW LEVEL SECURITY;

CREATE POLICY mcp_region ON analytics.sales     FOR SELECT TO mcp_reader USING (analytics.row_visible(region_id));
CREATE POLICY mcp_region ON analytics.stores    FOR SELECT TO mcp_reader USING (analytics.row_visible(region_id));
CREATE POLICY mcp_region ON analytics.customers FOR SELECT TO mcp_reader USING (analytics.row_visible(region_id));

CREATE POLICY cube_all ON analytics.sales     FOR SELECT TO cube_reader USING (true);
CREATE POLICY cube_all ON analytics.stores    FOR SELECT TO cube_reader USING (true);
CREATE POLICY cube_all ON analytics.customers FOR SELECT TO cube_reader USING (true);

-- Masked customer view. It runs with the owner's rights (so mcp_reader needn't see raw email),
-- so it must apply the region filter itself. security_barrier stops predicate push-down leaks.
CREATE VIEW analytics.customers_v WITH (security_barrier) AS
SELECT id,
       name,
       CASE WHEN current_setting('app.mask_pii', true) = 'false' THEN email
            ELSE regexp_replace(email, '^(.).*(@.*)$', '\1***\2') END AS email,
       region_id
FROM analytics.customers
WHERE analytics.row_visible(region_id);

GRANT SELECT ON analytics.customers_v TO mcp_reader;

-- Separate database for LangGraph conversation checkpoints (agent memory).
CREATE DATABASE agent_state OWNER app_owner;
SQL
