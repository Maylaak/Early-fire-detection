-- Read-only role for the web app. The detection service connects as "gateway"
-- (owner, read/write); the web app connects as "webapp_ro", so the database
-- itself enforces that the web app can only read.
--
-- Runs once, when the Postgres volume is first created.

CREATE ROLE webapp_ro LOGIN PASSWORD 'webapp_ro';
GRANT CONNECT ON DATABASE wildfire TO webapp_ro;
GRANT USAGE ON SCHEMA public TO webapp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO webapp_ro;

-- Tables are created later by the detection service (as "gateway").
-- Make sure every one of them is readable by the web role automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE gateway IN SCHEMA public
    GRANT SELECT ON TABLES TO webapp_ro;
