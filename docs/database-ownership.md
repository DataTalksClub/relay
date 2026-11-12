# Database ownership and deploy preflight

Relay prod uses database `relay` and migration/login role `relay_app`. DEV
uses database and role `relay_dev`. The database itself may be owned by the
local administrator. Every object in `public` must be owned by the migration
role, including restored tables, partitions, indexes, sequences, views and
routines. DML grants and REFERENCES grants do not allow ALTER TABLE.

## Restore/bootstrap

Create a new database/schema with CREATE privileges for the migration role,
then restore as that role, for example:

```sh
pg_restore --no-owner --no-acl --role=relay_app --dbname=relay backup.dump
```

The connection must be the migration role or an administrator permitted to
SET ROLE to it. For DEV substitute `relay_dev` for both database and role.
Do not restore as `pgadmin` without explicitly selecting the migration role.
Run the candidate image's `db_preflight` before changing serving containers.

Audit as local administrator or migration role (the catalog is readable by both):

```sql
SELECT kind, owner, count(*) FROM (
  SELECT c.relkind::text AS kind, pg_get_userbyid(c.relowner) AS owner
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public'
  UNION ALL
  SELECT p.prokind::text, pg_get_userbyid(p.proowner)
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public'
) objects GROUP BY kind, owner ORDER BY kind, owner;
```

Kinds: `r` table, `p` partitioned table, `S` sequence, `i` index, `I`
partitioned index, `v` view, `m` materialized view, `f` foreign table or
function (catalog-dependent), `a` aggregate, `w` window function. Attached
partitions are included. No routines are present as of 2026-10-24.

For an existing restore, first save the audit and review mismatches. Use the
local PostgreSQL admin through `docker exec` on the Coolify pg-relay host;
never log database URLs/passwords. The following alignment changes ownership
only, without deleting objects or data. Set `target_role` for the correct DB.
Do not run it blindly when the audit is already aligned.

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
DECLARE
  target_role text := 'relay_app'; -- DEV: relay_dev
  item record;
  object_type text;
BEGIN
  FOR item IN
    SELECT c.relkind, n.nspname, c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p','f','v','m','S')
      AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname=target_role)
    ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END, c.oid
  LOOP
    object_type := CASE item.relkind
      WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
      WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE'
      ELSE 'TABLE' END;
    EXECUTE format('ALTER %s %I.%I OWNER TO %I',
                   object_type, item.nspname, item.relname, target_role);
  END LOOP;
  -- Index ownership follows its table; PostgreSQL does not support a
  -- separate ALTER INDEX OWNER. Each partition is included above.
  FOR item IN
    SELECT p.oid, p.prokind, n.nspname, p.proname,
           pg_get_function_identity_arguments(p.oid) AS arguments
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public'
      AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname=target_role)
  LOOP
    object_type := CASE item.prokind WHEN 'p' THEN 'PROCEDURE'
                   WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END;
    EXECUTE format('ALTER %s %I.%I(%s) OWNER TO %I', object_type,
                   item.nspname, item.proname, item.arguments, target_role);
  END LOOP;
END $$;
COMMIT;
```

Re-run the audit and then, **as the migration role**:

```sh
uv run python manage.py db_preflight --expected-role relay_app --expected-database relay
```

## Deployment behavior

Both Contabo workflows run `scripts/run_db_preflight.sh` before any Coolify
compose PATCH or deploy call. A restricted SSH command starts the exact
candidate image with only DATABASE_URL and SECRET_KEY from the serving web
container. It overrides the normal image entrypoint to check ownership before
any migration, confirms current_user/session_user/database, and lists each
mismatched public object. It then runs `migrate --plan`, applies migrations
once, checks for unapplied migrations, and rechecks ownership. The shared
PostgreSQL advisory lock serializes this entire operation with all container
startup migration gates. An up-to-date database exits 0 with no DDL.

A failed preflight exits the workflow before changing Coolify configuration
or replacing containers. Old containers continue serving. Fix the named
objects or failed migration and rerun the workflow. Schema/data migrations
must remain compatible with the older serving release; PostgreSQL cannot
automatically reverse committed migrations if a later deploy step fails.

The startup gate remains a second defense for all roles and container
restarts. Deploy through these workflows; a direct Coolify UI deploy skips
the workflow preflight. A rollback image older than `db_preflight` fails
closed; first rebuild that source with the preflight command before promotion.

Coolify can start a service process before attaching its shared database
network. Before acquiring the advisory lock, the startup gate retries transient
DNS/connection-refused/startup errors up to 30 times, two seconds apart, without
logging connection details. Authentication errors fail immediately. Failed
migration operations are never retried by this wait. This absorbs the observed
first-connection DNS gap without restarting otherwise healthy containers.

## Host runner installation and credentials

Install `scripts/relay_db_preflight_host.py` as root-owned, mode 0755,
`/usr/local/sbin/relay-db-preflight` on contabo-b. Its fixed allowlist maps
development/production to the existing Relay web containers, DBs and roles.
It permits only immutable Relay release tags, uses a private temporary Docker
configuration for registry login, and removes temporary credentials on exit.
It never stops or restarts a container.

The same restricted runner accepts `ENVIRONMENT inspect` for deployment
verification. This read-only command returns the host observation time and
the actual Docker container IDs, creation times, images and states for only
that environment's Relay compose services. It omits container environment
variables and all other services. Install the updated runner before deploying
workflows that call `scripts/run_runtime_verify.sh`.

Before the Coolify PATCH/deploy, the workflow records a baseline through this
command. The shared verifier requires every service in the compose template
to have a new container ID, a creation time after that baseline, the exact
pinned image, a running state and a healthy healthcheck when one exists.
It also requires Coolify's settled service status and the public readiness
endpoint to return the exact baked tag and source SHA. This prevents desired
image metadata, old workers, an empty API application list or a same-tag
redeploy from reporting success before replacement. Polling uses a 300-second
monotonic deadline and five-second intervals; public polls bypass caches.

Use a separate ed25519 SSH key for each GitHub environment. Install its public
key in the deploy user's authorized_keys with this forced command (replace
ENVIRONMENT with development or production):

```text
restrict,command="sudo -n /usr/local/sbin/relay-db-preflight ENVIRONMENT \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC_KEY
```

The forced command restricts each key to its own environment and rejects
arbitrary commands. Store the private key as `RELAY_PREFLIGHT_SSH_KEY` in
that GitHub environment. Store `RELAY_PREFLIGHT_TARGET` (user@host) and
`RELAY_PREFLIGHT_KNOWN_HOSTS` (host key verified through the existing admin
SSH connection) as repository secrets. Never use trust-on-first-use in CI.
The production job retains the production environment's required reviewer.

The DEV workflow runs real PostgreSQL integration tests against a disposable
database: two concurrent migration processes on an empty schema, repeated
no-op starts, and an ownership drift fixture covering partitions, indexes,
sequences, a view and a function. Unit tests cover role/DB mismatch,
ownership errors, migration failure, lock release and pipeline ordering.
