import enum
from datetime import datetime, timezone

from flask import current_app
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash

from app import db, login_manager


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Role(enum.Enum):
    OFFICER = 'Officer'
    CHEMIST = 'Chemist'
    SENIOR_CHEMIST = 'Senior Chemist'
    DEPUTY = 'Deputy'
    HOD = 'HOD'
    ADMIN = 'Admin'


class Branch(enum.Enum):
    TOXICOLOGY = 'Toxicology'
    PHARMACEUTICAL = 'Pharmaceutical'
    FOOD_MILK = 'Food (Milk)'
    FOOD_ALCOHOL = 'Food (Alcohol)'


class SampleStatus(enum.Enum):
    REGISTERED = 'Registered'
    ASSIGNED = 'Assigned'
    IN_PROGRESS = 'In Progress'
    REPORT_SUBMITTED = 'Report Submitted'
    UNDER_PRELIMINARY_REVIEW = 'Preliminary Review'
    UNDER_TECHNICAL_REVIEW = 'Technical Review'
    RETURNED = 'Returned for Correction'
    ACCEPTED = 'Accepted'
    DEPUTY_REVIEW = 'Deputy Review'
    DEPUTY_RETURNED = 'Returned by Deputy'
    CERTIFICATE_PREPARATION = 'Certificate Preparation'
    HOD_REVIEW = 'HOD Review'
    HOD_RETURNED = 'Returned by HOD'
    CERTIFIED = 'Certified'
    REJECTED = 'Rejected'
    COMPLETED = 'Completed'


class AssignmentStatus(enum.Enum):
    ASSIGNED = 'Assigned'
    IN_PROGRESS = 'In Progress'
    REPORT_SUBMITTED = 'Report Submitted'
    UNDER_PRELIMINARY_REVIEW = 'Preliminary Review'
    UNDER_TECHNICAL_REVIEW = 'Technical Review'
    RETURNED = 'Returned for Correction'
    ACCEPTED = 'Accepted'
    REJECTED = 'Rejected'
    COMPLETED = 'Completed'


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    first_name = db.Column(db.String(120), nullable=False)
    last_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.Enum(Role), nullable=False, default=Role.CHEMIST)
    branch = db.Column(db.Enum(Branch), nullable=True)
    is_active_user = db.Column(db.Boolean, default=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    uploaded_samples = db.relationship(
        'Sample', backref='uploaded_by_user', lazy='dynamic',
        foreign_keys='Sample.uploaded_by'
    )
    assignments = db.relationship(
        'SampleAssignment', backref='chemist', lazy='dynamic',
        foreign_keys='SampleAssignment.chemist_id'
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def get_reset_token(self):
        """Generate a timed password-reset token."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        return s.dumps(self.id, salt='password-reset')

    @staticmethod
    def verify_reset_token(token, max_age=1800):
        """Return the User for a valid token, or None (default 30 min expiry)."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        try:
            user_id = s.loads(token, salt='password-reset', max_age=max_age)
        except (SignatureExpired, BadSignature):
            return None
        return db.session.get(User, user_id)

    def is_branch_head(self):
        return self.role in (Role.SENIOR_CHEMIST, Role.HOD, Role.DEPUTY)

    def __repr__(self):
        return f'<User {self.username} ({self.role.value})>'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------

class Sample(db.Model):
    __tablename__ = 'samples'

    id = db.Column(db.Integer, primary_key=True)
    lab_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    sample_name = db.Column(db.String(255), nullable=False)
    sample_type = db.Column(db.Enum(Branch), nullable=False)
    description = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.String(100), nullable=True)
    parish = db.Column(db.String(100), nullable=True)
    patient_name = db.Column(db.String(255), nullable=True)  # Toxicology
    source = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.Enum(SampleStatus), nullable=False, default=SampleStatus.REGISTERED
    )

    # Scanned document
    scanned_file = db.Column(db.String(500), nullable=True)
    scanned_file_original_name = db.Column(db.String(255), nullable=True)

    # Tracking
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    date_received = db.Column(db.Date, nullable=False)
    date_registered = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    expected_report_date = db.Column(db.Date, nullable=True)

    # Summary report (pharmaceutical samples – prepared by Senior Chemist)
    summary_report = db.Column(db.Text, nullable=True)
    summary_report_file = db.Column(db.String(500), nullable=True)
    summary_report_file_original_name = db.Column(db.String(255), nullable=True)
    summary_report_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    summary_report_at = db.Column(db.DateTime, nullable=True)

    # Deputy Government Chemist review
    deputy_review_comments = db.Column(db.Text, nullable=True)
    deputy_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    deputy_reviewed_at = db.Column(db.DateTime, nullable=True)

    # Certificate of Analysis (prepared by Deputy)
    certificate_text = db.Column(db.Text, nullable=True)
    certificate_file = db.Column(db.String(500), nullable=True)
    certificate_file_original_name = db.Column(db.String(255), nullable=True)
    certificate_prepared_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    certificate_prepared_at = db.Column(db.DateTime, nullable=True)

    # Government Chemist (HOD) review & signing
    hod_review_comments = db.Column(db.Text, nullable=True)
    hod_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    hod_reviewed_at = db.Column(db.DateTime, nullable=True)
    certified_at = db.Column(db.DateTime, nullable=True)
    certified_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )

    # Relationships
    assignments = db.relationship(
        'SampleAssignment', backref='sample', lazy='dynamic',
        cascade='all, delete-orphan'
    )
    history = db.relationship(
        'SampleHistory', backref='sample', lazy='dynamic',
        cascade='all, delete-orphan', order_by='SampleHistory.created_at.desc()'
    )
    summary_report_user = db.relationship(
        'User', foreign_keys=[summary_report_by]
    )
    deputy_reviewer = db.relationship(
        'User', foreign_keys=[deputy_reviewed_by]
    )
    certificate_preparer = db.relationship(
        'User', foreign_keys=[certificate_prepared_by]
    )
    hod_reviewer = db.relationship(
        'User', foreign_keys=[hod_reviewed_by]
    )
    certifier = db.relationship(
        'User', foreign_keys=[certified_by]
    )

    def __repr__(self):
        return f'<Sample {self.lab_number} - {self.sample_name}>'


# ---------------------------------------------------------------------------
# Sample Assignment  (one sample → many chemists)
# ---------------------------------------------------------------------------

class SampleAssignment(db.Model):
    __tablename__ = 'sample_assignments'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    chemist_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    assigned_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    test_name = db.Column(db.String(255), nullable=False)
    test_reference = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.Enum(AssignmentStatus), nullable=False,
        default=AssignmentStatus.ASSIGNED
    )

    assigned_date = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    expected_completion = db.Column(db.Date, nullable=True)
    date_completed = db.Column(db.DateTime, nullable=True)

    # Report
    report_text = db.Column(db.Text, nullable=True)
    report_file = db.Column(db.String(500), nullable=True)
    report_file_original_name = db.Column(db.String(255), nullable=True)
    report_submitted_at = db.Column(db.DateTime, nullable=True)

    # Preliminary review (by Officer / Senior Chemist Technologist)
    preliminary_review_comments = db.Column(db.Text, nullable=True)
    preliminary_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    preliminary_reviewed_at = db.Column(db.DateTime, nullable=True)

    # Technical review (by Senior Chemist)
    review_comments = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)

    # Track which review stage returned from ('preliminary' or 'technical')
    return_stage = db.Column(db.String(20), nullable=True)

    # Relationships
    assigner = db.relationship(
        'User', foreign_keys=[assigned_by], backref='made_assignments'
    )
    preliminary_reviewer = db.relationship(
        'User', foreign_keys=[preliminary_reviewed_by],
        backref='preliminary_reviewed_assignments'
    )
    reviewer = db.relationship(
        'User', foreign_keys=[reviewed_by], backref='reviewed_assignments'
    )

    def __repr__(self):
        return f'<Assignment {self.id}: Sample {self.sample_id} → User {self.chemist_id}>'


# ---------------------------------------------------------------------------
# Audit / History
# ---------------------------------------------------------------------------

class SampleHistory(db.Model):
    __tablename__ = 'sample_history'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    action = db.Column(db.String(255), nullable=False)
    details = db.Column(db.Text, nullable=True)
    performed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    performer = db.relationship('User', foreign_keys=[performed_by])

    def __repr__(self):
        return f'<History {self.action} on Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    email_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic'))

    def __repr__(self):
        return f'<Notification {self.title} for User {self.user_id}>'


# ---------------------------------------------------------------------------
# App Settings (key/value store)
# ---------------------------------------------------------------------------

class Setting(db.Model):
    __tablename__ = 'settings'

    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.String(500), nullable=False, default='')

    @staticmethod
    def get(key, default=''):
        row = db.session.get(Setting, key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = db.session.get(Setting, key)
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=key, value=value))

    @staticmethod
    def get_bool(key, default=True):
        val = Setting.get(key)
        if val == '':
            return default
        return val.lower() in ('true', '1', 'yes')

    def __repr__(self):
        return f'<Setting {self.key}={self.value}>'
