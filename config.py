import os
import secrets
from datetime import timedelta, timezone


# Jamaica timezone (GMT-05:00) – used throughout the application
JAMAICA_TZ = timezone(timedelta(hours=-5))

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_or_create_secret_key() -> str:
    """Return a stable SECRET_KEY shared by all worker processes.

    Resolution order:
    1. ``SECRET_KEY`` environment variable (recommended for production).
    2. A ``.secret_key`` file in the project root (created automatically on
       first run so that all gunicorn workers read the same value).

    Without a shared key every worker generates its own random secret,
    which causes Flask session cookies — and therefore CSRF tokens — to be
    unreadable by any worker other than the one that created them.
    """
    key = os.environ.get('SECRET_KEY')
    if key:
        return key

    key_file = os.path.join(_BASE_DIR, '.secret_key')
    try:
        with open(key_file, encoding='utf-8') as fh:
            key = fh.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass

    # Generate once and persist so subsequent processes (workers/restarts)
    # all share the same secret.  Use os.open() with O_CREAT|O_EXCL so the
    # file is created with restricted permissions (0o600) atomically, avoiding
    # a window where another process could read a world-readable file.
    key = secrets.token_hex(32)
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(key)
    except FileExistsError:
        # Another worker beat us to it; read the key it wrote.
        try:
            with open(key_file, encoding='utf-8') as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        except OSError:
            pass
    except OSError as exc:
        import warnings
        warnings.warn(
            f"Could not persist SECRET_KEY to {key_file}: {exc}. "
            "All workers will use an in-memory key for this run only; "
            "sessions will be invalidated on restart.",
            RuntimeWarning,
            stacklevel=2,
        )
    return key


class Config:
    SECRET_KEY = _load_or_create_secret_key()
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL', 'sqlite:///dgc_sms.db'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Jamaica timezone (GMT-05:00) for all timestamps
    TIMEZONE = JAMAICA_TZ

    # File uploads
    UPLOAD_FOLDER = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'uploads'
    )
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB – allows full-database import ZIPs
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'}

    # Mail – Gmail SMTP defaults
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get(
        'MAIL_DEFAULT_SENDER', 'dgcjamaica@gmail.com'
    )

    # Session timeout (server-side)
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=30)

    # Secure session cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'


class DevelopmentConfig(Config):
    DEBUG = True
    MAIL_SUPPRESS_SEND = True


class ProductionConfig(Config):
    DEBUG = False
    # Set SESSION_COOKIE_SECURE=true in .env only when the site is served over HTTPS.
    # Leaving it false (the default) is required when running behind a plain-HTTP proxy.
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig,
}
