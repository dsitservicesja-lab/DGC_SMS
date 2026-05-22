from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect

from config import config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
mail = Mail()
migrate = Migrate()
csrf = CSRFProtect()


def _verify_schema_compatibility(app):
    """Fail fast when the runtime DB schema is older than current code.

    This avoids opaque 500s later in request handlers by validating a small set
    of critical tables/columns at startup and raising a clear, actionable error.
    """
    required = {
        'users': {'is_active_user', 'must_change_password'},
        'samples': {'api', 'expected_report_date', 'sample_name', 'status'},
        'user_permissions': set(),
        'custom_roles': {'name'},
        'custom_role_permissions': {'custom_role_id', 'permission'},
        'user_custom_roles': {'user_id', 'custom_role_id'},
    }

    engine = db.engine
    inspector = inspect(engine)
    missing_tables = []
    missing_columns = {}

    for table_name, required_cols in required.items():
        if not inspector.has_table(table_name):
            missing_tables.append(table_name)
            continue
        existing_cols = {c['name'] for c in inspector.get_columns(table_name)}
        missing = sorted(required_cols - existing_cols)
        if missing:
            missing_columns[table_name] = missing

    if not missing_tables and not missing_columns:
        return

    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    db_hint = '<db-path>'
    if db_uri.startswith('sqlite:///'):
        db_hint = db_uri.replace('sqlite:///', '', 1)

    lines = [
        'Database schema is out of date for this application version.',
        '',
    ]
    if missing_tables:
        lines.append(f"Missing tables: {', '.join(sorted(missing_tables))}")
    if missing_columns:
        lines.append('Missing columns:')
        for table_name, cols in sorted(missing_columns.items()):
            lines.append(f"  - {table_name}: {', '.join(cols)}")

    lines.extend([
        '',
        'Action required:',
        f"  1. Run migration: python migrate_db.py {db_hint}",
        '  2. Restart app service: sudo systemctl restart dgc_sms',
    ])

    raise RuntimeError('\n'.join(lines))


def create_app(config_name=None):
    if config_name is None:
        import os
        config_name = os.environ.get('FLASK_CONFIG', 'default')

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    import os as _os
    if config_name == 'production' and not _os.environ.get('SECRET_KEY'):
        import warnings
        warnings.warn(
            "SECRET_KEY environment variable is not set. A file-based fallback key "
            "is in use (.secret_key). For hardened production deployments set "
            "SECRET_KEY explicitly in your .env or environment.",
            RuntimeWarning,
            stacklevel=2,
        )

    if config_name == 'production' and not app.config.get('SESSION_COOKIE_SECURE'):
        import warnings
        warnings.warn(
            "SESSION_COOKIE_SECURE is disabled. Session cookies will be sent over "
            "plain HTTP. Set SESSION_COOKIE_SECURE=true in .env once HTTPS is "
            "configured to protect user sessions.",
            RuntimeWarning,
            stacklevel=2,
        )

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
        if config_name != 'testing':
            _verify_schema_compatibility(app)

    # Register blueprints
    from app.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from app.samples import samples_bp
    app.register_blueprint(samples_bp, url_prefix='/samples')

    from app.main import main_bp
    app.register_blueprint(main_bp)

    # Make enums available in all templates
    from app.models import Role, Branch, Permission
    app.jinja_env.globals['Role'] = Role
    app.jinja_env.globals['Branch'] = Branch
    app.jinja_env.globals['Permission'] = Permission

    # Custom Jinja2 filter to parse JSON strings in templates
    import json

    def tojson_load(value):
        """Parse a JSON string, returning an empty dict on failure."""
        if not value:
            return {}
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}

    app.jinja_env.filters['tojson_load'] = tojson_load

    # Make preliminary review checklist categories available in all templates
    from app.forms import PreliminaryReviewForm
    _checklist_for_display = []
    for cat, field_names in PreliminaryReviewForm.CHECKLIST_CATEGORIES:
        items = []
        for fn in field_names:
            # UnboundField stores the label as the first positional arg
            field = getattr(PreliminaryReviewForm, fn, None)
            label = field.args[0] if (field and field.args) else fn
            items.append((fn, label))
        _checklist_for_display.append((cat, items))
    app.jinja_env.globals['checklist_categories'] = _checklist_for_display

    @app.before_request
    def make_session_permanent():
        from flask import session
        session.permanent = True

    @app.before_request
    def update_last_seen():
        from flask_login import current_user
        from flask import request
        from app.models import jamaica_now
        if (
            current_user.is_authenticated
            and request.endpoint
            and request.endpoint != 'static'
        ):
            # Store as naive datetime (SQLite doesn't preserve timezone info)
            now = jamaica_now().replace(tzinfo=None)
            # Only write to DB at most once per minute to avoid hammering
            if (
                current_user.last_seen is None
                or (now - current_user.last_seen).total_seconds() > 60
            ):
                current_user.last_seen = now
                try:
                    db.session.commit()
                except Exception:
                    current_app.logger.exception('Failed to update last_seen for user %s', current_user.id)
                    db.session.rollback()

    @app.after_request
    def set_security_headers(response):
        """Add HTTP security headers to every response."""
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = (
            'geolocation=(), camera=(), microphone=()'
        )
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "img-src 'self' data:; "
            "frame-ancestors 'self'"
        )
        response.headers['Content-Security-Policy'] = csp
        return response

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

    # ---------------------------------------------------------------------------
    # Error handlers – show useful details for internal users
    # ---------------------------------------------------------------------------

    @app.errorhandler(403)
    def forbidden(exc):
        return render_template('errors/403.html'), 403

    @app.errorhandler(404)
    def page_not_found(exc):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_server_error(exc):
        import traceback
        from flask_login import current_user
        error_details = traceback.format_exc()
        app.logger.error('Internal Server Error: %s', error_details)
        # Only expose the traceback to authenticated admin/HOD users
        visible_details = None
        try:
            from app.models import Role
            if (
                current_user.is_authenticated
                and current_user.has_any_role(Role.ADMIN, Role.HOD)
            ):
                visible_details = error_details
        except Exception:
            pass
        return render_template('errors/500.html', error_details=visible_details), 500

    # CLI command: flask send-reminders
    @app.cli.command('send-reminders')
    def send_reminders_cmd():
        """Send expected-report-date reminder notifications."""
        from app.notifications import send_report_date_reminders
        count = send_report_date_reminders()
        print(f'Sent {count} reminder notification(s).')

    return app
