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
)
from app.forms import (
    SampleRegisterForm, SampleEditForm, SampleAssignForm,
    ReportSubmitForm, ReportReviewForm,
)
from app.notifications import (
    notify_sample_uploaded, notify_sample_assigned,
    notify_report_submitted, notify_report_reviewed,
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
    if current_user.role == Role.CHEMIST:
        # Chemists see only samples assigned to them
        assigned_ids = db.select(SampleAssignment.sample_id).where(
            SampleAssignment.chemist_id == current_user.id
        ).scalar_subquery()
        query = query.filter(Sample.id.in_(assigned_ids))
    elif current_user.role == Role.OFFICER:
        # Officers see samples they uploaded
        query = query.filter(Sample.uploaded_by == current_user.id)
    elif current_user.role == Role.SENIOR_CHEMIST and current_user.branch:
        # Senior Chemists see samples in their branch
        query = query.filter(Sample.sample_type == current_user.branch)

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
    if current_user.role not in (Role.OFFICER, Role.ADMIN, Role.HOD):
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
    if current_user.role not in (Role.OFFICER, Role.ADMIN, Role.HOD) and \
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
    if not current_user.is_branch_head() and current_user.role != Role.ADMIN:
        flash('Only Senior Chemists / Branch Heads can assign samples.', 'danger')
        return redirect(url_for('samples.detail', sample_id=sample.id))

    form = SampleAssignForm()

    # Populate chemist choices – chemists in the matching branch
    chemists = User.query.filter(
        User.role == Role.CHEMIST,
        User.is_active_user.is_(True),
    )
    if current_user.branch:
        chemists = chemists.filter(User.branch == current_user.branch)
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
    if current_user.role == Role.ADMIN:
        return True
    if current_user.id == assignment.chemist_id:
        return True
    if current_user.id == assignment.sample.uploaded_by:
        return True
    if current_user.is_branch_head():
        if current_user.branch is None or current_user.branch == assignment.sample.sample_type:
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
        assignment.status = AssignmentStatus.REPORT_SUBMITTED

        if form.report_file.data:
            stored, original = _save_file(form.report_file.data)
            assignment.report_file = stored
            assignment.report_file_original_name = original

        _add_history(
            assignment.sample, 'Report Submitted',
            f'{current_user.full_name} submitted report for test '
            f'"{assignment.test_name}"',
        )

        # Update sample status
        sample = assignment.sample
        all_submitted = all(
            a.status in (
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_REVIEW,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.COMPLETED,
            )
            for a in sample.assignments.all()
        )
        if all_submitted:
            sample.status = SampleStatus.REPORT_SUBMITTED

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
# Review report (branch head / senior chemist)
# ---------------------------------------------------------------------------

@samples_bp.route('/assignment/<int:assignment_id>/review', methods=['GET', 'POST'])
@login_required
def review_report(assignment_id):
    assignment = SampleAssignment.query.get_or_404(assignment_id)

    if not current_user.is_branch_head() and current_user.role != Role.ADMIN:
        flash('Only branch heads can review reports.', 'danger')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    if assignment.status not in (
        AssignmentStatus.REPORT_SUBMITTED,
        AssignmentStatus.UNDER_REVIEW,
    ):
        flash('No report to review in the current state.', 'warning')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    form = ReportReviewForm()
    if form.validate_on_submit():
        action = form.action.data
        assignment.review_comments = form.review_comments.data
        assignment.reviewed_by = current_user.id
        assignment.reviewed_at = datetime.now(timezone.utc)

        status_map = {
            'accepted': AssignmentStatus.ACCEPTED,
            'returned': AssignmentStatus.RETURNED,
            'rejected': AssignmentStatus.REJECTED,
            'completed': AssignmentStatus.COMPLETED,
        }
        assignment.status = status_map[action]

        if action == 'returned':
            assignment.date_completed = None

        if action in ('accepted', 'completed'):
            assignment.date_completed = datetime.now(timezone.utc)

        _add_history(
            assignment.sample,
            f'Report {action.title()}',
            f'{current_user.full_name} {action} report for test '
            f'"{assignment.test_name}". '
            f'Comments: {form.review_comments.data or "N/A"}',
        )

        # Update overall sample status
        _update_sample_status(assignment.sample)

        db.session.commit()

        notify_report_reviewed(assignment, action)
        db.session.commit()

        flash(f'Report has been {action}.', 'success')
        return redirect(url_for('samples.assignment_detail', assignment_id=assignment.id))

    return render_template(
        'samples/review_report.html', form=form, assignment=assignment
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
    elif any(s in (AssignmentStatus.UNDER_REVIEW, AssignmentStatus.REPORT_SUBMITTED) for s in statuses):
        sample.status = SampleStatus.UNDER_REVIEW
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
