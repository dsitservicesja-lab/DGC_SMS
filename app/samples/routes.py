import json
import os
import uuid
from datetime import datetime, timezone, date, timedelta

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_from_directory, abort, jsonify,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.samples import samples_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, User, SupportingDocument,
    Role, Branch, SampleStatus, AssignmentStatus, Permission,
    user_roles, user_branches, jamaica_now,
    DocumentVersion, ReviewHistory, BackDateRequest,
    AuditLog, Notification, Setting, DeleteRequest,
    KpiTarget, fiscal_year_for_date, fiscal_quarter_for_date,
    calculate_working_days, fetch_non_working_days, add_working_days,
    Invoice, InvoiceItem, PHARMA_TEST_PRICES, DropdownConfig,
)
from app.forms import (
    SampleRegisterForm, SampleEditForm, SampleAssignForm,
    ReportSubmitForm, PreliminaryReviewForm, ReportReviewForm,
    SubmitToDeputyForm, DeputyReviewForm, CertificateForm, HODReviewForm,
    get_sample_register_form, SupportingDocumentForm, BackDateRequestForm,
    DeleteRequestForm, COADecertifyForm, COAReissueForm,
    InvoiceCreateForm, InvoiceItemForm,
    BRANCH_TEST_NAMES, BRANCH_TEST_REFERENCES,
)
from app.notifications import (
    notify_sample_uploaded, notify_sample_assigned,
    notify_report_submitted, notify_preliminary_review_completed,
    notify_report_reviewed, notify_submitted_to_deputy,
    notify_deputy_review_completed, notify_certificate_prepared,
    notify_certificate_signed, notify_assignment_removed,
    notify_backdate_request_submitted,
    notify_delete_request_submitted,
)


def _save_file(file_storage):
    """Save an uploaded file and return (stored_name, original_name).

    Aborts with 400 if the file extension is not in ALLOWED_EXTENSIONS.
    """
    original = secure_filename(file_storage.filename)
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else ''
    if not ext:
        abort(400, description='Files without an extension are not permitted.')
    if ext not in current_app.config['ALLOWED_EXTENSIONS']:
        abort(400, description=f'File type ".{ext}" is not permitted.')
    stored = f'{uuid.uuid4().hex}.{ext}'
    file_storage.save(
        os.path.join(current_app.config['UPLOAD_FOLDER'], stored)
    )
    return stored, original


def _get_field(form, name):
    """Return the value of an optional form field, or None if absent/empty."""
    field = getattr(form, name, None)
    if field is None:
        return None
    return field.data or None


def _add_history(sample, action, details=None, action_type=None,
                 object_affected=None, change_description=None):
    entry = SampleHistory(
        sample_id=sample.id,
        action=action,
        details=details,
        performed_by=current_user.id,
        action_type=action_type or action,
        object_affected=object_affected,
        change_description=change_description,
    )
    db.session.add(entry)


# ---------------------------------------------------------------------------
# List / Dashboard views
# ---------------------------------------------------------------------------

@samples_bp.route('/')
@login_required
def sample_list():
    query = Sample.query

    # Filters
    status_filters = request.args.getlist('status')
    status_filter = status_filters[0] if len(status_filters) == 1 else None
    type_filter = request.args.get('type')
    search = request.args.get('q', '').strip()

    if len(status_filters) > 1:
        valid_statuses = []
        for sf in status_filters:
            try:
                valid_statuses.append(SampleStatus[sf])
            except KeyError:
                pass
        if valid_statuses:
            query = query.filter(Sample.status.in_(valid_statuses))
    elif status_filter:
        try:
            query = query.filter(Sample.status == SampleStatus[status_filter])
        except KeyError:
            pass
    if type_filter:
        try:
            query = query.filter(Sample.sample_type == Branch[type_filter])
        except KeyError:
            pass
    if search:
        query = query.filter(
            db.or_(
                Sample.lab_number.ilike(f'%{search}%'),
                Sample.sample_name.ilike(f'%{search}%'),
            )
        )

    # Role-based filtering
    # Officers, Deputies, HOD, and Admins see all samples (no filter).
    if current_user.has_role(Role.CHEMIST) and not current_user.has_any_role(Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        # Chemists see only samples assigned to them
        assigned_ids = db.select(SampleAssignment.sample_id).where(
            SampleAssignment.chemist_id == current_user.id
        ).scalar_subquery()
        query = query.filter(Sample.id.in_(assigned_ids))
    elif current_user.has_role(Role.SENIOR_CHEMIST) and current_user.branches and not current_user.has_any_role(Role.OFFICER, Role.HOD, Role.ADMIN):
        # Senior Chemists see samples in their branch(es)
        query = query.filter(Sample.sample_type.in_(current_user.branches))

    # Sorting
    _sort_columns = {
        'lab_number':           Sample.lab_number,
        'sample_name':          Sample.sample_name,
        'date_received':        Sample.date_received,
        'expected_report_date': Sample.expected_report_date,
        'status':               Sample.status,
    }
    sort_by = request.args.get('sort', 'date_registered')
    sort_dir = request.args.get('dir', 'desc')
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    sort_col = _sort_columns.get(sort_by, Sample.date_registered)
    if sort_dir == 'asc':
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=25, error_out=False)
    result_count = pagination.total

    # Compute per-sample working-day countdown to expected_report_date.
    # For samples without an explicit expected_report_date, derive a virtual
    # deadline from date_registered + KPI TAT target days for the sample's lab.
    terminal_statuses = {SampleStatus.CERTIFIED, SampleStatus.COMPLETED, SampleStatus.REJECTED}
    today = date.today()

    # Branch → KPI metric key that holds the TAT (turnaround) target in days.
    _BRANCH_TAT_KEY = {
        Branch.TOXICOLOGY:        'avg_days_toxicology_roa',
        Branch.PHARMACEUTICAL:    'avg_days_pharma_coa',
        Branch.PHARMACEUTICAL_NR: 'avg_days_pharma_coa',
        Branch.FOOD_MILK:         'avg_days_milk_coa',
        Branch.FOOD_ALCOHOL:      'avg_days_alcohol_coa',
    }

    # Collect samples that need a virtual deadline (no explicit date, not done).
    samples_needing_virtual = [
        s for s in pagination.items
        if s.expected_report_date is None
        and s.status not in terminal_statuses
        and s.sample_type in _BRANCH_TAT_KEY
        and s.date_registered is not None
    ]

    # Fetch KPI targets for the fiscal year/quarter of each sample's date_registered.
    kpi_targets_map = {}  # (kpi_key, year, quarter) -> target_value (days)
    fallback_targets_map = {}  # kpi_key -> latest target_value across any quarter
    if samples_needing_virtual:
        kpi_lookups = set()
        for s in samples_needing_virtual:
            kpi_key = _BRANCH_TAT_KEY[s.sample_type]
            fy = fiscal_year_for_date(s.date_registered)
            fq = fiscal_quarter_for_date(s.date_registered)
            kpi_lookups.add((kpi_key, fy, fq))
        conditions = [
            db.and_(
                KpiTarget.kpi_key == k,
                KpiTarget.year == y,
                KpiTarget.quarter == q,
            )
            for k, y, q in kpi_lookups
        ]
        rows = KpiTarget.query.filter(db.or_(*conditions)).all()
        for row in rows:
            kpi_targets_map[(row.kpi_key, row.year, row.quarter)] = row.target_value

        # Fallback: if no target for a specific quarter, use the latest available
        # target for that kpi_key across all quarters.
        missing_keys = {
            _BRANCH_TAT_KEY[s.sample_type]
            for s in samples_needing_virtual
            if (_BRANCH_TAT_KEY[s.sample_type],
                fiscal_year_for_date(s.date_registered),
                fiscal_quarter_for_date(s.date_registered)) not in kpi_targets_map
        }
        if missing_keys:
            fallback_rows = KpiTarget.query.filter(
                KpiTarget.kpi_key.in_(missing_keys),
                KpiTarget.target_value.isnot(None),
            ).order_by(KpiTarget.year.desc(), KpiTarget.quarter.desc()).all()
            fallback_targets_map = {}  # kpi_key -> latest target_value (any quarter)
            for row in fallback_rows:
                if row.kpi_key not in fallback_targets_map:
                    fallback_targets_map[row.kpi_key] = row.target_value

    # Helper: look up TAT target days for a sample (quarter-specific, then fallback).
    def _get_target_days(s):
        kpi_key = _BRANCH_TAT_KEY[s.sample_type]
        fy = fiscal_year_for_date(s.date_registered)
        fq = fiscal_quarter_for_date(s.date_registered)
        return kpi_targets_map.get((kpi_key, fy, fq)) or fallback_targets_map.get(kpi_key)

    # Estimate rough upper bound for virtual deadlines to size the holiday window.
    # Each working day requires at most ~2 calendar days on average (weekends)
    # plus a 10-day buffer for public holidays.
    rough_virtual_ends = []
    for s in samples_needing_virtual:
        target_days = _get_target_days(s)
        if target_days and target_days > 0:
            reg_date = s.date_registered.date() if isinstance(s.date_registered, datetime) else s.date_registered
            rough_virtual_ends.append(
                reg_date + timedelta(days=int(target_days) * 2 + 10)
            )

    # Pre-fetch holidays once for the full date window to avoid N+1 queries.
    all_deadline_dates = (
        [s.expected_report_date for s in pagination.items if s.expected_report_date is not None]
        + rough_virtual_ends
    )
    if all_deadline_dates:
        window_start = min(today, min(all_deadline_dates))
        window_end = max(today, max(all_deadline_dates))
        holidays = fetch_non_working_days(window_start, window_end)
    else:
        holidays = set()

    # Compute virtual deadlines (date_registered + KPI target working days).
    virtual_deadlines = {}
    for s in samples_needing_virtual:
        target_days = _get_target_days(s)
        if target_days and target_days > 0:
            virtual_deadlines[s.id] = add_working_days(
                s.date_registered, int(target_days), holidays
            )

    tat_remaining = {}
    for sample in pagination.items:
        if sample.status in terminal_statuses:
            tat_remaining[sample.id] = None
            continue
        deadline = sample.expected_report_date or virtual_deadlines.get(sample.id)
        if deadline is None:
            tat_remaining[sample.id] = None
        elif deadline >= today:
            # Working days remaining (0 = due today, >0 = future)
            tat_remaining[sample.id] = (
                calculate_working_days(today, deadline, holidays) - 1
            )
        else:
            # Overdue: count working days from the deadline up to (but not including)
            # today, then negate to produce a negative "days remaining" value.
            tat_remaining[sample.id] = -(
                calculate_working_days(deadline, today - timedelta(days=1), holidays)
            )

    return render_template(
        'samples/sample_list.html',
        samples=pagination.items,
        pagination=pagination,
        SampleStatus=SampleStatus,
        Branch=Branch,
        status_filter=status_filter,
        status_filters=status_filters,
        type_filter=type_filter,
        search=search,
        result_count=result_count,
        is_filtered=bool(status_filters or type_filter or search),
        today_date=today,
        sort_by=sort_by,
        sort_dir=sort_dir,
        tat_remaining=tat_remaining,
        terminal_statuses=terminal_statuses,
    )


# ---------------------------------------------------------------------------
# Register a new sample
# ---------------------------------------------------------------------------

def _generate_lab_number(branch):
    """Auto-generate a unique lab number for pharmaceutical samples."""
    from datetime import date
    prefix_map = {
        Branch.PHARMACEUTICAL: 'PH',
        Branch.PHARMACEUTICAL_NR: 'PNR',
    }
    prefix = prefix_map.get(branch, 'LAB')
    year = date.today().year
    # Find the latest number with this prefix+year
    pattern = f'{prefix}{year}%'
    last = Sample.query.filter(
        Sample.lab_number.like(pattern)
    ).order_by(Sample.id.desc()).first()
    if last:
        try:
            seq = int(last.lab_number[len(prefix) + 4:]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f'{prefix}{year}{seq:04d}'


@samples_bp.route('/register', methods=['GET', 'POST'])
@login_required
def register():
    if not (
        current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD)
        or current_user.has_permission(Permission.REGISTER_SAMPLE)
    ):
        flash('Only officers can register samples.', 'danger')
        return redirect(url_for('samples.sample_list'))

    selected_type = request.form.get('sample_type') if request.method == 'POST' else request.args.get('type')
    if not selected_type:
        selected_type = next(iter(Branch)).name
    FormClass = get_sample_register_form(selected_type)
    form = FormClass()

    if request.method == 'GET' and selected_type in Branch.__members__:
        form.sample_type.data = selected_type

    is_pharma = selected_type in ('PHARMACEUTICAL', 'PHARMACEUTICAL_NR')

    if form.validate_on_submit():
        branch = Branch[form.sample_type.data]
        is_pharma_type = branch in (Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR)

        # Auto-generate lab number for pharmaceutical samples if not provided
        lab_number = form.lab_number.data if hasattr(form, 'lab_number') and form.lab_number.data else None
        if not lab_number and is_pharma_type:
            lab_number = _generate_lab_number(branch)
        elif not lab_number:
            flash('Lab number is required.', 'danger')
            return render_template('samples/register.html', form=form, is_pharma=is_pharma)

        # Date received is always manually entered
        date_received = form.date_received.data

        sample = Sample(
            lab_number=lab_number,
            sample_name=form.sample_name.data,
            sample_type=branch,
            description=form.description.data,
            quantity=_get_field(form, 'quantity'),
            parish=_get_field(form, 'parish'),
            patient_name=_get_field(form, 'patient_name'),
            source=_get_field(form, 'source'),
            date_received=date_received,
            expected_report_date=_get_field(form, 'expected_report_date'),
            uploaded_by=current_user.id,
        )

        # Type-specific fields
        vol = _get_field(form, 'volume')
        if vol:
            sample.volume = vol
        mt = _get_field(form, 'milk_type')
        if mt:
            sample.milk_type = mt
        ft = _get_field(form, 'formulation_type')
        if ft:
            sample.formulation_type = ft
        ai = _get_field(form, 'active_ingredient')
        if ai:
            sample.active_ingredient = ai
        at = _get_field(form, 'alcohol_type')
        if at:
            sample.alcohol_type = at
        cb = _get_field(form, 'claim_butt_number')
        if cb:
            sample.claim_butt_number = cb
        # New fields
        tst = _get_field(form, 'toxicology_sample_type_name')
        if tst:
            sample.toxicology_sample_type_name = tst
        ln = _get_field(form, 'lot_number')
        if ln:
            sample.lot_number = ln
        ed = _get_field(form, 'expiration_date')
        if ed:
            sample.expiration_date = ed
        bln = _get_field(form, 'batch_lot_number')
        if bln:
            sample.batch_lot_number = bln
        # Toxicology clinical fields
        dn = _get_field(form, 'doctors_name')
        if dn:
            sample.doctors_name = dn
        rdn = _get_field(form, 'registration_docket_no')
        if rdn:
            sample.registration_docket_no = rdn
        pg = _get_field(form, 'patient_gender')
        if pg:
            sample.patient_gender = pg
        wc = _get_field(form, 'ward_clinic')
        if wc:
            sample.ward_clinic = wc
        tr = _get_field(form, 'test_requested')
        if tr:
            sample.test_requested = tr
        di = _get_field(form, 'diagnosis_indicated')
        if di:
            sample.diagnosis_indicated = di

        if form.scanned_file.data:
            stored, original = _save_file(form.scanned_file.data)
            sample.scanned_file = stored
            sample.scanned_file_original_name = original

        db.session.add(sample)
        try:
            db.session.flush()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception('Failed to flush new sample %r', lab_number)
            flash(f'An error occurred while registering the sample: {exc}', 'danger')
            return render_template('samples/register.html', form=form, is_pharma=is_pharma)

        # Create document version for scanned file
        if form.scanned_file.data:
            db.session.add(DocumentVersion(
                sample_id=sample.id,
                document_type='scanned_file',
                version_number=1,
                file_path=sample.scanned_file,
                original_name=sample.scanned_file_original_name,
                upload_label='original',
                uploaded_by=current_user.id,
            ))

        _add_history(sample, 'Sample Registered',
                     f'Registered by {current_user.full_name}',
                     action_type='Original Submission',
                     object_affected='Sample')
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception('Failed to commit new sample %r', lab_number)
            flash(f'An error occurred while registering the sample: {exc}', 'danger')
            return render_template('samples/register.html', form=form, is_pharma=is_pharma)

        notify_sample_uploaded(sample)
        db.session.commit()

        flash(f'Sample {sample.lab_number} registered successfully.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template('samples/register.html', form=form, is_pharma=is_pharma)


# ---------------------------------------------------------------------------
# Sample detail
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>')
@login_required
def detail(sample_id):
    sample = db.get_or_404(Sample, sample_id)
    assignments = sample.assignments.all()
    history = sample.history.all()
    supporting_docs = sample.supporting_documents.order_by(
        SupportingDocument.uploaded_at.desc()
    ).all()
    supporting_doc_form = SupportingDocumentForm()
    document_versions = sample.document_versions.order_by(
        DocumentVersion.document_type, DocumentVersion.version_number.desc()
    ).all() if hasattr(sample, 'document_versions') else []
    review_histories = ReviewHistory.query.filter_by(
        sample_id=sample.id
    ).order_by(ReviewHistory.review_type, ReviewHistory.review_number).all()
    pending_backdate = BackDateRequest.query.filter_by(
        sample_id=sample.id, field_name='date_registered', status='pending',
    ).first()
    return render_template(
        'samples/detail.html',
        sample=sample,
        assignments=assignments,
        history=history,
        supporting_docs=supporting_docs,
        supporting_doc_form=supporting_doc_form,
        today_date=date.today(),
        document_versions=document_versions,
        review_histories=review_histories,
        pending_backdate=pending_backdate,
    )


# ---------------------------------------------------------------------------
# Edit sample
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(sample_id):
    sample = db.get_or_404(Sample, sample_id)
    can_edit = (
        current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD, Role.SENIOR_CHEMIST)
        or current_user.has_permission(Permission.EDIT_SAMPLE)
        or current_user.id == sample.uploaded_by
    )
    if not can_edit:
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleEditForm(obj=sample)
    # Ensure the Laboratory dropdown is pre-selected with the current value.
    # obj=sample sets sample_type to the Branch enum, but the SelectField
    # expects the enum .name string to match its choices.
    if request.method == 'GET' and sample.sample_type:
        form.sample_type.data = sample.sample_type.name
        form.lab_number.data = sample.lab_number
        if sample.active_ingredient:
            form.active_ingredient.data = sample.active_ingredient
    if form.validate_on_submit():
        new_lab_number = form.lab_number.data.strip()
        if new_lab_number != sample.lab_number:
            conflict = Sample.query.filter(
                Sample.lab_number == new_lab_number,
                Sample.id != sample.id,
            ).first()
            if conflict:
                flash(f'Lab number "{new_lab_number}" is already in use by another sample.', 'danger')
                return render_template('samples/edit.html', form=form, sample=sample)
            sample.lab_number = new_lab_number
        sample.sample_name = form.sample_name.data
        sample.sample_type = Branch[form.sample_type.data]
        sample.description = form.description.data
        sample.quantity = form.quantity.data
        sample.volume = form.volume.data
        sample.parish = form.parish.data
        sample.patient_name = form.patient_name.data
        sample.source = form.source.data
        sample.formulation_type = form.formulation_type.data
        sample.active_ingredient = form.active_ingredient.data or None
        sample.alcohol_type = form.alcohol_type.data if form.alcohol_type.data else None
        sample.claim_butt_number = form.claim_butt_number.data
        sample.batch_lot_number = form.batch_lot_number.data or None
        sample.milk_type = form.milk_type.data if form.milk_type.data else None
        sample.lot_number = form.lot_number.data or None
        sample.expiration_date = form.expiration_date.data
        sample.toxicology_sample_type_name = form.toxicology_sample_type_name.data or None
        sample.doctors_name = form.doctors_name.data or None
        sample.registration_docket_no = form.registration_docket_no.data or None
        sample.patient_gender = form.patient_gender.data or None
        sample.ward_clinic = form.ward_clinic.data or None
        sample.test_requested = form.test_requested.data or None
        sample.diagnosis_indicated = form.diagnosis_indicated.data or None
        sample.expected_report_date = form.expected_report_date.data

        scanned_upload = form.scanned_file.data
        if (
            scanned_upload
            and hasattr(scanned_upload, 'filename')
            and scanned_upload.filename
        ):
            stored, original = _save_file(scanned_upload)
            sample.scanned_file = stored
            sample.scanned_file_original_name = original

            # Create document version for replaced scanned file
            existing = DocumentVersion.query.filter_by(
                sample_id=sample.id, document_type='scanned_file'
            ).count()
            db.session.add(DocumentVersion(
                sample_id=sample.id,
                document_type='scanned_file',
                version_number=existing + 1,
                file_path=stored,
                original_name=original,
                upload_label='revised',
                uploaded_by=current_user.id,
            ))

        _add_history(sample, 'Sample Updated', f'Updated by {current_user.full_name}',
                     action_type='Edit', object_affected='Sample')
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception('Failed to save edits for sample %r', sample.lab_number)
            flash(f'An error occurred while saving the sample: {exc}', 'danger')
            return render_template('samples/edit.html', form=form, sample=sample)
        flash('Sample updated.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template('samples/edit.html', form=form, sample=sample)


# ---------------------------------------------------------------------------
# Assign sample to chemist(s)
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/assign', methods=['GET', 'POST'])
@login_required
def assign(sample_id):
    sample = db.get_or_404(Sample, sample_id)
    if not (
        current_user.is_branch_head()
        or current_user.has_role(Role.ADMIN)
        or current_user.has_permission(Permission.ASSIGN_SAMPLE)
    ):
        flash('Only Senior Chemists / Branch Heads can assign samples.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleAssignForm()

    # Determine if this sample type has predefined test names/references
    branch_key = sample.sample_type.name
    predefined_tests = BRANCH_TEST_NAMES.get(branch_key)
    predefined_refs = BRANCH_TEST_REFERENCES.get(branch_key)
    has_predefined_tests = bool(predefined_tests)
    has_predefined_refs = bool(predefined_refs)

    if has_predefined_tests:
        form.test_names.choices = predefined_tests
    if has_predefined_refs:
        form.test_reference_select.choices = [('', '-- Select Reference --')] + predefined_refs
    chemists = User.query.filter(
        User.is_active_user.is_(True),
    ).join(user_roles).filter(
        user_roles.c.role == Role.CHEMIST,
    )
    if current_user.branches:
        chemists = chemists.join(user_branches).filter(
            user_branches.c.branch.in_(current_user.branches)
        )
    chemists = chemists.order_by(User.last_name).all()
    form.chemist_ids.choices = [(c.id, c.full_name) for c in chemists]

    if form.validate_on_submit():
        # Determine test names: from multi-select dropdown or free-text
        if has_predefined_tests and form.test_names.data:
            selected_test_names = form.test_names.data
        elif form.test_name.data:
            selected_test_names = [form.test_name.data]
        else:
            flash('At least one test name is required.', 'danger')
            return render_template(
                'samples/assign.html', form=form, sample=sample,
                today=jamaica_now().date().isoformat(),
                has_predefined_tests=has_predefined_tests,
                has_predefined_refs=has_predefined_refs,
            )

        # Determine test reference(s) – multi-select supported
        if has_predefined_refs and form.test_reference_select.data:
            # Filter out empty values from multi-select
            selected_refs = [r for r in form.test_reference_select.data if r]
            # Use semicolon delimiter for robust parsing when multiple refs selected
            test_ref = '; '.join(selected_refs) if selected_refs else form.test_reference.data
        else:
            test_ref = form.test_reference.data

        assignment_count = 0
        skipped_count = 0
        for chemist_id in form.chemist_ids.data:
            for test_name in selected_test_names:
                # Prevent duplicate assignments for the same
                # (sample, chemist, test_name) combination.
                existing = SampleAssignment.query.filter_by(
                    sample_id=sample.id,
                    chemist_id=chemist_id,
                    test_name=test_name,
                ).filter(
                    SampleAssignment.status.notin_([
                        AssignmentStatus.REJECTED,
                    ])
                ).first()
                if existing:
                    skipped_count += 1
                    continue

                assignment = SampleAssignment(
                    sample_id=sample.id,
                    chemist_id=chemist_id,
                    assigned_by=current_user.id,
                    test_name=test_name,
                    test_reference=test_ref,
                    expected_completion=form.expected_completion.data,
                    comments=form.comments.data or None,
                    quantity_volume=form.quantity_volume.data or None,
                    oos_investigation=bool(form.oos_investigation.data),
                )
                db.session.add(assignment)
                db.session.flush()
                notify_sample_assigned(assignment)
                assignment_count += 1

        if assignment_count == 0:
            db.session.rollback()
            flash('All selected test/chemist combinations are already assigned.', 'warning')
            return redirect(url_for('samples.detail', sample_id=sample.id))

        sample.status = SampleStatus.ASSIGNED
        # Collect assigned chemist names for the history detail
        assigned_chemist_names = [
            c.full_name for c in chemists
            if c.id in form.chemist_ids.data
        ]
        test_list = ', '.join(selected_test_names)
        chemist_list = ', '.join(assigned_chemist_names) if assigned_chemist_names else 'N/A'
        _add_history(
            sample, 'Sample Assigned',
            (f'{current_user.full_name} assigned {assignment_count} test(s) '
             f'({test_list}) to {chemist_list}'),
            action_type='Assignment',
            object_affected='Sample Assignment',
            change_description=(
                f'Tests: {test_list}; '
                f'Assigned to: {chemist_list}; '
                f'Assigned by: {current_user.full_name}'),
        )
        db.session.commit()
        if skipped_count > 0:
            flash(f'Sample assigned successfully. {skipped_count} duplicate assignment(s) skipped.', 'success')
        else:
            flash('Sample assigned successfully.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template('samples/assign.html', form=form, sample=sample,
                           today=jamaica_now().date().isoformat(),
                           has_predefined_tests=has_predefined_tests,
                           has_predefined_refs=has_predefined_refs)


# ---------------------------------------------------------------------------
# Assignment detail (for chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>')
@login_required
def assignment_detail(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    # Access: the assigned chemist, branch heads, officer who uploaded, admin
    if not _can_view_assignment(assignment):
        abort(403)
    review_histories = ReviewHistory.query.filter_by(
        assignment_id=assignment.id
    ).order_by(ReviewHistory.review_type, ReviewHistory.review_number).all()
    return render_template(
        'samples/assignment_detail.html',
        assignment=assignment,
        review_histories=review_histories,
    )


def _can_view_assignment(assignment):
    if current_user.has_role(Role.ADMIN):
        return True
    if current_user.id == assignment.chemist_id:
        return True
    if current_user.id == assignment.sample.uploaded_by:
        return True
    if current_user.has_role(Role.OFFICER):
        return True
    if current_user.is_branch_head():
        if not current_user.branches or assignment.sample.sample_type in current_user.branches:
            return True
    return False


# ---------------------------------------------------------------------------
# Remove assignment (Admin, HOD, or the user who assigned)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/remove', methods=['POST'])
@login_required
def remove_assignment(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    sample = assignment.sample

    # Only Admin, HOD, Senior Chemist, or the user who made the assignment can remove it
    can_remove = (
        current_user.has_any_role(Role.ADMIN, Role.HOD, Role.SENIOR_CHEMIST)
        or current_user.id == assignment.assigned_by
    )
    if not can_remove:
        flash('You do not have permission to remove this assignment.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    chemist_name = assignment.chemist.full_name
    test_name = assignment.test_name
    chemist_id = assignment.chemist_id
    sample_ref = sample.lab_number
    _add_history(
        sample, 'Assignment Removed',
        (f'{current_user.full_name} removed assignment of test '
         f'"{test_name}" from {chemist_name}.'),
        action_type='Assignment Removed',
        object_affected='Sample Assignment',
        change_description=(
            f'Test "{test_name}" unassigned from {chemist_name} '
            f'by {current_user.full_name}'),
    )

    db.session.delete(assignment)

    # Update sample status based on remaining assignments
    remaining = sample.assignments.filter(
        SampleAssignment.id != assignment_id
    ).all()
    if not remaining:
        sample.status = SampleStatus.REGISTERED
    else:
        _update_sample_status(sample)

    db.session.commit()

    # Notify the removed assignee
    notify_assignment_removed(
        chemist_id, sample_ref, test_name, current_user.full_name, sample.id
    )
    db.session.commit()

    flash(f'Assignment of "{test_name}" to {chemist_name} has been removed.', 'success')
    return redirect(url_for('samples.detail', sample_id=sample.id))


# ---------------------------------------------------------------------------
# Return assignment to analyst (Senior Chemist / HOD at any stage)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/return-to-analyst', methods=['POST'])
@login_required
def return_to_analyst(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    sample = assignment.sample

    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD, Role.ADMIN):
        flash('Only Senior Chemists, HOD, or Admins can return assignments to the analyst.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    # Cannot return an assignment that is already with the analyst
    non_returnable = {AssignmentStatus.ASSIGNED, AssignmentStatus.RETURNED}
    if assignment.status in non_returnable:
        flash('Assignment is already assigned to the analyst.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    old_status = assignment.status.value
    assignment.status = AssignmentStatus.RETURNED
    assignment.return_stage = 'technical'
    assignment.date_completed = None

    chemist_name = assignment.chemist.full_name if assignment.chemist else 'Unknown'
    _add_history(
        sample,
        'Returned to Analyst',
        (f'{current_user.full_name} returned assignment for test '
         f'"{assignment.test_name}" to analyst {chemist_name} '
         f'(was: {old_status}).'),
        action_type='Return to Analyst',
        object_affected='Sample Assignment',
        change_description=(
            f'Test "{assignment.test_name}" returned to {chemist_name} '
            f'by {current_user.full_name} (from {old_status})'),
    )

    _update_sample_status(sample)
    db.session.commit()

    # Notify the analyst
    from app.notifications import create_notification
    create_notification(
        assignment.chemist_id,
        f'Assignment Returned: {sample.lab_number}',
        (f'Your assignment for test "{assignment.test_name}" on sample '
         f'"{sample.sample_name}" (Lab# {sample.lab_number}) has been '
         f'returned to you for correction by {current_user.full_name}.'),
        f'/samples/assignment/{assignment.id}',
    )
    db.session.commit()

    flash(f'Assignment returned to analyst {chemist_name}.', 'success')
    return redirect(url_for('samples.detail', sample_id=sample.id))


# ---------------------------------------------------------------------------
# Edit assignment (Senior Chemist, HOD, Admin)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_assignment(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    sample = assignment.sample

    # Senior Chemist, HOD, and Admin can edit assignments
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD, Role.ADMIN):
        flash('Only Senior Chemists, HOD, or Admins can edit assignments.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    branch_key = sample.sample_type.name
    predefined_tests = BRANCH_TEST_NAMES.get(branch_key)
    predefined_refs = BRANCH_TEST_REFERENCES.get(branch_key)

    # Populate chemist choices
    chemists = User.query.filter(
        User.is_active_user.is_(True),
    ).join(user_roles).filter(
        user_roles.c.role == Role.CHEMIST,
    )
    if current_user.branches:
        chemists = chemists.join(user_branches).filter(
            user_branches.c.branch.in_(current_user.branches)
        )
    chemists = chemists.order_by(User.last_name).all()

    if request.method == 'POST':
        new_chemist_id = request.form.get('chemist_id', type=int)
        new_test_name = request.form.get('test_name', '').strip()
        new_test_reference = request.form.get('test_reference', '').strip()
        new_expected = request.form.get('expected_completion', '').strip()
        new_comments = request.form.get('comments', '').strip()

        changes = []
        if new_chemist_id and new_chemist_id != assignment.chemist_id:
            old_chemist = assignment.chemist.full_name if assignment.chemist else 'Unknown'
            new_chemist = db.session.get(User, new_chemist_id)
            if not new_chemist:
                flash('Selected chemist not found.', 'danger')
                return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))
            assignment.chemist_id = new_chemist_id
            changes.append(f'Reassigned: {old_chemist} → {new_chemist.full_name}')
            # Notify the new chemist
            notify_sample_assigned(assignment)
        if new_test_name and new_test_name != assignment.test_name:
            changes.append(f'Test: {assignment.test_name} → {new_test_name}')
            assignment.test_name = new_test_name
        if new_test_reference != (assignment.test_reference or ''):
            assignment.test_reference = new_test_reference or None
            changes.append(f'Reference updated')
        if new_expected:
            from datetime import datetime as dt
            assignment.expected_completion = dt.strptime(new_expected, '%Y-%m-%d').date()
        if new_comments:
            assignment.comments = new_comments

        if changes:
            chemist_name = assignment.chemist.full_name if assignment.chemist else 'Unknown'
            _add_history(
                sample, 'Assignment Edited',
                (f'{current_user.full_name} edited assignment for '
                 f'{chemist_name}: {"; ".join(changes)}'),
                action_type='Assignment Edit',
                object_affected='Sample Assignment',
                change_description='; '.join(changes) + f' (by {current_user.full_name})',
            )
            db.session.commit()
            flash('Assignment updated.', 'success')
        else:
            flash('No changes made.', 'info')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/edit_assignment.html',
        assignment=assignment,
        sample=sample,
        chemists=chemists,
        predefined_tests=predefined_tests or [],
        predefined_refs=predefined_refs or [],
        today=jamaica_now().date().isoformat(),
    )


# ---------------------------------------------------------------------------
# Supporting documents upload
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/upload-supporting-doc', methods=['GET', 'POST'])
@login_required
def upload_supporting_document(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    # Only Officers, Admin, HOD, GC Assistants, or the sample uploader may add documents
    if not (
        current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD, Role.GOVT_CHEMIST_ASSISTANT)
        or current_user.id == sample.uploaded_by
    ):
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SupportingDocumentForm()

    if form.validate_on_submit():
        stored, original = _save_file(form.file.data)
        doc = SupportingDocument(
            sample_id=sample.id,
            file_path=stored,
            original_name=original,
            description=form.description.data or None,
            uploaded_by=current_user.id,
        )
        db.session.add(doc)
        _add_history(sample, 'Supporting Document Uploaded',
                     f'{current_user.full_name} uploaded "{original}"',
                     action_type='Document Upload',
                     object_affected='Supporting Document',
                     change_description=f'File: {original} (uploaded by {current_user.full_name})')
        db.session.commit()
        flash('Supporting document uploaded.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/upload_supporting_doc.html', form=form, sample=sample
    )


# ---------------------------------------------------------------------------
# Submit report (chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/report', methods=['GET', 'POST'])
@login_required
def submit_report(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    if current_user.id != assignment.chemist_id:
        flash('Only the assigned chemist can submit a report.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status not in (
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.IN_PROGRESS,
        AssignmentStatus.RETURNED,
    ):
        flash('Report cannot be submitted in the current state.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Find all sibling assignments for the same sample assigned to this
    # chemist that are also in a submittable state.
    sibling_assignments = SampleAssignment.query.filter(
        SampleAssignment.sample_id == assignment.sample_id,
        SampleAssignment.chemist_id == current_user.id,
        SampleAssignment.status.in_([
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.IN_PROGRESS,
            AssignmentStatus.RETURNED,
        ]),
    ).all()

    today = jamaica_now().date()
    min_test_date = assignment.sample.date_received

    form = ReportSubmitForm()
    if form.validate_on_submit():
        now = jamaica_now()
        report_text = form.report_text.data
        all_returned = form.all_samples_returned.data or None
        return_qty = form.return_quantity.data or None
        test_date = form.test_date.data
        meets_spec = form.meets_specifications.data or None
        report_comments = form.report_comments.data or None

        # Validate test_date bounds (single-test case)
        if test_date:
            if test_date > today:
                flash('Test date cannot be in the future.', 'danger')
                return render_template(
                    'samples/submit_report.html', form=form, assignment=assignment,
                    sibling_assignments=sibling_assignments,
                    today=today.isoformat(), min_test_date=min_test_date.isoformat(),
                )
            if test_date < min_test_date:
                flash('Test date cannot be before the date the sample was received.', 'danger')
                return render_template(
                    'samples/submit_report.html', form=form, assignment=assignment,
                    sibling_assignments=sibling_assignments,
                    today=today.isoformat(), min_test_date=min_test_date.isoformat(),
                )

        stored = original = None
        if form.report_file.data:
            stored, original = _save_file(form.report_file.data)

            # Create document version for the report file
            from app.models import DocumentVersion
            existing_versions = DocumentVersion.query.filter_by(
                sample_id=assignment.sample_id, document_type='report',
                assignment_id=assignment.id,
            ).count()
            version_num = existing_versions + 1
            upload_label = 'original' if version_num == 1 else 'resubmission'
            db.session.add(DocumentVersion(
                sample_id=assignment.sample_id,
                document_type='report',
                version_number=version_num,
                file_path=stored,
                original_name=original,
                upload_label=upload_label,
                uploaded_by=current_user.id,
                assignment_id=assignment.id,
            ))

        # Check if per-test fields were submitted (multiple sibling assignments)
        has_per_test = len(sibling_assignments) > 1

        # Apply the report to all sibling assignments that are submittable
        date_error = None
        submitted_names = []
        for a in sibling_assignments:
            a.report_text = report_text
            a.report_submitted_at = now
            a.all_samples_returned = all_returned
            a.return_quantity = return_qty
            a.report_comments = report_comments

            if has_per_test:
                # Per-test test_date and meets_specifications from form fields
                per_test_date_str = request.form.get(f'test_date_{a.id}', '')
                per_meets_spec = request.form.get(f'meets_spec_{a.id}') or None
                if per_test_date_str:
                    try:
                        parsed_date = datetime.strptime(per_test_date_str, '%Y-%m-%d').date()
                        if parsed_date > today:
                            date_error = f'Test date for {a.test_name} cannot be in the future.'
                        elif parsed_date < min_test_date:
                            date_error = f'Test date for {a.test_name} cannot be before the date the sample was received.'
                        else:
                            a.test_date = parsed_date
                    except ValueError:
                        a.test_date = None
                else:
                    a.test_date = None
                a.meets_specifications = per_meets_spec
            else:
                a.test_date = test_date
                a.meets_specifications = meets_spec

        if date_error:
            flash(date_error, 'danger')
            return render_template(
                'samples/submit_report.html', form=form, assignment=assignment,
                sibling_assignments=sibling_assignments,
                today=today.isoformat(), min_test_date=min_test_date.isoformat(),
            )

        for a in sibling_assignments:
            if stored:
                a.report_file = stored
                a.report_file_original_name = original

            # Route to correct review stage based on where it was returned from
            if a.return_stage == 'technical':
                a.status = AssignmentStatus.UNDER_TECHNICAL_REVIEW
            else:
                a.status = AssignmentStatus.REPORT_SUBMITTED

            a.return_stage = None
            submitted_names.append(a.test_name)

        _add_history(
            assignment.sample, 'Report Submitted',
            (f'{current_user.full_name} submitted report for test(s): '
             f'{", ".join(submitted_names)}'),
            action_type='Report Submission',
            object_affected='Report',
            change_description=(
                f'Report submitted for: {", ".join(submitted_names)} '
                f'by {current_user.full_name}'),
        )

        # Update sample status
        _update_sample_status(assignment.sample)

        db.session.commit()

        notify_report_submitted(assignment)
        db.session.commit()

        test_count = len(submitted_names)
        if test_count > 1:
            flash(f'Report submitted for {test_count} tests successfully.', 'success')
        else:
            flash('Report submitted successfully.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Pre-fill if resubmitting
    if request.method == 'GET' and assignment.report_text:
        form.report_text.data = assignment.report_text
    if request.method == 'GET' and assignment.test_date:
        form.test_date.data = assignment.test_date
    if request.method == 'GET' and assignment.meets_specifications:
        form.meets_specifications.data = assignment.meets_specifications
    if request.method == 'GET' and assignment.report_comments:
        form.report_comments.data = assignment.report_comments

    return render_template(
        'samples/submit_report.html', form=form, assignment=assignment,
        sibling_assignments=sibling_assignments,
        today=today.isoformat(), min_test_date=min_test_date.isoformat(),
    )


# ---------------------------------------------------------------------------
# Preliminary review (Officer / Senior Chemist Technologist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/preliminary-review', methods=['GET', 'POST'])
@login_required
def preliminary_review(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)

    # Officers, Senior Chemists, Deputy, HOD, Admin, or the sample uploader
    # can do preliminary review.  Explicit PRELIMINARY_REVIEW permission also grants access.
    sample = assignment.sample
    allowed = (
        current_user.has_any_role(
            Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN
        )
        or current_user.has_permission(Permission.PRELIMINARY_REVIEW)
        or current_user.id == sample.uploaded_by
    )
    if not allowed:
        flash('You do not have permission to perform preliminary reviews.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status != AssignmentStatus.REPORT_SUBMITTED:
        flash('This report is not awaiting preliminary review.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Determine whether grouped review mode is enabled
    grouped_mode = Setting.get_bool('preliminary_review_grouped', default=False)

    # Find sibling assignments for the same sample that are also awaiting
    # preliminary review (REPORT_SUBMITTED).
    # In grouped mode: include ALL sibling assignments for the sample.
    # In per-test mode: only include this specific assignment.
    if grouped_mode:
        sibling_assignments = SampleAssignment.query.filter(
            SampleAssignment.sample_id == assignment.sample_id,
            SampleAssignment.status == AssignmentStatus.REPORT_SUBMITTED,
        ).all()
    else:
        sibling_assignments = [assignment]

    form = PreliminaryReviewForm()
    if form.validate_on_submit():
        action = form.action.data

        # Validation: If any checklist item is "No", only allow return
        if form.has_any_no() and action == 'approved':
            flash('Cannot approve: one or more checklist items are marked "No". '
                  'Please return for correction.', 'danger')
            return render_template(
                'samples/preliminary_review.html', form=form,
                assignment=assignment,
                sibling_assignments=sibling_assignments,
                grouped_mode=grouped_mode,
            )

        now = jamaica_now()
        comments = form.review_comments.data

        # Save checklist answers
        checklist = {}
        for _, fields in form.CHECKLIST_CATEGORIES:
            for field_name in fields:
                checklist[field_name] = getattr(form, field_name).data
        checklist_json = json.dumps(checklist)

        # Apply the review to all sibling assignments
        # Pre-fetch existing review counts to avoid N+1 queries
        sibling_ids = [a.id for a in sibling_assignments]
        existing_counts = dict(
            db.session.query(
                ReviewHistory.assignment_id,
                db.func.count(ReviewHistory.id)
            ).filter(
                ReviewHistory.assignment_id.in_(sibling_ids),
                ReviewHistory.review_type == 'preliminary'
            ).group_by(ReviewHistory.assignment_id).all()
        )

        reviewed_names = []
        for a in sibling_assignments:
            # Log the review in ReviewHistory BEFORE overwriting fields
            prev_count = existing_counts.get(a.id, 0)
            db.session.add(ReviewHistory(
                sample_id=sample.id,
                assignment_id=a.id,
                review_type='preliminary',
                review_number=prev_count + 1,
                action=action,
                reviewer_id=current_user.id,
                reviewed_at=now,
                comments=comments,
                checklist_data=checklist_json,
            ))

            a.preliminary_review_comments = comments
            a.preliminary_reviewed_by = current_user.id
            a.preliminary_reviewed_at = now
            a.preliminary_review_checklist = checklist_json

            if action == 'approved':
                a.status = AssignmentStatus.UNDER_TECHNICAL_REVIEW
            else:  # returned
                a.status = AssignmentStatus.RETURNED
                a.return_stage = 'preliminary'
                a.date_completed = None

            reviewed_names.append(a.test_name)

        test_list = ', '.join(reviewed_names)
        if action == 'approved':
            _add_history(
                sample, 'Preliminary Review Approved',
                (f'{current_user.full_name} approved preliminary review for '
                 f'test(s): {test_list}. '
                 f'Forwarded to Senior Chemist for technical review.'),
                action_type='Preliminary Review',
                object_affected='Report',
                change_description=(
                    f'Tests: {test_list} — approved by {current_user.full_name}, '
                    f'forwarded to Senior Chemist Review'),
            )
        else:
            _add_history(
                sample, 'Preliminary Review Returned',
                (f'{current_user.full_name} returned report for test(s): '
                 f'{test_list} for correction. '
                 f'Comments: {comments or "N/A"}'),
                action_type='Preliminary Review',
                object_affected='Report',
                change_description=(
                    f'Tests: {test_list} — returned for correction '
                    f'by {current_user.full_name}. Comments: {comments or "N/A"}'),
            )

        _update_sample_status(sample)
        db.session.commit()

        for a in sibling_assignments:
            notify_preliminary_review_completed(a, action)
        db.session.commit()

        action_text = 'approved and forwarded' if action == 'approved' else 'returned for correction'
        test_count = len(reviewed_names)
        if test_count > 1:
            flash(f'{test_count} reports have been {action_text}.', 'success')
        else:
            flash(f'Report has been {action_text}.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/preliminary_review.html', form=form,
        assignment=assignment, sibling_assignments=sibling_assignments,
        grouped_mode=grouped_mode,
    )


# ---------------------------------------------------------------------------
# Senior Chemist Review (formerly Technical Review)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/review', methods=['GET', 'POST'])
@login_required
def review_report(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)

    if not (
        current_user.is_branch_head()
        or current_user.has_role(Role.ADMIN)
        or current_user.has_permission(Permission.TECHNICAL_REVIEW)
    ):
        flash('Only Senior Chemists / Branch Heads can review reports.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status != AssignmentStatus.UNDER_TECHNICAL_REVIEW:
        flash('This report is not awaiting technical review.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Determine whether grouped review mode is enabled
    grouped_mode = Setting.get_bool('technical_review_grouped', default=False)

    # Find sibling assignments for the same sample that are also awaiting
    # technical review (UNDER_TECHNICAL_REVIEW).
    # In grouped mode: include ALL sibling assignments for the sample.
    # In per-test mode: only include this specific assignment.
    sample = assignment.sample
    if grouped_mode:
        sibling_assignments = SampleAssignment.query.filter(
            SampleAssignment.sample_id == sample.id,
            SampleAssignment.status == AssignmentStatus.UNDER_TECHNICAL_REVIEW,
        ).all()
    else:
        sibling_assignments = [assignment]

    form = ReportReviewForm()

    # Populate reassignment choices (only used in per-test mode)
    chemists = User.query.filter(
        User.is_active_user.is_(True),
    ).join(user_roles).filter(
        user_roles.c.role == Role.CHEMIST,
    )
    if current_user.branches:
        chemists = chemists.join(user_branches).filter(
            user_branches.c.branch.in_(current_user.branches)
        )
    chemists = chemists.order_by(User.last_name).all()
    form.reassign_chemist_id.choices = [(0, '-- No Reassignment --')] + [
        (c.id, c.full_name) for c in chemists
    ]

    if form.validate_on_submit():
        action = form.action.data
        now = jamaica_now()
        comments = form.review_comments.data
        out_of_spec_flag = form.out_of_spec.data

        # Pre-fetch existing review counts to avoid N+1 queries
        sibling_ids = [a.id for a in sibling_assignments]
        existing_counts = dict(
            db.session.query(
                ReviewHistory.assignment_id,
                db.func.count(ReviewHistory.id)
            ).filter(
                ReviewHistory.assignment_id.in_(sibling_ids),
                ReviewHistory.review_type == 'technical'
            ).group_by(ReviewHistory.assignment_id).all()
        )

        reviewed_names = []
        for a in sibling_assignments:
            prev_count = existing_counts.get(a.id, 0)
            db.session.add(ReviewHistory(
                sample_id=sample.id,
                assignment_id=a.id,
                review_type='technical',
                review_number=prev_count + 1,
                action=action,
                reviewer_id=current_user.id,
                reviewed_at=now,
                comments=comments,
            ))

            a.out_of_spec = out_of_spec_flag
            a.review_comments = comments
            a.reviewed_by = current_user.id
            a.reviewed_at = now

            if action == 'accepted':
                a.status = AssignmentStatus.ACCEPTED
                a.date_completed = now
            elif action == 'returned':
                a.status = AssignmentStatus.RETURNED
                a.return_stage = 'technical'
                a.date_completed = None
            elif action == 'rejected':
                a.status = AssignmentStatus.REJECTED
                a.date_completed = now

            reviewed_names.append(a.test_name)

        # Handle reassignment (per-test mode only — first/only sibling)
        reassign_msg = ''
        if not grouped_mode:
            single_assignment = sibling_assignments[0]
            chemist_name = single_assignment.chemist.full_name if single_assignment.chemist else 'Unknown'
            reassign_id = form.reassign_chemist_id.data
            if reassign_id and reassign_id != 0 and reassign_id != single_assignment.chemist_id:
                old_chemist_name = chemist_name
                new_chemist = db.session.get(User, reassign_id)
                if new_chemist:
                    single_assignment.chemist_id = reassign_id
                    single_assignment.status = AssignmentStatus.ASSIGNED
                    single_assignment.return_stage = None
                    single_assignment.date_completed = None
                    reassign_msg = (f' Reassigned from {old_chemist_name} '
                                    f'to {new_chemist.full_name}.')
                    notify_sample_assigned(single_assignment)

        out_of_spec_msg = ' [OUT OF SPEC]' if out_of_spec_flag else ''
        test_list = ', '.join(reviewed_names)

        _add_history(
            sample,
            f'Senior Chemist Review – {action.title()}{out_of_spec_msg}',
            (f'{current_user.full_name} {action} report for test(s): '
             f'{test_list}.{out_of_spec_msg}{reassign_msg} '
             f'Comments: {comments or "N/A"}'),
            action_type='Senior Chemist Review',
            object_affected='Report',
            change_description=(
                f'Test(s): {test_list} — {action} '
                f'by {current_user.full_name}'
                f'{out_of_spec_msg}{reassign_msg}'),
        )

        _update_sample_status(sample)
        db.session.commit()

        for a in sibling_assignments:
            notify_report_reviewed(a, action)
        db.session.commit()

        test_count = len(reviewed_names)
        if test_count > 1:
            flash(f'{test_count} reports have been {action}.{reassign_msg}', 'success')
        else:
            flash(f'Report has been {action}.{reassign_msg}', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/review_report.html', form=form, assignment=assignment,
        chemists=chemists,
        sibling_assignments=sibling_assignments,
        grouped_mode=grouped_mode,
    )


# ---------------------------------------------------------------------------
# Submit to Deputy Government Chemist (Senior Chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/submit-to-deputy', methods=['GET', 'POST'])
@login_required
def submit_to_deputy(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not current_user.is_branch_head() and not current_user.has_role(Role.ADMIN):
        flash('Only Senior Chemists can submit to Deputy.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status not in (SampleStatus.ACCEPTED, SampleStatus.DEPUTY_RETURNED):
        flash('Sample must have all reports accepted before submitting to Deputy.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    is_resubmission = sample.status == SampleStatus.DEPUTY_RETURNED
    is_pharma = sample.sample_type == Branch.PHARMACEUTICAL
    form = SubmitToDeputyForm()

    # Pre-fill the summary report on GET when resubmitting after a deputy return
    if request.method == 'GET' and is_resubmission and sample.summary_report:
        form.summary_report.data = sample.summary_report

    if form.validate_on_submit():
        # For pharmaceutical, require summary report
        if is_pharma and not form.summary_report.data:
            flash('Summary report is required for pharmaceutical samples.', 'danger')
            return render_template(
                'samples/submit_to_deputy.html', form=form, sample=sample,
                is_pharma=is_pharma, is_resubmission=is_resubmission,
            )

        if form.summary_report.data:
            sample.summary_report = form.summary_report.data
            sample.summary_report_by = current_user.id
            sample.summary_report_at = jamaica_now()

        if form.summary_report_file.data:
            stored, original = _save_file(form.summary_report_file.data)
            sample.summary_report_file = stored
            sample.summary_report_file_original_name = original

        sample.status = SampleStatus.DEPUTY_REVIEW

        if is_resubmission:
            action_label = 'Resubmitted to Deputy'
            detail_parts = [
                f'Resubmitted to Deputy Government Chemist by {current_user.full_name} after corrections.'
            ]
            change_desc = (
                f'Resubmitted to Deputy Government Chemist '
                f'by {current_user.full_name} after corrections.'
            )
            flash_msg = 'Resubmitted to Deputy Government Chemist.'
        else:
            action_label = 'Submitted to Deputy'
            detail_parts = [f'Submitted to Deputy Government Chemist by {current_user.full_name}.']
            change_desc = (
                f'Submitted to Deputy Government Chemist '
                f'by {current_user.full_name}'
            )
            flash_msg = 'Reports submitted to Deputy Government Chemist.'

        if is_pharma:
            detail_parts.append('Summary report included (Pharmaceutical sample).')

        _add_history(sample, action_label, ' '.join(detail_parts),
                     action_type='Deputy Submission',
                     object_affected='Sample',
                     change_description=change_desc)
        db.session.commit()

        notify_submitted_to_deputy(sample)
        db.session.commit()

        flash(flash_msg, 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/submit_to_deputy.html', form=form, sample=sample,
        is_pharma=is_pharma, is_resubmission=is_resubmission,
    )


# ---------------------------------------------------------------------------
# Deputy Government Chemist review
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/deputy-review', methods=['GET', 'POST'])
@login_required
def deputy_review(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not (
        current_user.has_any_role(Role.DEPUTY, Role.HOD, Role.ADMIN)
        or current_user.has_permission(Permission.DEPUTY_REVIEW)
    ):
        flash('Only the Deputy Government Chemist can perform this review.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status != SampleStatus.DEPUTY_REVIEW:
        flash('Sample is not awaiting Deputy review.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = DeputyReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        now = jamaica_now()

        # Log the review in ReviewHistory
        prev_count = ReviewHistory.query.filter_by(
            sample_id=sample.id, review_type='deputy'
        ).count()
        db.session.add(ReviewHistory(
            sample_id=sample.id,
            assignment_id=None,
            review_type='deputy',
            review_number=prev_count + 1,
            action=action,
            reviewer_id=current_user.id,
            reviewed_at=now,
            comments=form.review_comments.data,
        ))

        sample.deputy_review_comments = form.review_comments.data
        sample.deputy_reviewed_by = current_user.id
        sample.deputy_reviewed_at = now

        if action == 'approved':
            sample.status = SampleStatus.CERTIFICATE_PREPARATION
            _add_history(
                sample, 'Deputy Review Approved',
                (f'{current_user.full_name} approved the submission. '
                 f'Certificate of Analysis to be prepared.'),
                action_type='Deputy Review',
                object_affected='Sample',
                change_description=(
                    f'Approved by Deputy Government Chemist '
                    f'{current_user.full_name}. '
                    f'Proceeding to Certificate Preparation.'),
            )
        else:  # returned
            sample.status = SampleStatus.DEPUTY_RETURNED
            _add_history(
                sample, 'Deputy Review Returned',
                (f'{current_user.full_name} returned submission to '
                 f'Senior Chemist. Comments: {form.review_comments.data or "N/A"}'),
                action_type='Deputy Review',
                object_affected='Sample',
                change_description=(
                    f'Returned by Deputy Government Chemist '
                    f'{current_user.full_name}. '
                    f'Comments: {form.review_comments.data or "N/A"}'),
            )

        db.session.commit()

        notify_deputy_review_completed(sample, action)
        db.session.commit()

        action_text = 'approved' if action == 'approved' else 'returned to Senior Chemist'
        flash(f'Submission has been {action_text}.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    assignments = sample.assignments.all()
    return render_template(
        'samples/deputy_review.html', form=form, sample=sample,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# Resubmit to Deputy (Senior Chemist, after deputy return)
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/resubmit-to-deputy', methods=['POST'])
@login_required
def resubmit_to_deputy(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not current_user.is_branch_head() and not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status != SampleStatus.DEPUTY_RETURNED:
        flash('Sample is not in a returned-by-Deputy state.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    sample.status = SampleStatus.DEPUTY_REVIEW
    _add_history(
        sample, 'Resubmitted to Deputy',
        (f'{current_user.full_name} resubmitted to Deputy Government Chemist '
         f'after corrections.'),
        action_type='Deputy Resubmission',
        object_affected='Sample',
        change_description=(
            f'Resubmitted by {current_user.full_name} after corrections'),
    )
    db.session.commit()

    notify_submitted_to_deputy(sample)
    db.session.commit()

    flash('Resubmitted to Deputy Government Chemist.', 'success')
    return redirect(url_for('samples.detail', sample_id=sample.id))


# ---------------------------------------------------------------------------
# Prepare Certificate of Analysis (Deputy Government Chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/prepare-certificate', methods=['GET', 'POST'])
@login_required
def prepare_certificate(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not current_user.has_any_role(Role.DEPUTY, Role.HOD, Role.ADMIN):
        flash('Only the Deputy Government Chemist can prepare certificates.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status not in (SampleStatus.CERTIFICATE_PREPARATION, SampleStatus.HOD_RETURNED):
        flash('Sample is not ready for certificate preparation.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = CertificateForm()
    if form.validate_on_submit():
        sample.certificate_text = form.certificate_text.data
        sample.certificate_prepared_by = current_user.id
        sample.certificate_prepared_at = jamaica_now()
        sample.coa_reference = form.coa_reference.data or sample.coa_reference

        if form.certificate_file.data:
            stored, original = _save_file(form.certificate_file.data)
            sample.certificate_file = stored
            sample.certificate_file_original_name = original

            # Create a document version entry for the COA
            from app.models import DocumentVersion
            existing_versions = DocumentVersion.query.filter_by(
                sample_id=sample.id, document_type='certificate'
            ).count()
            version_num = existing_versions + 1
            upload_label = 'original' if version_num == 1 else 'revised'
            db.session.add(DocumentVersion(
                sample_id=sample.id,
                document_type='certificate',
                version_number=version_num,
                file_path=stored,
                original_name=original,
                upload_label=upload_label,
                uploaded_by=current_user.id,
            ))

        sample.status = SampleStatus.HOD_REVIEW

        coa_ref = form.coa_reference.data
        _add_history(
            sample, 'Certificate Prepared',
            (f'Certificate of Analysis prepared by {current_user.full_name}. '
             f'Submitted to Government Chemist for review and signing.'
             f'{" COA Ref: " + coa_ref if coa_ref else ""}'),
            action_type='Certificate Preparation',
            object_affected='Certificate of Analysis',
            change_description=(
                f'COA prepared by {current_user.full_name}, '
                f'submitted to Government Chemist for signing'),
        )
        db.session.commit()

        notify_certificate_prepared(sample)
        db.session.commit()

        flash('Certificate of Analysis submitted for Government Chemist review.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    # Pre-fill if resubmitting after HOD return
    if request.method == 'GET' and sample.certificate_text:
        form.certificate_text.data = sample.certificate_text
    if request.method == 'GET' and sample.coa_reference:
        form.coa_reference.data = sample.coa_reference

    return render_template(
        'samples/prepare_certificate.html', form=form, sample=sample,
    )


# ---------------------------------------------------------------------------
# HOD (Government Chemist) review & sign certificate
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/hod-review', methods=['GET', 'POST'])
@login_required
def hod_review(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not (
        current_user.has_any_role(Role.HOD, Role.ADMIN)
        or current_user.has_permission(Permission.HOD_REVIEW)
    ):
        flash('Only the Government Chemist can review and sign certificates.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status != SampleStatus.HOD_REVIEW:
        flash('Sample is not awaiting Government Chemist review.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = HODReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        now = jamaica_now()

        # Log the review in ReviewHistory
        prev_count = ReviewHistory.query.filter_by(
            sample_id=sample.id, review_type='hod'
        ).count()
        db.session.add(ReviewHistory(
            sample_id=sample.id,
            assignment_id=None,
            review_type='hod',
            review_number=prev_count + 1,
            action=action,
            reviewer_id=current_user.id,
            reviewed_at=now,
            comments=form.review_comments.data,
        ))

        sample.hod_review_comments = form.review_comments.data
        sample.hod_reviewed_by = current_user.id
        sample.hod_reviewed_at = now

        if action == 'sign':
            sample.certified_at = now
            sample.certified_by = current_user.id
            sample.status = SampleStatus.CERTIFIED
            _add_history(
                sample, 'Certificate Signed',
                (f'Certificate of Analysis signed by '
                 f'Government Chemist {current_user.full_name}. '
                 f'Sample analysis process completed.'),
                action_type='Certificate Signed',
                object_affected='Certificate of Analysis',
                change_description=(
                    f'Signed by Government Chemist {current_user.full_name}. '
                    f'Process completed.'),
            )
        else:  # returned
            sample.status = SampleStatus.HOD_RETURNED
            _add_history(
                sample, 'Certificate Returned by HOD',
                (f'Government Chemist {current_user.full_name} returned '
                 f'certificate for correction. '
                 f'Comments: {form.review_comments.data or "N/A"}'),
                action_type='HOD Review',
                object_affected='Certificate of Analysis',
                change_description=(
                    f'Returned by Government Chemist {current_user.full_name}. '
                    f'Comments: {form.review_comments.data or "N/A"}'),
            )

        db.session.commit()

        notify_certificate_signed(sample, action)
        db.session.commit()

        if action == 'sign':
            flash('Certificate of Analysis signed. Process completed.', 'success')
        else:
            flash('Certificate returned to Deputy for correction.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/hod_review.html', form=form, sample=sample,
    )


# ---------------------------------------------------------------------------
# Back-date registration date request
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/request-backdate', methods=['GET', 'POST'])
@login_required
def request_backdate(sample_id):
    """Request to back-date any date field on a sample or assignment."""
    sample = db.get_or_404(Sample, sample_id)

    # Officers, Senior Chemists, Admin, HOD, Chemists can request
    if not current_user.has_any_role(
        Role.OFFICER, Role.SENIOR_CHEMIST, Role.ADMIN, Role.HOD, Role.DEPUTY, Role.CHEMIST
    ):
        flash('You do not have permission to request a back-date.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = BackDateRequestForm()

    # Populate assignment choices
    assignments = sample.assignments.all()
    form.assignment_id.choices = [(0, '-- Sample-level (no assignment) --')] + [
        (a.id, f'{a.test_name} – {a.chemist.full_name if a.chemist else "Unknown"}')
        for a in assignments
    ]

    if form.validate_on_submit():
        field_name = form.field_name.data
        assignment_id_val = form.assignment_id.data if form.assignment_id.data else None
        if assignment_id_val == 0:
            assignment_id_val = None

        # Determine the original date value
        date_fmt = '%Y-%m-%d'
        # Fields on sample
        sample_date_fields = {
            'date_registered': sample.date_registered,
            'date_received': sample.date_received,
            'expected_report_date': sample.expected_report_date,
            'certificate_prepared_at': sample.certificate_prepared_at,
            'certified_at': sample.certified_at,
        }
        # Fields on assignment
        assignment_date_fields = {
            'assigned_date', 'expected_completion', 'report_submitted_at', 'test_date',
        }

        original = ''
        if field_name in sample_date_fields:
            val = sample_date_fields[field_name]
            if val:
                original = val.strftime(date_fmt) if hasattr(val, 'strftime') else str(val)
        elif field_name in assignment_date_fields and assignment_id_val:
            asgn = db.session.get(SampleAssignment, assignment_id_val)
            if asgn:
                val = getattr(asgn, field_name, None)
                if val:
                    original = val.strftime(date_fmt) if hasattr(val, 'strftime') else str(val)
        elif field_name in assignment_date_fields and not assignment_id_val:
            flash('Please select an assignment for assignment-level date fields.', 'danger')
            return render_template(
                'samples/request_backdate.html', form=form, sample=sample,
                assignments=assignments,
            )

        proposed = form.proposed_date.data.strftime(date_fmt)

        # Check for existing pending request on this field
        pending_q = BackDateRequest.query.filter_by(
            sample_id=sample.id,
            field_name=field_name,
            status='pending',
        )
        if assignment_id_val:
            pending_q = pending_q.filter_by(assignment_id=assignment_id_val)
        pending = pending_q.first()
        if pending:
            flash(f'A back-date request for "{field_name.replace("_", " ").title()}" is already pending.', 'warning')
            return redirect(url_for('samples.detail', sample_id=sample.id))

        bdr = BackDateRequest(
            sample_id=sample.id,
            assignment_id=assignment_id_val,
            field_name=field_name,
            original_date=original,
            proposed_date=proposed,
            reason=form.reason.data,
            requested_by=current_user.id,
        )
        db.session.add(bdr)

        field_label = field_name.replace('_', ' ').title()
        _add_history(
            sample, 'Back-Date Requested',
            (f'{current_user.full_name} requested to change {field_label} '
             f'from {original or "N/A"} to {proposed}. Reason: {form.reason.data}'),
            action_type='Back-Date Request',
            object_affected='Sample' if not assignment_id_val else 'Assignment',
            change_description=(
                f'{field_name}: {original or "N/A"} → {proposed} '
                f'(requested by {current_user.full_name})'),
        )
        db.session.commit()

        notify_backdate_request_submitted(bdr)
        db.session.commit()

        flash('Back-date request submitted for approval.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/request_backdate.html', form=form, sample=sample,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# Request deletion of a sample (Senior Chemist, Deputy, Officer, GC Assistant)
# ---------------------------------------------------------------------------

# Roles that may submit a deletion request (HOD and Admin can delete directly)
_DELETE_REQUEST_ROLES = (
    Role.SENIOR_CHEMIST, Role.DEPUTY, Role.OFFICER, Role.GOVT_CHEMIST_ASSISTANT,
)


@samples_bp.route('/<int:sample_id>/request-delete', methods=['GET', 'POST'])
@login_required
def request_sample_delete(sample_id):
    """Let authorised staff submit a deletion request for a sample."""
    sample = db.get_or_404(Sample, sample_id)

    if not current_user.has_any_role(*_DELETE_REQUEST_ROLES):
        flash('You do not have permission to request sample deletion.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    # Block if a pending request already exists
    existing = DeleteRequest.query.filter_by(
        request_type='sample', sample_id=sample.id, status='pending'
    ).first()
    if existing:
        flash('A deletion request for this sample is already pending approval.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = DeleteRequestForm()
    if form.validate_on_submit():
        uploader = db.session.get(User, sample.uploaded_by)
        snapshot = json.dumps({
            'lab_number': sample.lab_number,
            'sample_name': sample.sample_name,
            'sample_type': sample.sample_type.value,
            'status': sample.status.value,
            'date_received': sample.date_received.isoformat() if sample.date_received else None,
            'date_registered': sample.date_registered.isoformat() if sample.date_registered else None,
            'uploaded_by': sample.uploaded_by,
            'uploaded_by_name': uploader.full_name if uploader else None,
            'assignment_count': sample.assignments.count(),
        })
        dr = DeleteRequest(
            request_type='sample',
            sample_id=sample.id,
            reason=form.reason.data,
            entity_snapshot=snapshot,
            entity_label=sample.lab_number,
            requested_by=current_user.id,
        )
        db.session.add(dr)

        _add_history(
            sample, 'Deletion Requested',
            (f'{current_user.full_name} requested deletion of this sample. '
             f'Reason: {form.reason.data}'),
            action_type='Delete Request',
            object_affected='Sample',
            change_description=f'Deletion requested by {current_user.full_name}',
        )
        db.session.commit()

        notify_delete_request_submitted(dr)
        db.session.commit()

        flash('Deletion request submitted for HOD approval.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/request_delete.html', form=form, sample=sample, assignment=None,
    )


@samples_bp.route('/assignment/<int:assignment_id>/request-delete', methods=['GET', 'POST'])
@login_required
def request_assignment_delete(assignment_id):
    """Let authorised staff submit a deletion request for a test assignment."""
    assignment = db.get_or_404(SampleAssignment, assignment_id)
    sample = assignment.sample

    if not current_user.has_any_role(*_DELETE_REQUEST_ROLES):
        flash('You do not have permission to request assignment deletion.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Block if a pending request already exists
    existing = DeleteRequest.query.filter_by(
        request_type='assignment', assignment_id=assignment.id, status='pending'
    ).first()
    if existing:
        flash('A deletion request for this assignment is already pending approval.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    form = DeleteRequestForm()
    if form.validate_on_submit():
        chemist = db.session.get(User, assignment.chemist_id)
        snapshot = json.dumps({
            'assignment_id': assignment.id,
            'sample_lab_number': sample.lab_number,
            'sample_name': sample.sample_name,
            'test_name': assignment.test_name,
            'test_reference': assignment.test_reference,
            'chemist_id': assignment.chemist_id,
            'chemist_name': chemist.full_name if chemist else None,
            'status': assignment.status.value,
            'assigned_date': assignment.assigned_date.isoformat() if assignment.assigned_date else None,
        })
        label = f'{sample.lab_number} – {assignment.test_name}'
        dr = DeleteRequest(
            request_type='assignment',
            sample_id=sample.id,
            assignment_id=assignment.id,
            reason=form.reason.data,
            entity_snapshot=snapshot,
            entity_label=label,
            requested_by=current_user.id,
        )
        db.session.add(dr)

        _add_history(
            sample, 'Assignment Deletion Requested',
            (f'{current_user.full_name} requested deletion of assignment '
             f'"{assignment.test_name}". Reason: {form.reason.data}'),
            action_type='Delete Request',
            object_affected='Sample Assignment',
            change_description=(
                f'Deletion of assignment "{assignment.test_name}" requested '
                f'by {current_user.full_name}'),
        )
        db.session.commit()

        notify_delete_request_submitted(dr)
        db.session.commit()

        flash('Deletion request submitted for HOD approval.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/request_delete.html', form=form, sample=sample, assignment=assignment,
    )


def _update_sample_status(sample):
    """Derive the overall sample status from assignment statuses."""
    assignments = sample.assignments.all()
    if not assignments:
        return

    statuses = {a.status for a in assignments}

    if all(s == AssignmentStatus.COMPLETED for s in statuses):
        sample.status = SampleStatus.COMPLETED
    elif all(s in (AssignmentStatus.ACCEPTED, AssignmentStatus.COMPLETED) for s in statuses):
        sample.status = SampleStatus.ACCEPTED
    elif any(s == AssignmentStatus.REJECTED for s in statuses):
        sample.status = SampleStatus.REJECTED
    elif any(s == AssignmentStatus.RETURNED for s in statuses):
        sample.status = SampleStatus.RETURNED
    elif any(s == AssignmentStatus.UNDER_TECHNICAL_REVIEW for s in statuses):
        sample.status = SampleStatus.UNDER_TECHNICAL_REVIEW
    elif any(s == AssignmentStatus.REPORT_SUBMITTED for s in statuses):
        sample.status = SampleStatus.REPORT_SUBMITTED
    elif any(s == AssignmentStatus.IN_PROGRESS for s in statuses):
        sample.status = SampleStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# File downloads and inline preview
# ---------------------------------------------------------------------------

@samples_bp.route('/download/<path:filename>')
@login_required
def download_file(filename):
    # Prevent path traversal
    safe_name = secure_filename(filename)
    if safe_name != filename:
        abort(404)
    return send_from_directory(
        current_app.config['UPLOAD_FOLDER'], safe_name, as_attachment=True
    )


@samples_bp.route('/view/<path:filename>')
@login_required
def view_file(filename):
    """Serve a file inline (for PDF preview in browser)."""
    safe_name = secure_filename(filename)
    if safe_name != filename:
        abort(404)
    return send_from_directory(
        current_app.config['UPLOAD_FOLDER'], safe_name, as_attachment=False
    )


# ---------------------------------------------------------------------------
# Bulk Delete Samples  (Admin only – always audited)
# ---------------------------------------------------------------------------

@samples_bp.route('/bulk-delete', methods=['POST'])
@login_required
def bulk_delete():
    """Delete one or more samples and all related data.

    Every deletion is recorded in the permanent AuditLog table so that
    the action can never be lost – even after the sample row is gone.
    """
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.sample_list'))

    sample_ids = request.form.getlist('sample_ids', type=int)
    if not sample_ids:
        flash('No samples selected.', 'warning')
        return redirect(url_for('samples.sample_list'))

    samples = Sample.query.filter(Sample.id.in_(sample_ids)).all()
    if not samples:
        flash('No matching samples found.', 'warning')
        return redirect(url_for('samples.sample_list'))

    deleted_labels = []
    now = jamaica_now()

    for sample in samples:
        # Build a snapshot of sample data for the audit log
        uploader = db.session.get(User, sample.uploaded_by)
        snapshot = json.dumps({
            'lab_number': sample.lab_number,
            'sample_name': sample.sample_name,
            'sample_type': sample.sample_type.value,
            'status': sample.status.value,
            'description': sample.description,
            'quantity': sample.quantity,
            'date_received': sample.date_received.isoformat() if sample.date_received else None,
            'date_registered': sample.date_registered.isoformat() if sample.date_registered else None,
            'uploaded_by': sample.uploaded_by,
            'uploaded_by_name': uploader.full_name if uploader else None,
            'assignment_count': sample.assignments.count(),
        })

        # Write permanent audit record
        db.session.add(AuditLog(
            action='SAMPLE_DELETED',
            entity_type='Sample',
            entity_id=sample.id,
            entity_label=sample.lab_number,
            details=snapshot,
            performed_by=current_user.id,
            performed_at=now,
        ))

        # Remove uploaded files from disk
        _delete_sample_files(sample)

        # Explicitly delete ReviewHistory records (sample_id is NOT NULL so
        # SQLAlchemy cannot null it out; the cascade on the relationship
        # handles this, but we also do it explicitly for safety).
        ReviewHistory.query.filter_by(sample_id=sample.id).delete(
            synchronize_session=False
        )

        # Delete sample (cascades to assignments, history, supporting docs,
        # document versions, back-date requests via relationship cascades)
        db.session.delete(sample)

        deleted_labels.append(sample.lab_number)

    # Bulk-remove related notifications for all deleted samples
    notif_patterns = [f'%/samples/{sid}%' for sid in sample_ids]
    for pattern in notif_patterns:
        Notification.query.filter(
            Notification.link.like(pattern)
        ).delete(synchronize_session=False)

    db.session.commit()

    count = len(deleted_labels)
    flash(
        f'{count} sample{"s" if count != 1 else ""} deleted: '
        f'{", ".join(deleted_labels)}.',
        'success',
    )
    return redirect(url_for('samples.sample_list'))


def _delete_sample_files(sample):
    """Remove all uploaded files associated with a sample from disk."""
    upload_folder = current_app.config.get('UPLOAD_FOLDER')
    if not upload_folder:
        return

    paths_to_remove = set()

    # Scanned file on the sample itself
    if sample.scanned_file:
        paths_to_remove.add(sample.scanned_file)
    if sample.summary_report_file:
        paths_to_remove.add(sample.summary_report_file)
    if sample.certificate_file:
        paths_to_remove.add(sample.certificate_file)

    # Assignment report files
    for assignment in sample.assignments.all():
        if assignment.report_file:
            paths_to_remove.add(assignment.report_file)

    # Supporting documents
    for doc in sample.supporting_documents.all():
        if doc.file_path:
            paths_to_remove.add(doc.file_path)

    # Document versions
    for dv in sample.document_versions.all():
        if dv.file_path:
            paths_to_remove.add(dv.file_path)

    for filename in paths_to_remove:
        full_path = os.path.join(upload_folder, filename)
        if os.path.isfile(full_path):
            try:
                os.remove(full_path)
            except OSError:
                current_app.logger.warning(
                    'Could not remove file %s during sample deletion', full_path
                )
        # Remove cached PDF conversion if this was a Word document
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext in ('doc', 'docx'):
            base_filename = os.path.splitext(filename)[0]
            cached_pdf = os.path.join(upload_folder, 'pdf_cache', base_filename + '.pdf')
            if os.path.isfile(cached_pdf):
                try:
                    os.remove(cached_pdf)
                except OSError:
                    current_app.logger.warning(
                        'Could not remove cached PDF %s during sample deletion', cached_pdf
                    )


# ---------------------------------------------------------------------------
# COA Decertify / Re-Issue  (Feature 5)
# ---------------------------------------------------------------------------

def _can_manage_coa(user):
    """Return True if user is authorised to decertify/re-issue COAs."""
    return (
        user.has_any_role(Role.HOD, Role.ADMIN)
        or user.has_permission(Permission.COA_DECERTIFY_REISSUE)
    )


@samples_bp.route('/<int:sample_id>/coa/decertify', methods=['GET', 'POST'])
@login_required
def coa_decertify(sample_id):
    """Decertify a signed COA (HOD / Government Chemist only)."""
    sample = db.get_or_404(Sample, sample_id)
    if not _can_manage_coa(current_user):
        flash('Access denied. Only HOD or Government Chemist can decertify COAs.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    if sample.status != SampleStatus.CERTIFIED:
        flash('Only certified samples can be decertified.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    form = COADecertifyForm()
    if form.validate_on_submit():
        now = jamaica_now()
        sample.decertified_at = now
        sample.decertified_by = current_user.id
        sample.decertify_reason = form.reason.data
        sample.status = SampleStatus.CERTIFICATE_PREPARATION  # back to prep stage

        _add_history(
            sample, 'COA Decertified',
            f'Decertified by {current_user.full_name}: {form.reason.data}',
            action_type='Decertify',
            object_affected='COA',
            change_description=form.reason.data,
        )
        db.session.add(AuditLog(
            action='COA_DECERTIFY',
            entity_type='Sample',
            entity_id=sample.id,
            entity_label=sample.lab_number,
            details=f'Reason: {form.reason.data}',
            performed_by=current_user.id,
        ))
        db.session.commit()
        flash('COA decertified. Sample returned to Certificate Preparation stage.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    return render_template('samples/coa_decertify.html', form=form, sample=sample)


@samples_bp.route('/<int:sample_id>/coa/reissue', methods=['GET', 'POST'])
@login_required
def coa_reissue(sample_id):
    """Re-issue a Certificate of Analysis (HOD / Government Chemist only)."""
    sample = db.get_or_404(Sample, sample_id)
    if not _can_manage_coa(current_user):
        flash('Access denied. Only HOD or Government Chemist can re-issue COAs.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    # Can only re-issue if currently in CERTIFICATE_PREPARATION after a decertify
    if sample.status not in (SampleStatus.CERTIFICATE_PREPARATION, SampleStatus.CERTIFIED):
        flash('Sample is not in a state that permits COA re-issue.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    form = COAReissueForm()
    if form.validate_on_submit():
        now = jamaica_now()
        # Archive previous cert details in history before overwriting
        prev_ref = sample.coa_reference
        prev_ver = sample.coa_version or 1

        if form.certificate_file.data:
            stored, original = _save_file(form.certificate_file.data)
            sample.certificate_file = stored
            sample.certificate_file_original_name = original
            existing_versions = DocumentVersion.query.filter_by(
                sample_id=sample.id, document_type='certificate'
            ).count()
            db.session.add(DocumentVersion(
                sample_id=sample.id,
                document_type='certificate',
                version_number=existing_versions + 1,
                file_path=stored,
                original_name=original,
                upload_label='reissued',
                uploaded_by=current_user.id,
            ))

        sample.certificate_text = form.certificate_text.data
        if form.coa_reference.data:
            sample.coa_reference = form.coa_reference.data
        sample.coa_version = prev_ver + 1
        sample.reissued_at = now
        sample.reissued_by = current_user.id
        sample.status = SampleStatus.CERTIFIED
        sample.certified_at = now
        sample.certified_by = current_user.id

        _add_history(
            sample, 'COA Re-Issued',
            (f'Re-issued by {current_user.full_name}. '
             f'Previous version: {prev_ver}, Previous ref: {prev_ref}. '
             f'New ref: {sample.coa_reference}'),
            action_type='Reissue',
            object_affected='COA',
        )
        db.session.add(AuditLog(
            action='COA_REISSUE',
            entity_type='Sample',
            entity_id=sample.id,
            entity_label=sample.lab_number,
            details=f'Version: {sample.coa_version}, Ref: {sample.coa_reference}',
            performed_by=current_user.id,
        ))
        db.session.commit()
        flash(f'COA re-issued successfully (Version {sample.coa_version}).', 'success')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    # Pre-populate with current certificate text
    if request.method == 'GET':
        form.certificate_text.data = sample.certificate_text
        form.coa_reference.data = sample.coa_reference

    return render_template('samples/coa_reissue.html', form=form, sample=sample)


# ---------------------------------------------------------------------------
# Invoice routes  (Feature 9)
# ---------------------------------------------------------------------------

def _next_invoice_number():
    """Generate a sequential invoice number INV-YYYY-NNNN."""
    from datetime import date
    year = date.today().year
    prefix = f'INV-{year}-'
    last = Invoice.query.filter(
        Invoice.invoice_number.like(f'{prefix}%')
    ).order_by(Invoice.id.desc()).first()
    if last:
        try:
            seq = int(last.invoice_number[len(prefix):]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f'{prefix}{seq:04d}'


@samples_bp.route('/<int:sample_id>/invoice/new', methods=['GET', 'POST'])
@login_required
def invoice_create(sample_id):
    """Create a new invoice for a sample."""
    sample = db.get_or_404(Sample, sample_id)
    if not (
        current_user.has_any_role(Role.ADMIN, Role.HOD, Role.OFFICER)
        or current_user.has_permission(Permission.INVOICE_GENERATE)
    ):
        flash('Access denied. You are not permitted to generate invoices.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample_id))

    form = InvoiceCreateForm()
    if form.validate_on_submit():
        invoice = Invoice(
            sample_id=sample.id,
            invoice_number=_next_invoice_number(),
            created_by=current_user.id,
            notes=form.notes.data or None,
        )
        db.session.add(invoice)
        db.session.flush()

        # Parse line items from form data
        names = request.form.getlist('item_test_name')
        types = request.form.getlist('item_test_type')
        costs = request.form.getlist('item_unit_cost')
        qtys = request.form.getlist('item_quantity')

        for i, name in enumerate(names):
            if not name.strip():
                continue
            try:
                unit_cost = float(costs[i]) if i < len(costs) else 0
            except (ValueError, IndexError):
                unit_cost = 0
            try:
                qty = int(qtys[i]) if i < len(qtys) else 1
                qty = max(1, qty)
            except (ValueError, IndexError):
                qty = 1
            test_type = types[i] if i < len(types) else ''

            db.session.add(InvoiceItem(
                invoice_id=invoice.id,
                test_name=name.strip(),
                test_type=test_type or None,
                unit_cost=unit_cost,
                quantity=qty,
            ))

        _add_history(
            sample, 'Invoice Created',
            f'Invoice {invoice.invoice_number} created by {current_user.full_name}',
            action_type='Invoice',
            object_affected='Invoice',
        )
        db.session.commit()
        flash(f'Invoice {invoice.invoice_number} created successfully.', 'success')
        return redirect(url_for('samples.invoice_detail',
                                sample_id=sample_id, invoice_id=invoice.id))

    # Build pricing map for JS auto-populate
    pricing_json = json.dumps(PHARMA_TEST_PRICES)
    return render_template(
        'samples/invoice_create.html',
        form=form, sample=sample,
        pricing_json=pricing_json,
    )


@samples_bp.route('/<int:sample_id>/invoice/<int:invoice_id>')
@login_required
def invoice_detail(sample_id, invoice_id):
    """View an invoice."""
    sample = db.get_or_404(Sample, sample_id)
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.sample_id != sample.id:
        abort(404)
    items = invoice.items.all()
    grand_total = sum(item.line_total for item in items)
    return render_template(
        'samples/invoice_detail.html',
        sample=sample, invoice=invoice, items=items,
        grand_total=grand_total,
    )

