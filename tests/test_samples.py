from datetime import date
from app import db
from app.models import (
    Sample, SampleAssignment, User, Role, Branch,
    SampleStatus, AssignmentStatus,
)
from tests.conftest import _create_user, _login


def _setup_users(app):
    """Create officer, senior chemist, and chemist."""
    with app.app_context():
        officer = _create_user(Role.OFFICER, username='officer')
        sc = _create_user(
            Role.SENIOR_CHEMIST, Branch.TOXICOLOGY, username='senior'
        )
        chemist = _create_user(
            Role.CHEMIST, Branch.TOXICOLOGY, username='chemist'
        )
        return officer.id, sc.id, chemist.id


def _register_sample(client):
    """Register a sample via the form."""
    return client.post('/samples/register', data={
        'lab_number': 'TOX-001',
        'sample_name': 'Test Substance',
        'sample_type': 'TOXICOLOGY',
        'date_received': '2026-01-15',
        'description': 'Test sample',
        'quantity': '50ml',
    }, follow_redirects=True)


def test_register_sample(app, client):
    _setup_users(app)
    _login(client, 'officer')
    resp = _register_sample(client)
    assert resp.status_code == 200
    assert b'TOX-001' in resp.data


def test_sample_list(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    resp = client.get('/samples/')
    assert resp.status_code == 200
    assert b'TOX-001' in resp.data


def test_sample_detail(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    with app.app_context():
        sample = Sample.query.first()
    resp = client.get(f'/samples/{sample.id}')
    assert resp.status_code == 200
    assert b'Test Substance' in resp.data


def test_assign_sample(app, client):
    officer_id, sc_id, chemist_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Screening Test',
        'test_reference': 'REF-001',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'assigned successfully' in resp.data


def test_submit_report(app, client):
    officer_id, sc_id, chemist_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    # Assign
    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Blood Analysis',
    }, follow_redirects=True)
    client.get('/auth/logout')

    # Submit report
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'No harmful substances detected.',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'submitted successfully' in resp.data


def test_review_report(app, client):
    officer_id, sc_id, chemist_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Purity Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Results within acceptable range.',
    })
    client.get('/auth/logout')

    # Review
    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
        'review_comments': 'Looks good.',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'accepted' in resp.data


def test_return_for_correction(app, client):
    officer_id, sc_id, chemist_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Quality Check',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Initial findings.',
    })
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'returned',
        'review_comments': 'Please add more detail.',
    }, follow_redirects=True)
    assert b'returned' in resp.data

    # Chemist can resubmit
    client.get('/auth/logout')
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Updated detailed findings with additional analysis.',
    }, follow_redirects=True)
    assert b'submitted successfully' in resp.data


def test_chemist_cannot_register_sample(app, client):
    _setup_users(app)
    _login(client, 'chemist')
    resp = client.get('/samples/register', follow_redirects=True)
    assert b'Only officers can register samples' in resp.data


def test_officer_cannot_assign(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    with app.app_context():
        sample = Sample.query.first()
    resp = client.get(f'/samples/{sample.id}/assign', follow_redirects=True)
    assert b'Senior Chemists' in resp.data
