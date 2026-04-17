"""Tests for the data export / import feature."""

import io
import json
import os
import zipfile

import pytest

from app import create_app, db
from app.models import (
    User, Role, Branch, Sample, SampleAssignment, SampleHistory,
    SampleStatus, AssignmentStatus, Setting, Notification,
    KpiTarget, NonWorkingDay, AuditLog, ReviewHistory,
    SupportingDocument, DocumentVersion, BackDateRequest,
    user_roles, user_branches, jamaica_now,
)
from tests.conftest import _create_user, _login


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_admin(app):
    """Create and return an admin user."""
    with app.app_context():
        admin = User(
            username='admin',
            email='admin@test.com',
            first_name='Admin',
            last_name='User',
            must_change_password=False,
        )
        admin.set_password('password123')
        admin.role = Role.ADMIN
        admin.roles = {Role.ADMIN}
        db.session.add(admin)
        db.session.commit()
        return admin


def _create_sample_data(app):
    """Create some sample data to test export/import round-trip."""
    with app.app_context():
        # Officer to own the sample
        officer = User(
            username='officer1',
            email='officer1@test.com',
            first_name='Jane',
            last_name='Officer',
            must_change_password=False,
        )
        officer.set_password('password123')
        officer.role = Role.OFFICER
        officer.roles = {Role.OFFICER}
        officer.branches = {Branch.PHARMACEUTICAL}
        db.session.add(officer)
        db.session.flush()

        # Sample
        from datetime import date
        sample = Sample(
            lab_number='TEST-001',
            sample_name='Test Aspirin',
            sample_type=Branch.PHARMACEUTICAL,
            description='Test sample',
            status=SampleStatus.REGISTERED,
            uploaded_by=officer.id,
            date_received=date(2026, 1, 15),
        )
        db.session.add(sample)
        db.session.flush()

        # History
        history = SampleHistory(
            sample_id=sample.id,
            action='Registered',
            details='Sample registered',
            performed_by=officer.id,
        )
        db.session.add(history)

        # Setting
        Setting.set('email_enabled', 'true')

        # KPI Target
        kpi = KpiTarget(
            year=2026, quarter=1, kpi_key='pharma_coas', target_value=50.0,
        )
        db.session.add(kpi)

        # Non-working day
        nwd = NonWorkingDay(
            date=date(2026, 1, 1),
            description='New Year',
            day_type='holiday',
        )
        db.session.add(nwd)

        db.session.commit()


def _login_admin(client):
    return _login(client, 'admin', 'password123')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExportAccess:
    """Only admins can access export/import."""

    def test_export_requires_login(self, client):
        resp = client.get('/export-data')
        assert resp.status_code in (302, 401)

    def test_export_requires_admin(self, app, client):
        _create_user(role=Role.OFFICER)
        _login(client)
        resp = client.get('/export-data', follow_redirects=True)
        assert b'Access denied' in resp.data

    def test_import_requires_login(self, client):
        resp = client.post('/import-data')
        assert resp.status_code in (302, 401)

    def test_import_requires_admin(self, app, client):
        _create_user(role=Role.OFFICER)
        _login(client)
        resp = client.post('/import-data', follow_redirects=True)
        assert b'Access denied' in resp.data


class TestExport:
    """Test the export endpoint."""

    def test_export_returns_zip(self, app, client):
        _create_admin(app)
        _login_admin(client)
        resp = client.get('/export-data')
        assert resp.status_code == 200
        assert resp.content_type == 'application/zip'
        assert 'dgc_sms_export_' in resp.headers.get(
            'Content-Disposition', ''
        )

    def test_export_contains_data_json(self, app, client):
        _create_admin(app)
        _login_admin(client)
        resp = client.get('/export-data')

        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        assert 'data.json' in zf.namelist()

        data = json.loads(zf.read('data.json'))
        assert 'export_version' in data
        assert data['export_version'] == 1
        assert 'tables' in data
        assert 'row_counts' in data

    def test_export_includes_all_tables(self, app, client):
        _create_admin(app)
        _login_admin(client)
        resp = client.get('/export-data')

        data = json.loads(zipfile.ZipFile(io.BytesIO(resp.data)).read('data.json'))
        expected_tables = {
            'users', 'user_roles', 'user_branches', 'settings',
            'samples', 'sample_assignments', 'sample_history',
            'review_history', 'notifications', 'kpi_targets',
            'non_working_days', 'supporting_documents',
            'document_versions', 'back_date_requests', 'audit_log',
        }
        assert expected_tables == set(data['tables'].keys())

    def test_export_includes_user_data(self, app, client):
        _create_admin(app)
        _create_sample_data(app)
        _login_admin(client)
        resp = client.get('/export-data')

        data = json.loads(zipfile.ZipFile(io.BytesIO(resp.data)).read('data.json'))
        users = data['tables']['users']
        # Admin + officer = 2 users
        assert len(users) == 2
        usernames = {u['username'] for u in users}
        assert 'admin' in usernames
        assert 'officer1' in usernames

    def test_export_includes_samples(self, app, client):
        _create_admin(app)
        _create_sample_data(app)
        _login_admin(client)
        resp = client.get('/export-data')

        data = json.loads(zipfile.ZipFile(io.BytesIO(resp.data)).read('data.json'))
        samples = data['tables']['samples']
        assert len(samples) == 1
        assert samples[0]['lab_number'] == 'TEST-001'

    def test_export_includes_uploaded_files(self, app, client):
        _create_admin(app)
        _login_admin(client)

        # Create a test file in uploads
        upload_folder = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)
        test_file = os.path.join(upload_folder, 'test_doc.pdf')
        with open(test_file, 'wb') as f:
            f.write(b'%PDF-fake-content')

        resp = client.get('/export-data')
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        assert 'uploads/test_doc.pdf' in zf.namelist()
        assert zf.read('uploads/test_doc.pdf') == b'%PDF-fake-content'


class TestImport:
    """Test the import endpoint."""

    def _make_export_zip(self, data_dict, files=None):
        """Build a ZIP file in memory with data.json and optional files."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('data.json', json.dumps(data_dict))
            for arc_name, content in (files or {}).items():
                zf.writestr(arc_name, content)
        buf.seek(0)
        return buf

    def test_import_no_file(self, app, client):
        _create_admin(app)
        _login_admin(client)
        resp = client.post('/import-data', follow_redirects=True)
        assert b'No file selected' in resp.data

    def test_import_non_zip(self, app, client):
        _create_admin(app)
        _login_admin(client)
        data = {'import_file': (io.BytesIO(b'not a zip'), 'data.txt')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert b'Please upload a .zip export file' in resp.data

    def test_import_invalid_zip(self, app, client):
        _create_admin(app)
        _login_admin(client)
        data = {'import_file': (io.BytesIO(b'not a zip'), 'data.zip')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert b'Invalid ZIP file' in resp.data

    def test_import_missing_data_json(self, app, client):
        _create_admin(app)
        _login_admin(client)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('readme.txt', 'hello')
        buf.seek(0)
        data = {'import_file': (buf, 'export.zip')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert b'missing data.json' in resp.data

    def test_import_corrupt_json(self, app, client):
        _create_admin(app)
        _login_admin(client)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('data.json', '{invalid json')
        buf.seek(0)
        data = {'import_file': (buf, 'export.zip')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert b'Corrupt data.json' in resp.data

    def test_import_missing_tables_key(self, app, client):
        _create_admin(app)
        _login_admin(client)
        buf = self._make_export_zip({'export_version': 1})
        data = {'import_file': (buf, 'export.zip')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert b'missing tables key' in resp.data


class TestRoundTrip:
    """Test full export → import round-trip."""

    def test_round_trip_preserves_data(self, app, client):
        """Export data, clear DB, import, verify data is restored."""
        _create_admin(app)
        _create_sample_data(app)
        _login_admin(client)

        # Create a file in uploads
        upload_folder = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)
        test_file = os.path.join(upload_folder, 'round_trip.pdf')
        with open(test_file, 'wb') as f:
            f.write(b'%PDF-round-trip-content')

        # Export
        resp = client.get('/export-data')
        assert resp.status_code == 200
        export_zip = resp.data

        # Verify pre-import state
        with app.app_context():
            assert User.query.count() == 2  # admin + officer
            assert Sample.query.count() == 1
            assert Setting.get('email_enabled') == 'true'

        # Import (this replaces all data)
        data = {
            'import_file': (io.BytesIO(export_zip), 'export.zip'),
        }
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b'imported successfully' in resp.data

        # Verify all data is restored
        with app.app_context():
            assert User.query.count() == 2
            assert Sample.query.count() == 1
            sample = Sample.query.first()
            assert sample.lab_number == 'TEST-001'
            assert sample.sample_name == 'Test Aspirin'
            assert sample.sample_type == Branch.PHARMACEUTICAL
            assert sample.status == SampleStatus.REGISTERED

            assert SampleHistory.query.count() == 1
            assert Setting.get('email_enabled') == 'true'
            assert KpiTarget.query.count() == 1
            assert NonWorkingDay.query.count() == 1

            # Check user roles were restored
            admin = User.query.filter_by(username='admin').first()
            assert admin is not None
            officer = User.query.filter_by(username='officer1').first()
            assert officer is not None

        # Verify uploaded file is restored
        assert os.path.isfile(test_file)
        with open(test_file, 'rb') as f:
            assert f.read() == b'%PDF-round-trip-content'

    def test_import_replaces_existing_data(self, app, client):
        """Import should wipe existing data before restoring."""
        _create_admin(app)
        _login_admin(client)

        # Add extra data that should be wiped
        with app.app_context():
            Setting.set('extra_key', 'extra_value')
            db.session.commit()
            assert Setting.get('extra_key') == 'extra_value'

        # Create a minimal export with just admin user
        with app.app_context():
            export_data = {
                'export_version': 1,
                'exported_at': jamaica_now().isoformat(),
                'tables': {
                    'users': [{
                        'id': 1,
                        'email': 'newadmin@test.com',
                        'username': 'newadmin',
                        'first_name': 'New',
                        'last_name': 'Admin',
                        'password_hash': User.query.first().password_hash,
                        'role': 'Admin',
                        'branch': None,
                        'is_active_user': True,
                        'must_change_password': False,
                        'created_at': None,
                        'failed_login_attempts': 0,
                        'locked_until': None,
                    }],
                    'user_roles': [{'user_id': 1, 'role': 'Admin'}],
                    'user_branches': [],
                    'settings': [{'key': 'imported_key', 'value': 'imported_val'}],
                    'samples': [],
                    'sample_assignments': [],
                    'sample_history': [],
                    'review_history': [],
                    'notifications': [],
                    'kpi_targets': [],
                    'non_working_days': [],
                    'supporting_documents': [],
                    'document_versions': [],
                    'back_date_requests': [],
                    'audit_log': [],
                },
                'row_counts': {},
            }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('data.json', json.dumps(export_data))
        buf.seek(0)

        data = {'import_file': (buf, 'export.zip')}
        resp = client.post('/import-data', data=data,
                           content_type='multipart/form-data',
                           follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            # Old admin should be gone, new admin present
            assert User.query.count() == 1
            assert User.query.first().username == 'newadmin'
            # Old setting gone, new one present
            assert Setting.get('extra_key') == ''
            assert Setting.get('imported_key') == 'imported_val'
