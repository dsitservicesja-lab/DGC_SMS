from datetime import timedelta

from app.models import User, Role
from tests.conftest import _create_user, _login


def test_login_page(client):
    resp = client.get('/auth/login')
    assert resp.status_code == 200
    assert b'Sign In' in resp.data


def test_login_success(app, client):
    with app.app_context():
        _create_user()
    resp = _login(client)
    assert resp.status_code == 200
    assert b'Dashboard' in resp.data


def test_login_invalid(app, client):
    with app.app_context():
        _create_user()
    resp = _login(client, password='wrong')
    assert b'Invalid username or password' in resp.data


def test_logout(app, client):
    with app.app_context():
        _create_user()
    _login(client)
    resp = client.get('/auth/logout', follow_redirects=True)
    assert b'Sign In' in resp.data


def test_redirect_unauthenticated(client):
    resp = client.get('/dashboard')
    assert resp.status_code == 302


def test_user_list_access_denied(app, client):
    with app.app_context():
        _create_user(role=Role.CHEMIST)
    _login(client)
    resp = client.get('/auth/users', follow_redirects=True)
    assert b'Access denied' in resp.data


def test_user_list_admin(app, client):
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    resp = client.get('/auth/users')
    assert resp.status_code == 200
    assert b'Users' in resp.data


def test_create_user(app, client):
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    resp = client.post('/auth/users/create', data={
        'first_name': 'New',
        'last_name': 'User',
        'username': 'newuser',
        'email': 'new@test.com',
        'password': 'pass1234',
        'password2': 'pass1234',
        'roles': ['CHEMIST'],
        'branches': ['TOXICOLOGY'],
    }, follow_redirects=True)
    assert b'created successfully' in resp.data


def test_new_user_redirected_to_change_password(app, client):
    """A user created via the web UI (must_change_password=True) should be
    redirected to the change-password page on login, NOT see a 500 error."""
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin')
    _login(client, username='admin')
    client.post('/auth/users/create', data={
        'first_name': 'Jane',
        'last_name': 'Doe',
        'username': 'janedoe',
        'email': 'jane@test.com',
        'password': 'pass1234',
        'password2': 'pass1234',
        'roles': ['CHEMIST'],
        'branches': ['TOXICOLOGY'],
    }, follow_redirects=True)
    # Log out admin, log in as new user
    client.get('/auth/logout')
    resp = client.post('/auth/login', data={
        'username': 'janedoe',
        'password': 'pass1234',
    }, follow_redirects=True)
    # Should reach change-password page, not a 500
    assert resp.status_code == 200
    assert b'Change Password' in resp.data


def test_must_change_password_blocks_other_pages(app, client):
    """A user with must_change_password=True should be redirected away from
    all non-exempt pages, not given a 500."""
    with app.app_context():
        _create_user(role=Role.CHEMIST, username='chemist',
                     must_change_password=True)
    resp = _login(client, username='chemist')
    # After login, should land on change-password page
    assert b'Change Password' in resp.data
    # Trying to access dashboard should redirect back to change-password
    resp = client.get('/dashboard', follow_redirects=True)
    assert resp.status_code == 200
    assert b'Change Password' in resp.data


def test_session_timeout_configured(app):
    """PERMANENT_SESSION_LIFETIME must be set so sessions expire."""
    assert 'PERMANENT_SESSION_LIFETIME' in app.config
    lifetime = app.config['PERMANENT_SESSION_LIFETIME']
    assert isinstance(lifetime, timedelta)
    assert lifetime <= timedelta(hours=1), "Session timeout should be at most 1 hour"


def test_session_is_marked_permanent(app, client):
    """Every request should mark the session as permanent so that
    PERMANENT_SESSION_LIFETIME is enforced."""
    with app.app_context():
        _create_user()
    _login(client)
    with client.session_transaction() as sess:
        assert sess.permanent is True


def test_session_expires_after_lifetime(app, client):
    """The session cookie should carry an expiry so that the browser
    discards it after PERMANENT_SESSION_LIFETIME of inactivity."""
    with app.app_context():
        _create_user()
    resp = _login(client)

    # The Set-Cookie header for the session should include an Expires
    # or Max-Age directive because session.permanent = True
    set_cookie = resp.headers.get('Set-Cookie', '')
    assert 'Expires=' in set_cookie or 'Max-Age=' in set_cookie, \
        "Session cookie must have an expiry for timeout to work"
