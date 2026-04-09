import json
import os
import uuid
from datetime import datetime, timezone, date

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_from_directory, abort,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.samples import samples_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, User, SupportingDocument,
    Role, Branch, SampleStatus, AssignmentStatus,
    user_roles, user_branches, jamaica_now,
)
from app.forms import (
    SampleRegisterForm, SampleEditForm, SampleAssignForm,
    ReportSubmitForm, PreliminaryReviewForm, ReportReviewForm,
    SubmitToDeputyForm, DeputyReviewForm, CertificateForm, HODReviewForm,
    get_sample_register_form, SupportingDocumentForm,
    BRANCH_TEST_NAMES, BRANCH_TEST_REFERENCES,
)
from app.notifications import (
    notify_sample_uploaded, notify_sample_assigned,
    notify_report_submitted, notify_preliminary_review_completed,
    notify_report_reviewed, notify_submitted_to_deputy,
    notify_deputy_review_completed, notify_certificate_prepared,
    notify_certificate_signed, notify_assignment_removed,
)


def _save_file(file_storage):
    """Save an uploaded file and return (stored_name, original_name)."""
    original = secure_filename(file_storage.filename)
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else ''
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


def _add_history(sample, action, details=None):
    entry = SampleHistory(
        sample_id=sample.id,
        action=action,
        details=details,
        performed_by=current_user.id,
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
    status_filter = request.args.get('status')
    type_filter = request.args.get('type')
    search = request.args.get('q', '').strip()

    if status_filter:
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
    if current_user.has_role(Role.CHEMIST) and not current_user.has_any_role(Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        # Chemists see only samples assigned to them
        assigned_ids = db.select(SampleAssignment.sample_id).where(
            SampleAssignment.chemist_id == current_user.id
        ).scalar_subquery()
        query = query.filter(Sample.id.in_(assigned_ids))
    elif current_user.has_role(Role.OFFICER) and not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        # Officers see samples they uploaded AND all samples with reports submitted
        submitted_statuses = [
            SampleStatus.REPORT_SUBMITTED,
            SampleStatus.UNDER_PRELIMINARY_REVIEW,
            SampleStatus.UNDER_TECHNICAL_REVIEW,
            SampleStatus.ACCEPTED,
            SampleStatus.DEPUTY_REVIEW,
            SampleStatus.CERTIFICATE_PREPARATION,
            SampleStatus.HOD_REVIEW,
            SampleStatus.CERTIFIED,
            SampleStatus.COMPLETED,
        ]
        query = query.filter(
            db.or_(
                Sample.uploaded_by == current_user.id,
                Sample.status.in_(submitted_statuses),
            )
        )
    elif current_user.has_role(Role.SENIOR_CHEMIST) and current_user.branches and not current_user.has_any_role(Role.HOD, Role.ADMIN):
        # Senior Chemists see samples in their branch(es)
        query = query.filter(Sample.sample_type.in_(current_user.branches))

    samples = query.order_by(Sample.date_registered.desc()).all()
    result_count = len(samples)
    return render_template(
        'samples/sample_list.html',
        samples=samples,
        SampleStatus=SampleStatus,
        Branch=Branch,
        status_filter=status_filter,
        type_filter=type_filter,
        search=search,
        result_count=result_count,
        is_filtered=bool(status_filter or type_filter or search),
        today_date=date.today(),
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
    if not current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD):
        flash('Only officers can register samples.', 'danger')
        return redirect(url_for('samples.sample_list'))

    selected_type = request.form.get('sample_type') if request.method == 'POST' else request.args.get('type')
    FormClass = get_sample_register_form(selected_type) if selected_type else SampleRegisterForm
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

        if form.scanned_file.data:
            stored, original = _save_file(form.scanned_file.data)
            sample.scanned_file = stored
            sample.scanned_file_original_name = original

        db.session.add(sample)
        db.session.flush()
        _add_history(sample, 'Sample Registered',
                     f'Registered by {current_user.full_name}')
        db.session.commit()

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
    return render_template(
        'samples/detail.html',
        sample=sample,
        assignments=assignments,
        history=history,
        supporting_docs=supporting_docs,
        supporting_doc_form=supporting_doc_form,
        today_date=date.today(),
    )


# ---------------------------------------------------------------------------
# Edit sample
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(sample_id):
    sample = db.get_or_404(Sample, sample_id)
    if not current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD) and \
       current_user.id != sample.uploaded_by:
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleEditForm(obj=sample)
    if form.validate_on_submit():
        sample.sample_name = form.sample_name.data
        sample.sample_type = Branch[form.sample_type.data]
        sample.description = form.description.data
        sample.quantity = form.quantity.data
        sample.volume = form.volume.data
        sample.parish = form.parish.data
        sample.patient_name = form.patient_name.data
        sample.source = form.source.data
        sample.formulation_type = form.formulation_type.data
        sample.alcohol_type = form.alcohol_type.data if form.alcohol_type.data else None
        sample.claim_butt_number = form.claim_butt_number.data
        sample.batch_lot_number = form.batch_lot_number.data or None
        sample.milk_type = form.milk_type.data if form.milk_type.data else None
        sample.lot_number = form.lot_number.data or None
        sample.expiration_date = form.expiration_date.data
        sample.toxicology_sample_type_name = form.toxicology_sample_type_name.data or None
        sample.expected_report_date = form.expected_report_date.data

        if form.scanned_file.data:
            stored, original = _save_file(form.scanned_file.data)
            sample.scanned_file = stored
            sample.scanned_file_original_name = original

        _add_history(sample, 'Sample Updated', f'Updated by {current_user.full_name}')
        db.session.commit()
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
    if not current_user.is_branch_head() and not current_user.has_role(Role.ADMIN):
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
            test_ref = ', '.join(selected_refs) if selected_refs else form.test_reference.data
        else:
            test_ref = form.test_reference.data

        assignment_count = 0
        for chemist_id in form.chemist_ids.data:
            for test_name in selected_test_names:
                assignment = SampleAssignment(
                    sample_id=sample.id,
                    chemist_id=chemist_id,
                    assigned_by=current_user.id,
                    test_name=test_name,
                    test_reference=test_ref,
                    expected_completion=form.expected_completion.data,
                    comments=form.comments.data or None,
                    quantity_volume=form.quantity_volume.data or None,
                )
                db.session.add(assignment)
                db.session.flush()
                notify_sample_assigned(assignment)
                assignment_count += 1

        sample.status = SampleStatus.ASSIGNED
        _add_history(
            sample, 'Sample Assigned',
            f'Assigned {assignment_count} test(s) to '
            f'{len(form.chemist_ids.data)} chemist(s) '
            f'by {current_user.full_name}',
        )
        db.session.commit()
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
    return render_template('samples/assignment_detail.html', assignment=assignment)


def _can_view_assignment(assignment):
    if current_user.has_role(Role.ADMIN):
        return True
    if current_user.id == assignment.chemist_id:
        return True
    if current_user.id == assignment.sample.uploaded_by:
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
        f'{current_user.full_name} removed assignment of test '
        f'"{test_name}" from {chemist_name}.',
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
            old_chemist = assignment.chemist.full_name
            assignment.chemist_id = new_chemist_id
            new_chemist = db.session.get(User, new_chemist_id)
            changes.append(f'Assignee: {old_chemist} → {new_chemist.full_name}')
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
            _add_history(
                sample, 'Assignment Edited',
                f'{current_user.full_name} edited assignment: {"; ".join(changes)}',
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
                     f'{current_user.full_name} uploaded "{original}"')
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

    form = ReportSubmitForm()
    if form.validate_on_submit():
        assignment.report_text = form.report_text.data
        assignment.report_submitted_at = jamaica_now()
        assignment.all_samples_returned = form.all_samples_returned.data or None
        assignment.return_quantity = form.return_quantity.data or None

        if form.report_file.data:
            stored, original = _save_file(form.report_file.data)
            assignment.report_file = stored
            assignment.report_file_original_name = original

        # Route to correct review stage based on where it was returned from
        if assignment.return_stage == 'technical':
            # Returned by Senior Chemist - skip preliminary, go back to
            # technical review directly
            assignment.status = AssignmentStatus.UNDER_TECHNICAL_REVIEW
        else:
            # First submission or returned from preliminary review
            assignment.status = AssignmentStatus.REPORT_SUBMITTED

        assignment.return_stage = None

        _add_history(
            assignment.sample, 'Report Submitted',
            f'{current_user.full_name} submitted report for test '
            f'"{assignment.test_name}"',
        )

        # Update sample status
        _update_sample_status(assignment.sample)

        db.session.commit()

        notify_report_submitted(assignment)
        db.session.commit()

        flash('Report submitted successfully.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    # Pre-fill if resubmitting
    if request.method == 'GET' and assignment.report_text:
        form.report_text.data = assignment.report_text

    return render_template(
        'samples/submit_report.html', form=form, assignment=assignment
    )


# ---------------------------------------------------------------------------
# Preliminary review (Officer / Senior Chemist Technologist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/preliminary-review', methods=['GET', 'POST'])
@login_required
def preliminary_review(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)

    # Officers, Senior Chemists, Deputy, HOD, or Admin can do preliminary review
    sample = assignment.sample
    if not current_user.has_any_role(Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        flash('Only Officers, Senior Chemists, Deputy Government Chemist, or HOD can perform preliminary reviews.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status != AssignmentStatus.REPORT_SUBMITTED:
        flash('This report is not awaiting preliminary review.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    form = PreliminaryReviewForm()
    if form.validate_on_submit():
        action = form.action.data

        # Validation: If any checklist item is "No", only allow return
        if form.has_any_no() and action == 'approved':
            flash('Cannot approve: one or more checklist items are marked "No". '
                  'Please return for correction.', 'danger')
            return render_template(
                'samples/preliminary_review.html', form=form, assignment=assignment
            )

        assignment.preliminary_review_comments = form.review_comments.data
        assignment.preliminary_reviewed_by = current_user.id
        assignment.preliminary_reviewed_at = jamaica_now()

        # Save checklist answers
        checklist = {}
        for _, fields in form.CHECKLIST_CATEGORIES:
            for field_name in fields:
                checklist[field_name] = getattr(form, field_name).data
        assignment.preliminary_review_checklist = json.dumps(checklist)

        if action == 'approved':
            assignment.status = AssignmentStatus.UNDER_TECHNICAL_REVIEW
            _add_history(
                sample, 'Preliminary Review Approved',
                f'{current_user.full_name} approved preliminary review for '
                f'test "{assignment.test_name}". '
                f'Forwarded to Senior Chemist for technical review.',
            )
        else:  # returned
            assignment.status = AssignmentStatus.RETURNED
            assignment.return_stage = 'preliminary'
            assignment.date_completed = None
            _add_history(
                sample, 'Preliminary Review Returned',
                f'{current_user.full_name} returned report for test '
                f'"{assignment.test_name}" for correction. '
                f'Comments: {form.review_comments.data or "N/A"}',
            )

        _update_sample_status(sample)
        db.session.commit()

        notify_preliminary_review_completed(assignment, action)
        db.session.commit()

        action_text = 'approved and forwarded' if action == 'approved' else 'returned for correction'
        flash(f'Report has been {action_text}.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/preliminary_review.html', form=form, assignment=assignment
    )


# ---------------------------------------------------------------------------
# Senior Chemist Review (formerly Technical Review)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/review', methods=['GET', 'POST'])
@login_required
def review_report(assignment_id):
    assignment = db.get_or_404(SampleAssignment, assignment_id)

    if not current_user.is_branch_head() and not current_user.has_role(Role.ADMIN):
        flash('Only Senior Chemists / Branch Heads can review reports.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status != AssignmentStatus.UNDER_TECHNICAL_REVIEW:
        flash('This report is not awaiting technical review.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    form = ReportReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        assignment.review_comments = form.review_comments.data
        assignment.reviewed_by = current_user.id
        assignment.reviewed_at = jamaica_now()

        if action == 'accepted':
            assignment.status = AssignmentStatus.ACCEPTED
            assignment.date_completed = jamaica_now()
        elif action == 'returned':
            assignment.status = AssignmentStatus.RETURNED
            assignment.return_stage = 'technical'
            assignment.date_completed = None
        elif action == 'rejected':
            assignment.status = AssignmentStatus.REJECTED
            assignment.date_completed = jamaica_now()

        _add_history(
            assignment.sample,
            f'Technical Review – {action.title()}',
            f'{current_user.full_name} {action} report for test '
            f'"{assignment.test_name}". '
            f'Comments: {form.review_comments.data or "N/A"}',
        )

        _update_sample_status(assignment.sample)
        db.session.commit()

        notify_report_reviewed(assignment, action)
        db.session.commit()

        flash(f'Report has been {action}.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/review_report.html', form=form, assignment=assignment
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

    if sample.status != SampleStatus.ACCEPTED:
        flash('Sample must have all reports accepted before submitting to Deputy.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    is_pharma = sample.sample_type == Branch.PHARMACEUTICAL
    form = SubmitToDeputyForm()

    if form.validate_on_submit():
        # For pharmaceutical, require summary report
        if is_pharma and not form.summary_report.data:
            flash('Summary report is required for pharmaceutical samples.', 'danger')
            return render_template(
                'samples/submit_to_deputy.html', form=form, sample=sample,
                is_pharma=is_pharma,
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

        detail_parts = [f'Submitted to Deputy Government Chemist by {current_user.full_name}.']
        if is_pharma:
            detail_parts.append('Summary report included (Pharmaceutical sample).')

        _add_history(sample, 'Submitted to Deputy', ' '.join(detail_parts))
        db.session.commit()

        notify_submitted_to_deputy(sample)
        db.session.commit()

        flash('Reports submitted to Deputy Government Chemist.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template(
        'samples/submit_to_deputy.html', form=form, sample=sample,
        is_pharma=is_pharma,
    )


# ---------------------------------------------------------------------------
# Deputy Government Chemist review
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/deputy-review', methods=['GET', 'POST'])
@login_required
def deputy_review(sample_id):
    sample = db.get_or_404(Sample, sample_id)

    if not current_user.has_any_role(Role.DEPUTY, Role.HOD, Role.ADMIN):
        flash('Only the Deputy Government Chemist can perform this review.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status != SampleStatus.DEPUTY_REVIEW:
        flash('Sample is not awaiting Deputy review.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = DeputyReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        sample.deputy_review_comments = form.review_comments.data
        sample.deputy_reviewed_by = current_user.id
        sample.deputy_reviewed_at = jamaica_now()

        if action == 'approved':
            sample.status = SampleStatus.CERTIFICATE_PREPARATION
            _add_history(
                sample, 'Deputy Review Approved',
                f'{current_user.full_name} approved the submission. '
                f'Certificate of Analysis to be prepared.',
            )
        else:  # returned
            sample.status = SampleStatus.DEPUTY_RETURNED
            _add_history(
                sample, 'Deputy Review Returned',
                f'{current_user.full_name} returned submission to '
                f'Senior Chemist. Comments: {form.review_comments.data or "N/A"}',
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
        f'{current_user.full_name} resubmitted to Deputy Government Chemist '
        f'after corrections.',
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

        if form.certificate_file.data:
            stored, original = _save_file(form.certificate_file.data)
            sample.certificate_file = stored
            sample.certificate_file_original_name = original

        sample.status = SampleStatus.HOD_REVIEW

        _add_history(
            sample, 'Certificate Prepared',
            f'Certificate of Analysis prepared by {current_user.full_name}. '
            f'Submitted to Government Chemist for review and signing.',
        )
        db.session.commit()

        notify_certificate_prepared(sample)
        db.session.commit()

        flash('Certificate of Analysis submitted for Government Chemist review.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    # Pre-fill if resubmitting after HOD return
    if request.method == 'GET' and sample.certificate_text:
        form.certificate_text.data = sample.certificate_text

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

    if not current_user.has_any_role(Role.HOD, Role.ADMIN):
        flash('Only the Government Chemist can review and sign certificates.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    if sample.status != SampleStatus.HOD_REVIEW:
        flash('Sample is not awaiting Government Chemist review.', 'warning')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = HODReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        sample.hod_review_comments = form.review_comments.data
        sample.hod_reviewed_by = current_user.id
        sample.hod_reviewed_at = jamaica_now()

        if action == 'sign':
            sample.certified_at = jamaica_now()
            sample.certified_by = current_user.id
            sample.status = SampleStatus.CERTIFIED
            _add_history(
                sample, 'Certificate Signed',
                f'Certificate of Analysis signed by '
                f'Government Chemist {current_user.full_name}. '
                f'Sample analysis process completed.',
            )
        else:  # returned
            sample.status = SampleStatus.HOD_RETURNED
            _add_history(
                sample, 'Certificate Returned by HOD',
                f'Government Chemist {current_user.full_name} returned '
                f'certificate for correction. '
                f'Comments: {form.review_comments.data or "N/A"}',
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
