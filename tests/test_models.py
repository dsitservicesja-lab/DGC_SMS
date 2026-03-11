from app.models import User, Role, Branch


def test_user_creation(app):
    with app.app_context():
        from app import db
        user = User(
            username='test', email='test@test.com',
            first_name='Test', last_name='User',
            role=Role.CHEMIST, branch=Branch.TOXICOLOGY,
        )
        user.set_password('password')
        db.session.add(user)
        db.session.commit()

        assert user.id is not None
        assert user.check_password('password')
        assert not user.check_password('wrong')
        assert user.full_name == 'Test User'
        assert not user.is_branch_head()


def test_senior_chemist_is_branch_head(app):
    with app.app_context():
        from app import db
        user = User(
            username='sc', email='sc@test.com',
            first_name='Senior', last_name='Chem',
            role=Role.SENIOR_CHEMIST, branch=Branch.PHARMACEUTICAL,
        )
        user.set_password('password')
        db.session.add(user)
        db.session.commit()
        assert user.is_branch_head()
