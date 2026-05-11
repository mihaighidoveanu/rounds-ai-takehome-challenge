-- Read-only role for the analytics agent.
-- Defense-in-depth: even if the SQL parser is bypassed, write attempts fail at the DB layer.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        CREATE ROLE analyst_ro LOGIN PASSWORD 'analyst_ro';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE analytics TO analyst_ro;
GRANT USAGE ON SCHEMA public TO analyst_ro;
GRANT SELECT ON apps TO analyst_ro;
GRANT SELECT ON daily_metrics TO analyst_ro;
