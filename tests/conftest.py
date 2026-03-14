import pytest
from app import create_app, db
from app.models import User, Role, Branch


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def _create_user(role=Role.OFFICER, branch=None, username='testuser',
                 must_change_password=False):
    user = User(
        username=username,
        email=f'{username}@test.com',
        first_name='Test',
        last_name='User',
        must_change_password=must_change_password,
    )
    user.set_password('password123')
    user.roles = {role}
    if branch:
        user.branches = {branch}
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, username='testuser', password='password123'):
    return client.post('/auth/login', data={
        'username': username,
        'password': password,
    }, follow_redirects=True)
