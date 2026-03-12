import os
import uuid
from datetime import datetime, timezone

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_from_directory, abort,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.samples import samples_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, User,
    Role, Branch, SampleStatus, AssignmentStatus,
    user_roles, user_branches,
)
from app.forms import (
    SampleRegisterForm, SampleEditForm, SampleAssignForm,
    ReportSubmitForm, PreliminaryReviewForm, ReportReviewForm,
    SubmitToDeputyForm, DeputyReviewForm, CertificateForm, HODReviewForm,
)
from app.notifications import (
    notify_sample_uploaded, notify_sample_assigned,
    notify_report_submitted, notify_preliminary_review_completed,
    notify_report_reviewed, notify_submitted_to_deputy,
    notify_deputy_review_completed, notify_certificate_prepared,
    notify_certificate_signed,
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
        # Officers see samples they uploaded
        query = query.filter(Sample.uploaded_by == current_user.id)
    elif current_user.has_role(Role.SENIOR_CHEMIST) and current_user.branches and not current_user.has_any_role(Role.HOD, Role.ADMIN):
        # Senior Chemists see samples in their branch(es)
        query = query.filter(Sample.sample_type.in_(current_user.branches))

    samples = query.order_by(Sample.date_registered.desc()).all()
    return render_template(
        'samples/sample_list.html',
        samples=samples,
        SampleStatus=SampleStatus,
        Branch=Branch,
        status_filter=status_filter,
        type_filter=type_filter,
        search=search,
    )


# ---------------------------------------------------------------------------
# Register a new sample
# ---------------------------------------------------------------------------

@samples_bp.route('/register', methods=['GET', 'POST'])
@login_required
def register():
    if not current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD):
        flash('Only officers can register samples.', 'danger')
        return redirect(url_for('samples.sample_list'))

    form = SampleRegisterForm()
    if form.validate_on_submit():
        sample = Sample(
            lab_number=form.lab_number.data,
            sample_name=form.sample_name.data,
            sample_type=Branch[form.sample_type.data],
            description=form.description.data,
            quantity=form.quantity.data,
            parish=form.parish.data,
            patient_name=form.patient_name.data,
            source=form.source.data,
            date_received=form.date_received.data,
            expected_report_date=form.expected_report_date.data,
            uploaded_by=current_user.id,
        )

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

    return render_template('samples/register.html', form=form)


# ---------------------------------------------------------------------------
# Sample detail
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>')
@login_required
def detail(sample_id):
    sample = Sample.query.get_or_404(sample_id)
    assignments = sample.assignments.all()
    history = sample.history.all()
    return render_template(
        'samples/detail.html',
        sample=sample,
        assignments=assignments,
        history=history,
    )


# ---------------------------------------------------------------------------
# Edit sample
# ---------------------------------------------------------------------------

@samples_bp.route('/<int:sample_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(sample_id):
    sample = Sample.query.get_or_404(sample_id)
    if not current_user.has_any_role(Role.OFFICER, Role.ADMIN, Role.HOD) and \
       current_user.id != sample.uploaded_by:
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleEditForm(obj=sample)
    if form.validate_on_submit():
        sample.sample_name = form.sample_name.data
        sample.description = form.description.data
        sample.quantity = form.quantity.data
        sample.parish = form.parish.data
        sample.patient_name = form.patient_name.data
        sample.source = form.source.data
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
    sample = Sample.query.get_or_404(sample_id)
    if not current_user.is_branch_head() and not current_user.has_role(Role.ADMIN):
        flash('Only Senior Chemists / Branch Heads can assign samples.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleAssignForm()

    # Populate chemist choices – chemists in the matching branch
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
        for chemist_id in form.chemist_ids.data:
            assignment = SampleAssignment(
                sample_id=sample.id,
                chemist_id=chemist_id,
                assigned_by=current_user.id,
                test_name=form.test_name.data,
                test_reference=form.test_reference.data,
                expected_completion=form.expected_completion.data,
            )
            db.session.add(assignment)
            db.session.flush()
            notify_sample_assigned(assignment)

        sample.status = SampleStatus.ASSIGNED
        _add_history(
            sample, 'Sample Assigned',
            f'Assigned to {len(form.chemist_ids.data)} chemist(s) '
            f'by {current_user.full_name}',
        )
        db.session.commit()
        flash('Sample assigned successfully.', 'success')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    return render_template('samples/assign.html', form=form, sample=sample)


# ---------------------------------------------------------------------------
# Assignment detail (for chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>')
@login_required
def assignment_detail(assignment_id):
    assignment = SampleAssignment.query.get_or_404(assignment_id)
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
# Submit report (chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/report', methods=['GET', 'POST'])
@login_required
def submit_report(assignment_id):
    assignment = SampleAssignment.query.get_or_404(assignment_id)
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
        assignment.report_submitted_at = datetime.now(timezone.utc)

        if form.report_file.data:
            stored, original = _save_file(form.report_file.data)
            assignment.report_file = stored
            assignment.report_file_original_name = original

        # Route to correct review stage based on where it was returned from
        if assignment.return_stage == 'technical':
            # Returned by Senior Chemist – skip preliminary, go back to
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
    assignment = SampleAssignment.query.get_or_404(assignment_id)

    # Only Officer who uploaded the sample, HOD, or Admin can do preliminary review
    sample = assignment.sample
    if current_user.has_role(Role.OFFICER) and current_user.id != sample.uploaded_by:
        flash('Access denied.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))
    if not current_user.has_any_role(Role.OFFICER, Role.HOD, Role.ADMIN):
        flash('Only Officers can perform preliminary reviews.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status != AssignmentStatus.REPORT_SUBMITTED:
        flash('This report is not awaiting preliminary review.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    form = PreliminaryReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        assignment.preliminary_review_comments = form.review_comments.data
        assignment.preliminary_reviewed_by = current_user.id
        assignment.preliminary_reviewed_at = datetime.now(timezone.utc)

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
# Technical review (Senior Chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/review', methods=['GET', 'POST'])
@login_required
def review_report(assignment_id):
    assignment = SampleAssignment.query.get_or_404(assignment_id)

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
        assignment.reviewed_at = datetime.now(timezone.utc)

        if action == 'accepted':
            assignment.status = AssignmentStatus.ACCEPTED
            assignment.date_completed = datetime.now(timezone.utc)
        elif action == 'returned':
            assignment.status = AssignmentStatus.RETURNED
            assignment.return_stage = 'technical'
            assignment.date_completed = None
        elif action == 'rejected':
            assignment.status = AssignmentStatus.REJECTED
            assignment.date_completed = datetime.now(timezone.utc)

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
    sample = Sample.query.get_or_404(sample_id)

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
            sample.summary_report_at = datetime.now(timezone.utc)

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
    sample = Sample.query.get_or_404(sample_id)

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
        sample.deputy_reviewed_at = datetime.now(timezone.utc)

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
    sample = Sample.query.get_or_404(sample_id)

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
    sample = Sample.query.get_or_404(sample_id)

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
        sample.certificate_prepared_at = datetime.now(timezone.utc)

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
    sample = Sample.query.get_or_404(sample_id)

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
        sample.hod_reviewed_at = datetime.now(timezone.utc)

        if action == 'sign':
            sample.certified_at = datetime.now(timezone.utc)
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
# File downloads
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
