from app.models import Role
from tests.conftest import _create_user, _login


def test_samples_route_smoke(app, client):
    with app.app_context():
        _create_user(role=Role.OFFICER, username='officer', must_change_password=False)

    _login(client, username='officer')
    resp = client.get('/samples/')

    assert resp.status_code == 200
    assert b'Samples' in resp.data


def test_roles_permissions_route_smoke(app, client):
    with app.app_context():
        _create_user(role=Role.ADMIN, username='admin', must_change_password=False)

    _login(client, username='admin')
    resp = client.get('/auth/roles-permissions')

    assert resp.status_code == 200
    assert b'Roles &amp; Permissions' in resp.data
