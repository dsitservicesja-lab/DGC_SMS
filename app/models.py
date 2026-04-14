import enum
import sqlite3
from datetime import datetime, timezone, timedelta, date

from flask import current_app
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from sqlalchemy import delete, event, insert, select
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash, check_password_hash

from app import db, login_manager

# Jamaica timezone (GMT-05:00) for consistent timestamp handling
JAMAICA_TZ = timezone(timedelta(hours=-5))


def jamaica_now():
    """Return current datetime in Jamaica timezone (GMT-05:00)."""
    return datetime.now(JAMAICA_TZ)


# ---------------------------------------------------------------------------
# SQLite tuning – enable WAL journal mode and a generous busy-timeout so that
# concurrent gunicorn workers don't produce "database is locked" errors.
# This listener fires for every new DBAPI connection; it is a no-op for any
# non-SQLite backend.
# ---------------------------------------------------------------------------

@event.listens_for(Engine, 'connect')
def _configure_sqlite(dbapi_conn, _connection_record):
    if isinstance(dbapi_conn, sqlite3.Connection):
        dbapi_conn.execute('PRAGMA journal_mode=WAL')
        dbapi_conn.execute('PRAGMA busy_timeout=5000')


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
    PHARMACEUTICAL_NR = 'Not Registered Pharm'
    FOOD_MILK = 'Food (Milk)'
    FOOD_ALCOHOL = 'Food (Alcohol)'


# ---------------------------------------------------------------------------
# Many-to-many association tables for User ↔ Role / Branch
# ---------------------------------------------------------------------------

user_roles = db.Table(
    'user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role', db.Enum(Role), primary_key=True),
)

user_branches = db.Table(
    'user_branches',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('branch', db.Enum(Branch), primary_key=True),
)


class SampleStatus(enum.Enum):
    REGISTERED = 'Registered'
    ASSIGNED = 'Assigned'
    IN_PROGRESS = 'In Progress'
    REPORT_SUBMITTED = 'Report Submitted'
    UNDER_PRELIMINARY_REVIEW = 'Preliminary Review'
    UNDER_TECHNICAL_REVIEW = 'Senior Chemist Review'
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
    UNDER_TECHNICAL_REVIEW = 'Senior Chemist Review'
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
    role = db.Column(db.Enum(Role), nullable=True)       # legacy single-role
    branch = db.Column(db.Enum(Branch), nullable=True)    # legacy single-branch
    is_active_user = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=True)
    created_at = db.Column(
        db.DateTime, default=jamaica_now
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

    # ----- role / branch helpers (backed by association tables) -----

    _roles = None        # in-memory cache / pending value
    _roles_dirty = False  # True only when set via the setter (needs DB write)
    _branches = None
    _branches_dirty = False

    @property
    def roles(self):
        """Return the set of Role enums for this user."""
        if self._roles is not None:
            return self._roles
        if self.id is None:
            self._roles = set()
            return self._roles
        rows = db.session.execute(
            select(user_roles).where(user_roles.c.user_id == self.id)
        ).fetchall()
        self._roles = {row.role for row in rows}
        # _roles_dirty intentionally NOT set – this is a DB read, not a write
        return self._roles

    @roles.setter
    def roles(self, value):
        self._roles = set(value)
        self._roles_dirty = True

    @property
    def branches(self):
        """Return the set of Branch enums for this user."""
        if self._branches is not None:
            return self._branches
        if self.id is None:
            self._branches = set()
            return self._branches
        rows = db.session.execute(
            select(user_branches).where(user_branches.c.user_id == self.id)
        ).fetchall()
        self._branches = {row.branch for row in rows}
        # _branches_dirty intentionally NOT set – this is a DB read, not a write
        return self._branches

    @branches.setter
    def branches(self, value):
        self._branches = set(value)
        self._branches_dirty = True

    def has_role(self, role):
        return role in self.roles

    def has_any_role(self, *roles):
        return bool(self.roles & set(roles))

    def has_branch(self, branch):
        return branch in self.branches

    def has_any_branch(self, *branches):
        return bool(self.branches & set(branches))

    @property
    def role_names(self):
        """Comma-separated display of roles."""
        return ', '.join(sorted(r.value for r in self.roles)) or '—'

    @property
    def branch_names(self):
        """Comma-separated display of branches."""
        return ', '.join(sorted(b.value for b in self.branches)) or '—'

    @property
    def primary_branch(self):
        """Return one branch (for filtering) or None."""
        return next(iter(self.branches), None)

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
        return self.has_any_role(Role.SENIOR_CHEMIST, Role.HOD, Role.DEPUTY)

    def __repr__(self):
        return f'<User {self.username} ({self.role_names})>'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Flush pending roles/branches to the association tables after insert or update.
# Only fires when roles/branches were explicitly assigned (dirty flag is set).

@event.listens_for(User, 'after_insert')
@event.listens_for(User, 'after_update')
def _flush_user_roles_branches(mapper, connection, target):
    if target._roles_dirty:
        connection.execute(
            delete(user_roles).where(user_roles.c.user_id == target.id)
        )
        for r in target._roles:
            connection.execute(insert(user_roles).values(
                user_id=target.id, role=r
            ))
        target._roles_dirty = False
    if target._branches_dirty:
        connection.execute(
            delete(user_branches).where(user_branches.c.user_id == target.id)
        )
        for b in target._branches:
            connection.execute(insert(user_branches).values(
                user_id=target.id, branch=b
            ))
        target._branches_dirty = False

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

    # Food (Milk) specific
    milk_type = db.Column(db.String(10), nullable=True)  # 'R' = Raw, 'P' = Processed

    # Volume (Toxicology and Milk samples)
    volume = db.Column(db.String(100), nullable=True)

    # Pharmaceutical specific
    formulation_type = db.Column(db.String(100), nullable=True)

    # Food (Alcohol) specific
    alcohol_type = db.Column(db.String(100), nullable=True)
    claim_butt_number = db.Column(db.String(100), nullable=True)
    batch_lot_number = db.Column(db.String(100), nullable=True)  # Food (Alcohol)

    # Pharmaceutical & Milk shared fields
    lot_number = db.Column(db.String(100), nullable=True)
    expiration_date = db.Column(db.Date, nullable=True)

    # Toxicology – sample type dropdown (Blood, Urine, etc.)
    toxicology_sample_type_name = db.Column(db.String(100), nullable=True)

    # Scanned document
    scanned_file = db.Column(db.String(500), nullable=True)
    scanned_file_original_name = db.Column(db.String(255), nullable=True)

    # Tracking
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    date_received = db.Column(db.Date, nullable=False)
    date_registered = db.Column(
        db.DateTime, default=jamaica_now
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
    coa_reference = db.Column(db.String(255), nullable=True)  # Certificate reference number

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
        db.DateTime, default=jamaica_now
    )
    expected_completion = db.Column(db.Date, nullable=True)
    date_completed = db.Column(db.DateTime, nullable=True)

    # Report
    report_text = db.Column(db.Text, nullable=True)
    report_file = db.Column(db.String(500), nullable=True)
    report_file_original_name = db.Column(db.String(255), nullable=True)
    report_submitted_at = db.Column(db.DateTime, nullable=True)
    all_samples_returned = db.Column(db.String(10), nullable=True)
    return_quantity = db.Column(db.String(100), nullable=True)

    # Preliminary review (by Officer / Senior Chemist Technologist)
    preliminary_review_comments = db.Column(db.Text, nullable=True)
    preliminary_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    preliminary_reviewed_at = db.Column(db.DateTime, nullable=True)
    preliminary_review_checklist = db.Column(db.Text, nullable=True)  # JSON

    # Technical review (by Senior Chemist)
    review_comments = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)

    # Track which review stage returned from ('preliminary' or 'technical')
    return_stage = db.Column(db.String(20), nullable=True)
    comments = db.Column(db.Text, nullable=True)
    quantity_volume = db.Column(db.String(100), nullable=True)

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
        db.DateTime, default=jamaica_now
    )
    # Enhanced audit fields
    action_type = db.Column(db.String(100), nullable=True)     # e.g. 'Original Submission', 'Resubmission'
    object_affected = db.Column(db.String(255), nullable=True)  # e.g. 'Report', 'COA', 'Sample'
    change_description = db.Column(db.Text, nullable=True)      # what changed and why

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
        db.DateTime, default=jamaica_now
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


# ---------------------------------------------------------------------------
# KPI Targets
# ---------------------------------------------------------------------------

# Canonical list of KPI metric keys and their human-readable labels.
KPI_METRICS = [
    ('pct_improvement_planned_targets',
     '% improvement in the achievement of planned targets'),
    ('pct_compliance_sop',
     '% compliance with standard operating procedures, quality control, '
     'environmental and accreditation standards'),
    ('complaints_resolved',
     '# of complaints resolved per quarter'),
    ('analysts_qualified',
     '# of analyst with required technical skills and competencies'),
    ('equipment_inspections',
     '# of equipment maintenance inspections conducted per quarter'),
    ('pharma_coas',
     '# Pharmaceutical COA\'s generated'),
    ('milk_coas',
     '# of milk COA\'s generated'),
    ('toxicology_roas',
     '# of toxicology ROA\'s generated'),
    ('alcohol_coas',
     '# Alcohol COA\'s generated'),
    ('avg_days_pharma_coa',
     'Average days taken to generate pharmaceutical COA\'s'),
    ('avg_days_milk_coa',
     'Average days taken to generate milk COA\'s'),
    ('avg_days_toxicology_roa',
     'Average days taken to generate toxicology ROA\'s'),
]

# Keys whose "Actual" value is auto-computed from Sample data.
AUTO_ACTUAL_KEYS = {
    'pharma_coas', 'milk_coas', 'toxicology_roas', 'alcohol_coas',
    'avg_days_pharma_coa', 'avg_days_milk_coa', 'avg_days_toxicology_roa',
}


class KpiTarget(db.Model):
    """Stores per-year / per-quarter KPI targets and optional manual actuals."""
    __tablename__ = 'kpi_targets'

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    quarter = db.Column(db.Integer, nullable=False)          # 1-4
    kpi_key = db.Column(db.String(100), nullable=False)
    target_value = db.Column(db.Float, nullable=True)
    actual_override = db.Column(db.Float, nullable=True)     # for manual KPIs

    __table_args__ = (
        db.UniqueConstraint('year', 'quarter', 'kpi_key',
                            name='uq_kpi_target'),
    )

    def __repr__(self):
        return (f'<KpiTarget {self.kpi_key} '
                f'Y{self.year} Q{self.quarter} '
                f'T={self.target_value} A={self.actual_override}>')


# ---------------------------------------------------------------------------
# Non-Working Days (holidays, emergency closures) – for TAT calculation
# ---------------------------------------------------------------------------

class NonWorkingDay(db.Model):
    """Calendar entries for public holidays and emergency closure days.
    These dates are excluded from turnaround time (TAT) calculations."""
    __tablename__ = 'non_working_days'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True, index=True)
    description = db.Column(db.String(255), nullable=False)
    day_type = db.Column(db.String(50), nullable=False, default='holiday')  # 'holiday' or 'emergency'
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    creator = db.relationship('User', foreign_keys=[created_by])

    def __repr__(self):
        return f'<NonWorkingDay {self.date} - {self.description}>'


def calculate_working_days(start_date, end_date):
    """Calculate working days between two dates, excluding weekends and
    non-working days (public holidays, emergency closures)."""
    if not start_date or not end_date:
        return None
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    # Get all non-working dates in the range
    non_working = {
        row.date for row in NonWorkingDay.query.filter(
            NonWorkingDay.date >= start_date,
            NonWorkingDay.date <= end_date,
        ).all()
    }

    count = 0
    current = start_date
    while current <= end_date:
        # Exclude weekends (Mon-Fri are 0-4; Sat=5, Sun=6) and non-working days
        if current.weekday() < 5 and current not in non_working:
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Supporting Documents (additional uploads by officers)
# ---------------------------------------------------------------------------

class SupportingDocument(db.Model):
    """Extra supporting documents uploaded for a sample."""
    __tablename__ = 'supporting_documents'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    uploaded_at = db.Column(db.DateTime, default=jamaica_now)

    sample = db.relationship('Sample', backref=db.backref(
        'supporting_documents', lazy='dynamic', cascade='all, delete-orphan'
    ))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])

    def __repr__(self):
        return f'<SupportingDocument {self.original_name} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Document Version (tracks every file upload as a new version)
# ---------------------------------------------------------------------------

class DocumentVersion(db.Model):
    """Tracks every uploaded file version for full retention.
    New uploads never overwrite previous documents; each is stored as a
    new version linked to the parent record."""
    __tablename__ = 'document_versions'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    document_type = db.Column(db.String(50), nullable=False)  # 'scanned_file', 'report', 'certificate', 'summary_report', 'supporting'
    version_number = db.Column(db.Integer, nullable=False, default=1)
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    upload_label = db.Column(db.String(50), nullable=True)  # 'original', 'revised', 'resubmission'
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    uploaded_at = db.Column(db.DateTime, default=jamaica_now)
    assignment_id = db.Column(
        db.Integer, db.ForeignKey('sample_assignments.id'), nullable=True
    )

    sample = db.relationship('Sample', backref=db.backref(
        'document_versions', lazy='dynamic', cascade='all, delete-orphan'
    ))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])
    assignment = db.relationship('SampleAssignment', backref=db.backref(
        'document_versions', lazy='dynamic'
    ))

    def __repr__(self):
        return f'<DocumentVersion v{self.version_number} {self.document_type} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Back-Date Request (controlled back-dating with approval workflow)
# ---------------------------------------------------------------------------

class BackDateRequest(db.Model):
    """Tracks requests to back-date entries, requiring HOD/Deputy approval."""
    __tablename__ = 'back_date_requests'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    field_name = db.Column(db.String(100), nullable=False)  # e.g. 'date_received', 'report_date'
    original_date = db.Column(db.String(50), nullable=False)
    proposed_date = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    requested_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    requested_at = db.Column(db.DateTime, default=jamaica_now)
    status = db.Column(db.String(20), nullable=False, default='pending')  # 'pending', 'approved', 'denied'
    decided_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_comments = db.Column(db.Text, nullable=True)

    sample = db.relationship('Sample', backref=db.backref(
        'back_date_requests', lazy='dynamic', cascade='all, delete-orphan'
    ))
    requester = db.relationship('User', foreign_keys=[requested_by])
    decider = db.relationship('User', foreign_keys=[decided_by])

    def __repr__(self):
        return f'<BackDateRequest {self.field_name} for Sample {self.sample_id} ({self.status})>'


# ---------------------------------------------------------------------------
# Financial Year Utilities
# ---------------------------------------------------------------------------

def fiscal_year_for_date(d):
    """Return the financial year (integer) for a given date.
    Financial year runs April 1 – March 31.
    E.g. April 2025 → FY 2025, March 2026 → FY 2025."""
    if isinstance(d, datetime):
        d = d.date()
    if d.month >= 4:
        return d.year
    return d.year - 1


def fiscal_quarter_for_date(d):
    """Return the fiscal quarter (1-4) for a given date.
    Q1: Apr-Jun, Q2: Jul-Sep, Q3: Oct-Dec, Q4: Jan-Mar."""
    if isinstance(d, datetime):
        d = d.date()
    month = d.month
    if month >= 4 and month <= 6:
        return 1
    elif month >= 7 and month <= 9:
        return 2
    elif month >= 10 and month <= 12:
        return 3
    else:  # Jan-Mar
        return 4


def fiscal_quarter_months(quarter):
    """Return (month_start, month_end) for a given fiscal quarter.
    Q1: (4,6), Q2: (7,9), Q3: (10,12), Q4: (1,3)."""
    mapping = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}
    return mapping.get(quarter, (1, 3))


def fiscal_year_date_range(year, quarter=None):
    """Return (start_date, end_date) for a fiscal year or specific quarter.
    If quarter is None, returns the full fiscal year range."""
    if quarter:
        month_start, month_end = fiscal_quarter_months(quarter)
        if quarter == 4:
            start = date(year + 1, month_start, 1)
            # End of March
            end = date(year + 1, 3, 31)
        else:
            start = date(year, month_start, 1)
            # Last day of end month
            if month_end == 6:
                end = date(year, 6, 30)
            elif month_end == 9:
                end = date(year, 9, 30)
            elif month_end == 12:
                end = date(year, 12, 31)
            else:
                end = date(year, month_end, 31)
        return start, end
    else:
        # Full fiscal year: April 1 of year to March 31 of year+1
        return date(year, 4, 1), date(year + 1, 3, 31)
