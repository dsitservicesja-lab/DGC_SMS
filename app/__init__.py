from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect

from config import config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
mail = Mail()
migrate = Migrate()
csrf = CSRFProtect()


def create_app(config_name=None):
    if config_name is None:
        import os
        config_name = os.environ.get('FLASK_CONFIG', 'default')

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    # Ensure upload directory exists
    import os
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Ensure all database tables exist
    with app.app_context():
        db.create_all()

    # Register blueprints
    from app.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from app.samples import samples_bp
    app.register_blueprint(samples_bp, url_prefix='/samples')

    from app.main import main_bp
    app.register_blueprint(main_bp)

    # Make enums available in all templates
    from app.models import Role, Branch
    app.jinja_env.globals['Role'] = Role
    app.jinja_env.globals['Branch'] = Branch

    @app.before_request
    def make_session_permanent():
        from flask import session
        session.permanent = True

    @app.before_request
    def check_password_change():
        from flask import request, redirect, url_for
        from flask_login import current_user
        if (
            current_user.is_authenticated
            and current_user.must_change_password
            and request.endpoint
            and request.endpoint not in ('auth.change_password', 'auth.logout', 'static')
        ):
            return redirect(url_for('auth.change_password'))

    # CLI command: flask send-reminders
    @app.cli.command('send-reminders')
    def send_reminders_cmd():
        """Send expected-report-date reminder notifications."""
        from app.notifications import send_report_date_reminders
        count = send_report_date_reminders()
        print(f'Sent {count} reminder notification(s).')

    return app
