#!/usr/bin/env python3
"""Idempotent database migration script for DGC SMS.

Adds any columns required by the current codebase that are missing from the
existing SQLite database.  Safe to run repeatedly — columns that already
exist are silently skipped.

Usage (from project root):
    python migrate_db.py              # uses instance/dgc_sms.db
    python migrate_db.py /path/to/db  # explicit path
"""

import sqlite3
import sys
import os

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'instance', 'dgc_sms.db'
)

# ---- Schema additions -------------------------------------------------------
# Each entry: (table, column_name, column_definition)

MIGRATIONS = [
    # samples – summary report
    ('samples', 'summary_report', 'TEXT'),
    ('samples', 'summary_report_file', 'VARCHAR(500)'),
    ('samples', 'summary_report_file_original_name', 'VARCHAR(255)'),
    ('samples', 'summary_report_by', 'INTEGER REFERENCES users(id)'),
    ('samples', 'summary_report_at', 'DATETIME'),
    # samples – deputy review
    ('samples', 'deputy_review_comments', 'TEXT'),
    ('samples', 'deputy_reviewed_by', 'INTEGER REFERENCES users(id)'),
    ('samples', 'deputy_reviewed_at', 'DATETIME'),
    # samples – certificate
    ('samples', 'certificate_text', 'TEXT'),
    ('samples', 'certificate_file', 'VARCHAR(500)'),
    ('samples', 'certificate_file_original_name', 'VARCHAR(255)'),
    ('samples', 'certificate_prepared_by', 'INTEGER REFERENCES users(id)'),
    ('samples', 'certificate_prepared_at', 'DATETIME'),
    # samples – HOD review / certification
    ('samples', 'hod_review_comments', 'TEXT'),
    ('samples', 'hod_reviewed_by', 'INTEGER REFERENCES users(id)'),
    ('samples', 'hod_reviewed_at', 'DATETIME'),
    ('samples', 'certified_at', 'DATETIME'),
    ('samples', 'certified_by', 'INTEGER REFERENCES users(id)'),
    # users – force password change on first login
    ('users', 'must_change_password', 'BOOLEAN DEFAULT 0'),
    # users – active flag and creation timestamp
    ('users', 'is_active_user', 'BOOLEAN DEFAULT 1'),
    ('users', 'created_at', 'DATETIME'),
    # sample_assignments – preliminary review
    ('sample_assignments', 'preliminary_review_comments', 'TEXT'),
    ('sample_assignments', 'preliminary_reviewed_by', 'INTEGER REFERENCES users(id)'),
    ('sample_assignments', 'preliminary_reviewed_at', 'DATETIME'),
    ('sample_assignments', 'return_stage', 'VARCHAR(20)'),
    ('sample_assignments', 'preliminary_review_checklist', 'TEXT'),
    # samples – milk type (Food Milk)
    ('samples', 'milk_type', 'VARCHAR(10)'),
    # samples – new type-specific fields
    ('samples', 'volume', 'VARCHAR(100)'),
    ('samples', 'formulation_type', 'VARCHAR(100)'),
    ('samples', 'alcohol_type', 'VARCHAR(100)'),
    ('samples', 'claim_butt_number', 'VARCHAR(100)'),
    # samples – expected report date
    ('samples', 'expected_report_date', 'DATE'),
    # sample_assignments – report return fields
    ('sample_assignments', 'all_samples_returned', 'VARCHAR(10)'),
    ('sample_assignments', 'return_quantity', 'VARCHAR(100)'),
    # sample_assignments – assignment comments and quantity/volume
    ('sample_assignments', 'comments', 'TEXT'),
    ('sample_assignments', 'quantity_volume', 'VARCHAR(100)'),
    # samples – new fields for requirements
    ('samples', 'batch_lot_number', 'VARCHAR(100)'),
    ('samples', 'lot_number', 'VARCHAR(100)'),
    ('samples', 'expiration_date', 'DATE'),
    ('samples', 'toxicology_sample_type_name', 'VARCHAR(100)'),
    # samples – COA reference
    ('samples', 'coa_reference', 'VARCHAR(255)'),
    # sample_history – enhanced audit fields
    ('sample_history', 'action_type', 'VARCHAR(100)'),
    ('sample_history', 'object_affected', 'VARCHAR(255)'),
    ('sample_history', 'change_description', 'TEXT'),
    # users – account lockout (brute-force protection)
    ('users', 'failed_login_attempts', 'INTEGER DEFAULT 0'),
    ('users', 'locked_until', 'DATETIME'),
]

NEW_TABLES = [
    (
        'settings',
        'CREATE TABLE IF NOT EXISTS settings ('
        '  key VARCHAR(120) PRIMARY KEY,'
        '  value VARCHAR(500) NOT NULL DEFAULT ""'
        ')',
    ),
    (
        'user_roles',
        'CREATE TABLE IF NOT EXISTS user_roles ('
        '  user_id INTEGER NOT NULL REFERENCES users(id),'
        '  role VARCHAR(20) NOT NULL,'
        '  PRIMARY KEY (user_id, role)'
        ')',
    ),
    (
        'user_branches',
        'CREATE TABLE IF NOT EXISTS user_branches ('
        '  user_id INTEGER NOT NULL REFERENCES users(id),'
        '  branch VARCHAR(20) NOT NULL,'
        '  PRIMARY KEY (user_id, branch)'
        ')',
    ),
    (
        'notifications',
        'CREATE TABLE IF NOT EXISTS notifications ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  user_id INTEGER NOT NULL REFERENCES users(id),'
        '  title VARCHAR(255) NOT NULL,'
        '  message TEXT NOT NULL,'
        '  link VARCHAR(500),'
        '  is_read BOOLEAN DEFAULT 0,'
        '  email_sent BOOLEAN DEFAULT 0,'
        '  created_at DATETIME'
        ')',
    ),
    (
        'kpi_targets',
        'CREATE TABLE IF NOT EXISTS kpi_targets ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  year INTEGER NOT NULL,'
        '  quarter INTEGER NOT NULL,'
        '  kpi_key VARCHAR(100) NOT NULL,'
        '  target_value FLOAT,'
        '  actual_override FLOAT,'
        '  UNIQUE (year, quarter, kpi_key)'
        ')',
    ),
    (
        'non_working_days',
        'CREATE TABLE IF NOT EXISTS non_working_days ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  date DATE NOT NULL UNIQUE,'
        '  description VARCHAR(255) NOT NULL,'
        '  day_type VARCHAR(50) NOT NULL DEFAULT "holiday",'
        '  created_by INTEGER REFERENCES users(id),'
        '  created_at DATETIME'
        ')',
    ),
    (
        'supporting_documents',
        'CREATE TABLE IF NOT EXISTS supporting_documents ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  sample_id INTEGER NOT NULL REFERENCES samples(id),'
        '  file_path VARCHAR(500) NOT NULL,'
        '  original_name VARCHAR(255) NOT NULL,'
        '  description VARCHAR(500),'
        '  uploaded_by INTEGER NOT NULL REFERENCES users(id),'
        '  uploaded_at DATETIME'
        ')',
    ),
    (
        'document_versions',
        'CREATE TABLE IF NOT EXISTS document_versions ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  sample_id INTEGER NOT NULL REFERENCES samples(id),'
        '  document_type VARCHAR(50) NOT NULL,'
        '  version_number INTEGER NOT NULL DEFAULT 1,'
        '  file_path VARCHAR(500) NOT NULL,'
        '  original_name VARCHAR(255) NOT NULL,'
        '  upload_label VARCHAR(50),'
        '  uploaded_by INTEGER NOT NULL REFERENCES users(id),'
        '  uploaded_at DATETIME,'
        '  assignment_id INTEGER REFERENCES sample_assignments(id)'
        ')',
    ),
    (
        'back_date_requests',
        'CREATE TABLE IF NOT EXISTS back_date_requests ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  sample_id INTEGER NOT NULL REFERENCES samples(id),'
        '  field_name VARCHAR(100) NOT NULL,'
        '  original_date VARCHAR(50) NOT NULL,'
        '  proposed_date VARCHAR(50) NOT NULL,'
        '  reason TEXT,'
        '  requested_by INTEGER NOT NULL REFERENCES users(id),'
        '  requested_at DATETIME,'
        '  status VARCHAR(20) NOT NULL DEFAULT "pending",'
        '  decided_by INTEGER REFERENCES users(id),'
        '  decided_at DATETIME,'
        '  decision_comments TEXT'
        ')',
    ),
    (
        'review_history',
        'CREATE TABLE IF NOT EXISTS review_history ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  sample_id INTEGER NOT NULL REFERENCES samples(id),'
        '  assignment_id INTEGER REFERENCES sample_assignments(id),'
        '  review_type VARCHAR(50) NOT NULL,'
        '  review_number INTEGER NOT NULL DEFAULT 1,'
        '  action VARCHAR(50) NOT NULL,'
        '  reviewer_id INTEGER NOT NULL REFERENCES users(id),'
        '  reviewed_at DATETIME,'
        '  comments TEXT,'
        '  checklist_data TEXT'
        ')',
    ),
    (
        'audit_log',
        'CREATE TABLE IF NOT EXISTS audit_log ('
        '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  action VARCHAR(100) NOT NULL,'
        '  entity_type VARCHAR(100) NOT NULL,'
        '  entity_id INTEGER,'
        '  entity_label VARCHAR(255),'
        '  details TEXT,'
        '  performed_by INTEGER NOT NULL REFERENCES users(id),'
        '  performed_at DATETIME'
        ')',
    ),
]

# ------------------------------------------------------------------------------


def _existing_columns(cursor, table):
    cursor.execute(f'PRAGMA table_info("{table}")')
    return {row[1] for row in cursor.fetchall()}


def migrate(db_path):
    if not os.path.exists(db_path):
        print(f'Database not found at {db_path} — nothing to migrate.')
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Create any missing tables
    for table_name, ddl in NEW_TABLES:
        cur.execute(ddl)
        print(f'  Table  {table_name}: ensured')

    # Add any missing columns
    cache = {}
    for table, col, typedef in MIGRATIONS:
        if table not in cache:
            cache[table] = _existing_columns(cur, table)
        if col in cache[table]:
            continue
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typedef}')
        cache[table].add(col)
        print(f'  Column {table}.{col}: added')

    conn.commit()

    # Migrate data from legacy single-value columns to association tables
    _migrate_roles_branches(cur)

    # Make legacy role/branch columns nullable (they may have NOT NULL from
    # the original schema, but the multi-role system no longer requires them).
    _make_legacy_columns_nullable(cur)

    conn.commit()
    conn.close()
    print('Migration complete.')


def _migrate_roles_branches(cur):
    """Copy role/branch from legacy columns into user_roles/user_branches tables.
    Only inserts rows that don't already exist."""
    # Migrate roles
    cur.execute(
        'SELECT id, role FROM users WHERE role IS NOT NULL'
    )
    for user_id, role in cur.fetchall():
        cur.execute(
            'INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?, ?)',
            (user_id, role),
        )
    print('  Legacy roles migrated to user_roles')

    # Migrate branches
    cur.execute(
        'SELECT id, branch FROM users WHERE branch IS NOT NULL'
    )
    for user_id, branch in cur.fetchall():
        cur.execute(
            'INSERT OR IGNORE INTO user_branches (user_id, branch) VALUES (?, ?)',
            (user_id, branch),
        )
    print('  Legacy branches migrated to user_branches')


def _make_legacy_columns_nullable(cur):
    """Recreate the users table so that the legacy 'role' and 'branch' columns
    are nullable.  SQLite doesn't support ALTER COLUMN, so we have to do the
    standard copy-rename dance.  This is idempotent: if the columns are already
    nullable, the operation is a harmless no-op."""
    # Check whether migration is needed
    cur.execute('PRAGMA table_info("users")')
    cols_info = cur.fetchall()
    needs_fix = False
    for col in cols_info:
        # col = (cid, name, type, notnull, dflt_value, pk)
        if col[1] in ('role', 'branch') and col[3] == 1:  # notnull == 1
            needs_fix = True
            break
    if not needs_fix:
        print('  Legacy columns already nullable — skipping')
        return

    # Build column list from existing schema
    col_names = [col[1] for col in cols_info]
    col_list = ', '.join(f'"{c}"' for c in col_names)

    # Build CREATE TABLE with the same columns but role/branch now nullable
    col_defs = []
    for col in cols_info:
        cid, name, ctype, notnull, dflt, pk = col
        parts = [f'"{name}"', ctype or 'TEXT']
        if pk:
            parts.append('PRIMARY KEY')
        if notnull and name not in ('role', 'branch'):
            parts.append('NOT NULL')
        if dflt is not None:
            parts.append(f'DEFAULT {dflt}')
        col_defs.append(' '.join(parts))

    create_sql = (
        'CREATE TABLE "users_new" (\n  '
        + ',\n  '.join(col_defs)
        + '\n)'
    )

    cur.execute(create_sql)
    cur.execute(f'INSERT INTO "users_new" ({col_list}) SELECT {col_list} FROM "users"')
    cur.execute('DROP TABLE "users"')
    cur.execute('ALTER TABLE "users_new" RENAME TO "users"')
    # Recreate indexes
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)')
    print('  Legacy role/branch columns made nullable')


if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    print(f'Migrating {db_path} ...')
    migrate(db_path)
