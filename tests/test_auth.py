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
        'role': 'CHEMIST',
        'branch': 'TOXICOLOGY',
    }, follow_redirects=True)
    assert b'created successfully' in resp.data
