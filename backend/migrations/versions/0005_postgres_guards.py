"""database-level guards (PostgreSQL): append-only evidence and immutable published snapshots

Defence in depth for app/immutability.py (Phase H2): the same rules enforced by triggers, so they also hold for raw
SQL, other clients and bugs that bypass the ORM. The one sanctioned exception is the audited retention purge
(app/retention.py), which sets `hornet.retention_purge = on` for its own transaction (SET LOCAL) and may then only
DELETE from the append-only tables, never UPDATE them. SQLite (development, tests) relies on the ORM guards alone.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

APPEND_ONLY = ("execution_observation", "ai_synthesis", "session_event", "gateway_event", "admin_audit_event")
SNAPSHOT_OWNED = ("virtual_node", "service_profile", "twin_image")

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION hornet_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND TG_TABLE_NAME <> 'admin_audit_event'
     AND current_setting('hornet.retention_purge', true) = 'on' THEN
    RETURN OLD;  -- the audited retention purge, inside its own transaction
  END IF;
  RAISE EXCEPTION 'hornet: % is append-only (% refused)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'P0001';
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hornet_snapshot_guard() RETURNS trigger AS $$
BEGIN
  IF OLD.status IN ('published', 'archived') THEN
    IF TG_OP = 'UPDATE' AND OLD.status = 'published' AND NEW.status = 'archived'
       AND (OLD.id, OLD.version, OLD.profile_hash, OLD.validation_report, OLD.created_at, OLD.published_at)
           IS NOT DISTINCT FROM
           (NEW.id, NEW.version, NEW.profile_hash, NEW.validation_report, NEW.created_at, NEW.published_at) THEN
      RETURN NEW;  -- publishing a newer version archives this one: the only change allowed
    END IF;
    RAISE EXCEPTION 'hornet: published snapshots are immutable (% refused)', TG_OP USING ERRCODE = 'P0001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hornet_snapshot_owned_guard() RETURNS trigger AS $$
DECLARE
  snap_id uuid;
  snap_status text;
BEGIN
  snap_id := CASE WHEN TG_OP = 'INSERT' THEN NEW.snapshot_id ELSE OLD.snapshot_id END;
  SELECT status INTO snap_status FROM environment_snapshot WHERE id = snap_id;
  IF snap_status IN ('published', 'archived')
     OR (TG_OP = 'UPDATE' AND NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id) THEN
    RAISE EXCEPTION 'hornet: % belongs to a published snapshot and is immutable (% refused)', TG_TABLE_NAME, TG_OP
      USING ERRCODE = 'P0001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite: the ORM guards in app/immutability.py apply
    op.execute(FUNCTIONS)
    for table in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER hornet_append_only BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION hornet_append_only()")
    op.execute("CREATE TRIGGER hornet_snapshot_guard BEFORE UPDATE OR DELETE ON environment_snapshot "
               "FOR EACH ROW EXECUTE FUNCTION hornet_snapshot_guard()")
    for table in SNAPSHOT_OWNED:
        op.execute(f"CREATE TRIGGER hornet_snapshot_owned_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION hornet_snapshot_owned_guard()")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS hornet_append_only ON {table}")
    op.execute("DROP TRIGGER IF EXISTS hornet_snapshot_guard ON environment_snapshot")
    for table in SNAPSHOT_OWNED:
        op.execute(f"DROP TRIGGER IF EXISTS hornet_snapshot_owned_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS hornet_append_only(); DROP FUNCTION IF EXISTS hornet_snapshot_guard(); "
               "DROP FUNCTION IF EXISTS hornet_snapshot_owned_guard();")
