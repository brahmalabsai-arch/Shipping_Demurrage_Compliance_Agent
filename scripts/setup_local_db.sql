-- One-time, idempotent local setup: application role + app database + test database.
-- Run via scripts/setup_local_db.py, which supplies :app_user, :app_password, :app_db and
-- :app_test_db from .env.
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE %I LOGIN', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') \gexec

-- Always re-applied so the role's password tracks .env.
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password') \gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_db') \gexec

-- Disposable database for pytest: the suite rebuilds its schema on every run.
SELECT format('CREATE DATABASE %I OWNER %I', :'app_test_db', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_test_db') \gexec

-- Conventions §3: timestamps in UTC; Asia/Kolkata only at the dashboard layer.
SELECT format('ALTER DATABASE %I SET timezone TO %L', :'app_db', 'UTC') \gexec
SELECT format('ALTER DATABASE %I SET timezone TO %L', :'app_test_db', 'UTC') \gexec
