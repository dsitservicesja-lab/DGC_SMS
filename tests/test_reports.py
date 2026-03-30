"""Tests for KPI reports and Pharmaceutical reports."""
from datetime import date, datetime, timezone

from app import db
from app.models import (
    Sample, User, Role, Branch, SampleStatus, KpiTarget,
    KPI_METRICS, AUTO_ACTUAL_KEYS,
)
from tests.conftest import _create_user, _login


def _setup_admin(app):
    """Create an admin user and return the user id."""
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin')
        return admin.id


def _setup_senior(app):
    """Create a senior chemist user and return the user id."""
    with app.app_context():
        sc = _create_user(Role.SENIOR_CHEMIST, Branch.PHARMACEUTICAL,
                          username='senior')
        return sc.id


def _register_pharma_sample(app, lab, name='Test Drug', certified=False):
    """Register a pharmaceutical sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.PHARMACEUTICAL,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.CERTIFIED if certified else SampleStatus.REGISTERED,
        )
        if certified:
            s.certified_at = datetime(2026, 2, 15, tzinfo=timezone.utc)
        db.session.add(s)
        db.session.commit()
        return s.id


# ---------------------------------------------------------------------------
# KPI Report page
# ---------------------------------------------------------------------------

def test_kpi_report_requires_login(app, client):
    resp = client.get('/kpi/report')
    assert resp.status_code == 302
    assert '/auth/login' in resp.headers.get('Location', '')


def test_kpi_report_access_denied_for_chemist(app, client):
    with app.app_context():
        _create_user(Role.CHEMIST, username='chem')
    _login(client, 'chem')
    resp = client.get('/kpi/report', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_kpi_report_renders_for_admin(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    assert b'KPI Report' in resp.data
    assert b'Download CSV' in resp.data
    # Check some KPI labels appear
    assert b'Pharmaceutical' in resp.data
    assert b'toxicology' in resp.data


def test_kpi_report_shows_auto_actuals(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-001', certified=True)
    _login(client, 'admin')
    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    # The certified pharma sample should show as actual = 1
    assert b'1' in resp.data


# ---------------------------------------------------------------------------
# KPI Report CSV download
# ---------------------------------------------------------------------------

def test_kpi_report_download_csv(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/report/download?year=2026&quarter=1')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'KPI,Target,Actual,Variance' in resp.data


# ---------------------------------------------------------------------------
# KPI Targets management
# ---------------------------------------------------------------------------

def test_kpi_targets_access_denied_for_senior(app, client):
    _setup_senior(app)
    _login(client, 'senior')
    resp = client.get('/kpi/targets', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_kpi_targets_renders_for_admin(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi/targets?year=2026&quarter=1')
    assert resp.status_code == 200
    assert b'KPI Targets' in resp.data
    assert b'Save Targets' in resp.data


def test_kpi_targets_save(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.post('/kpi/targets?year=2026&quarter=1', data={
        'year': 2026,
        'quarter': 1,
        'target_pharma_coas': '35',
        'target_milk_coas': '42',
        'target_complaints_resolved': '1',
        'actual_complaints_resolved': '0',
    }, follow_redirects=True)
    assert b'KPI targets saved' in resp.data

    with app.app_context():
        t = KpiTarget.query.filter_by(
            year=2026, quarter=1, kpi_key='pharma_coas'
        ).first()
        assert t is not None
        assert t.target_value == 35.0

        t2 = KpiTarget.query.filter_by(
            year=2026, quarter=1, kpi_key='complaints_resolved'
        ).first()
        assert t2 is not None
        assert t2.target_value == 1.0
        assert t2.actual_override == 0.0


def test_kpi_report_variance_computed(app, client):
    """When targets and actuals both exist, variance should be computed."""
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-V01', certified=True)
    _login(client, 'admin')

    # Set a target
    client.post('/kpi/targets?year=2026&quarter=1', data={
        'year': 2026,
        'quarter': 1,
        'target_pharma_coas': '5',
    }, follow_redirects=True)

    resp = client.get('/kpi/report?year=2026&quarter=1')
    assert resp.status_code == 200
    # actual=1, target=5, variance=-4
    assert b'-4' in resp.data


# ---------------------------------------------------------------------------
# Pharmaceutical Report
# ---------------------------------------------------------------------------

def test_pharma_report_requires_login(app, client):
    resp = client.get('/reports/pharma')
    assert resp.status_code == 302


def test_pharma_report_renders(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-R01', 'Aspirin', certified=True)
    _login(client, 'admin')
    resp = client.get('/reports/pharma?year=2026')
    assert resp.status_code == 200
    assert b'Pharmaceutical Report' in resp.data
    assert b'PH-R01' in resp.data
    assert b'Aspirin' in resp.data


def test_pharma_report_quarter_filter(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-Q01')
    _login(client, 'admin')
    # Q1 should include January samples
    resp = client.get('/reports/pharma?year=2026&quarter=1')
    assert b'PH-Q01' in resp.data
    # Q3 should not include January samples
    resp = client.get('/reports/pharma?year=2026&quarter=3')
    assert b'PH-Q01' not in resp.data


def test_pharma_report_download_csv(app, client):
    _setup_admin(app)
    _register_pharma_sample(app, 'PH-DL01')
    _login(client, 'admin')
    resp = client.get('/reports/pharma/download?year=2026')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'PH-DL01' in resp.data
    assert b'Lab Number' in resp.data


# ---------------------------------------------------------------------------
# Sidebar and navigation
# ---------------------------------------------------------------------------

def test_sidebar_shows_kpi_report_link(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/dashboard')
    assert b'KPI Report' in resp.data
    assert b'Pharm Report' in resp.data


def test_kpi_dashboard_has_report_links(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/kpi')
    assert b'KPI Report' in resp.data
    assert b'Pharm Report' in resp.data


# ---------------------------------------------------------------------------
# Milk Report
# ---------------------------------------------------------------------------

def _register_milk_sample(app, lab, name='Test Milk', certified=False):
    """Register a milk sample directly in the DB."""
    with app.app_context():
        officer = User.query.filter_by(username='admin').first()
        if not officer:
            officer = _create_user(Role.ADMIN, username='admin')
        s = Sample(
            lab_number=lab,
            sample_name=name,
            sample_type=Branch.FOOD_MILK,
            date_received=date(2026, 1, 15),
            uploaded_by=officer.id,
            status=SampleStatus.CERTIFIED if certified else SampleStatus.REGISTERED,
            milk_type='R',
            volume='500ml',
        )
        if certified:
            s.certified_at = datetime(2026, 2, 15, tzinfo=timezone.utc)
        db.session.add(s)
        db.session.commit()
        return s.id


def test_milk_report_requires_login(app, client):
    resp = client.get('/reports/milk')
    assert resp.status_code == 302


def test_milk_report_access_denied_for_chemist(app, client):
    with app.app_context():
        _create_user(Role.CHEMIST, username='chem')
    _login(client, 'chem')
    resp = client.get('/reports/milk', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_milk_report_renders(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-R01', 'Farm Milk A', certified=True)
    _login(client, 'admin')
    resp = client.get('/reports/milk?year=2026')
    assert resp.status_code == 200
    assert b'Milk Sample Report' in resp.data
    assert b'MILK-R01' in resp.data
    assert b'Farm Milk A' in resp.data


def test_milk_report_quarter_filter(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-Q01')
    _login(client, 'admin')
    # Q1 should include January samples
    resp = client.get('/reports/milk?year=2026&quarter=1')
    assert b'MILK-Q01' in resp.data
    # Q3 should not include January samples
    resp = client.get('/reports/milk?year=2026&quarter=3')
    assert b'MILK-Q01' not in resp.data


def test_milk_report_download_csv(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-DL01')
    _login(client, 'admin')
    resp = client.get('/reports/milk/download?year=2026')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert b'MILK-DL01' in resp.data
    assert b'Lab Number' in resp.data


def test_milk_report_shows_turnaround(app, client):
    _setup_admin(app)
    _register_milk_sample(app, 'MILK-TAT01', certified=True)
    _login(client, 'admin')
    resp = client.get('/reports/milk?year=2026')
    assert resp.status_code == 200
    # Certified sample should show TAT (31 days: Jan 15 → Feb 15)
    assert b'31' in resp.data


def test_sidebar_shows_milk_report_link(app, client):
    _setup_admin(app)
    _login(client, 'admin')
    resp = client.get('/dashboard')
    assert b'Milk Report' in resp.data
