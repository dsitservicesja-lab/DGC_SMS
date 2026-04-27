from flask import Flask, render_template
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
