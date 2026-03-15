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
    # sample_assignments – preliminary review
    ('sample_assignments', 'preliminary_review_comments', 'TEXT'),
    ('sample_assignments', 'preliminary_reviewed_by', 'INTEGER REFERENCES users(id)'),
    ('sample_assignments', 'preliminary_reviewed_at', 'DATETIME'),
    ('sample_assignments', 'return_stage', 'VARCHAR(20)'),
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


if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    print(f'Migrating {db_path} ...')
    migrate(db_path)
