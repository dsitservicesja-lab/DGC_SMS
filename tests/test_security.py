"""Security-focused tests for DGC SMS hardening."""

from app.models import User, Role, Branch
from tests.conftest import _create_user, _login


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------

def test_account_lockout_after_failed_attempts(app, client):
    """Account should lock after MAX_FAILED_ATTEMPTS wrong passwords."""
    with app.app_context():
        _create_user(role=Role.OFFICER, username='locktarget')

    for _ in range(User.MAX_FAILED_ATTEMPTS):
        client.post('/auth/login', data={
            'username': 'locktarget',
            'password': 'wrong',
        })

    # Now even the correct password should be blocked
    resp = client.post('/auth/login', data={
        'username': 'locktarget',
        'password': 'password123',
    }, follow_redirects=True)
    assert b'temporarily locked' in resp.data


def test_successful_login_resets_failed_counter(app, client):
    """A successful login should reset the failed login counter."""
    with app.app_context():
        _create_user(role=Role.OFFICER, username='resetme')

    # Two failed attempts
    for _ in range(2):
        client.post('/auth/login', data={
            'username': 'resetme',
            'password': 'wrong',
        })

    # Successful login
    resp = _login(client, username='resetme')
    assert b'Dashboard' in resp.data

    # Verify counter was reset
    with app.app_context():
        user = User.query.filter_by(username='resetme').first()
        assert user.failed_login_attempts == 0


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def test_security_headers_present(app, client):
    """All important security headers should be set on responses."""
    with app.app_context():
        _create_user()
    resp = _login(client)
    assert resp.headers.get('X-Content-Type-Options') == 'nosniff'
    assert resp.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert resp.headers.get('X-XSS-Protection') == '1; mode=block'
    assert 'Content-Security-Policy' in resp.headers
    assert resp.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

def test_session_cookie_httponly(app):
    """Session cookie should be HTTPOnly."""
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True


def test_session_cookie_samesite(app):
    """Session cookie should have SameSite=Lax."""
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

def test_password_requires_uppercase(app, client):
    """Creating a user with no uppercase letter in the password should fail."""
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    resp = client.post('/auth/users/create', data={
        'first_name': 'Weak',
        'last_name': 'Pass',
        'username': 'weakuser',
        'email': 'weak@test.com',
        'password': 'alllower1',
        'password2': 'alllower1',
        'roles': ['CHEMIST'],
        'branches': ['TOXICOLOGY'],
    }, follow_redirects=True)
    assert b'uppercase' in resp.data


def test_password_requires_digit(app, client):
    """Creating a user with no digit in the password should fail."""
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    resp = client.post('/auth/users/create', data={
        'first_name': 'Weak',
        'last_name': 'Pass',
        'username': 'weakuser2',
        'email': 'weak2@test.com',
        'password': 'NoDigitHere',
        'password2': 'NoDigitHere',
        'roles': ['CHEMIST'],
        'branches': ['TOXICOLOGY'],
    }, follow_redirects=True)
    assert b'digit' in resp.data


def test_password_min_length(app, client):
    """Password shorter than 8 chars should be rejected."""
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    resp = client.post('/auth/users/create', data={
        'first_name': 'Short',
        'last_name': 'Pass',
        'username': 'shortpw',
        'email': 'short@test.com',
        'password': 'Ab1',
        'password2': 'Ab1',
        'roles': ['CHEMIST'],
        'branches': ['TOXICOLOGY'],
    }, follow_redirects=True)
    assert b'created successfully' not in resp.data


# ---------------------------------------------------------------------------
# Open redirect prevention on notifications
# ---------------------------------------------------------------------------

def test_notification_link_must_be_relative(app, client):
    """Notification links pointing to external URLs should not be followed."""
    from app.models import Notification, jamaica_now
    from app import db

    with app.app_context():
        user = _create_user(role=Role.OFFICER, username='notifuser')
        notif = Notification(
            user_id=user.id,
            title='Phishing test',
            message='Click here',
            link='https://evil.example.com/steal',
            created_at=jamaica_now(),
        )
        db.session.add(notif)
        db.session.commit()
        notif_id = notif.id

    _login(client, username='notifuser')
    resp = client.post(f'/notifications/{notif_id}/read')
    # Should redirect to the notifications list, NOT to evil.example.com
    assert resp.status_code == 302
    assert 'evil.example.com' not in resp.headers.get('Location', '')


# ---------------------------------------------------------------------------
# Supporting document upload authorization
# ---------------------------------------------------------------------------

def test_upload_supporting_doc_denied_for_chemist(app, client):
    """A chemist who did not upload the sample should be denied."""
    from app.models import Sample, SampleStatus, jamaica_now
    from app import db

    with app.app_context():
        officer = _create_user(role=Role.OFFICER, username='officer',
                               branch=Branch.TOXICOLOGY)
        _create_user(role=Role.CHEMIST, username='chemist',
                     branch=Branch.TOXICOLOGY)
        now = jamaica_now()
        sample = Sample(
            lab_number='TOX-001',
            sample_name='Test Sample',
            sample_type=Branch.TOXICOLOGY,
            date_received=now.date(),
            uploaded_by=officer.id,
            status=SampleStatus.REGISTERED,
        )
        db.session.add(sample)
        db.session.commit()
        sample_id = sample.id

    _login(client, username='chemist')
    resp = client.get(f'/samples/{sample_id}/upload-supporting-doc',
                      follow_redirects=True)
    assert b'Access denied' in resp.data
