"""Email and in-app notification helpers."""

from threading import Thread

from flask import current_app, render_template_string
from flask_mail import Message

from app import db, mail
from app.models import Notification, User, Branch, Role, user_roles, user_branches


def _send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            app.logger.error(f'Failed to send email: {e}')


def send_email(subject, recipients, body_text, body_html=None):
    """Send an email (non-blocking). Respects the email_enabled setting."""
    from app.models import Setting
    if not Setting.get_bool('email_enabled', default=True):
        current_app.logger.info(f'Email suppressed (disabled in settings): {subject}')
        return
    app = current_app._get_current_object()
    msg = Message(subject=subject, recipients=recipients, body=body_text)
    if body_html:
        msg.html = body_html
    thread = Thread(target=_send_async_email, args=(app, msg))
    thread.daemon = True
    thread.start()


def create_notification(user_id, title, message, link=None, send_mail=True):
    """Create an in-app notification and optionally send email."""
    notif = Notification(
        user_id=user_id,
        title=title,
        message=message,
        link=link,
    )
    db.session.add(notif)

    if send_mail:
        user = db.session.get(User, user_id)
        if user and user.email:
            send_email(
                subject=f'[DGC SMS] {title}',
                recipients=[user.email],
                body_text=message,
            )
            notif.email_sent = True

    return notif


def notify_branch_heads(branch, title, message, link=None, exclude_user_id=None):
    """Notify Senior Chemist, Deputy and HOD for a given branch."""
    from app.models import Branch
    head_roles = [Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD]
    heads = User.query.join(user_roles).filter(
        user_roles.c.role.in_(head_roles),
        User.is_active_user.is_(True),
    ).distinct().all()

    # Treat PHARMACEUTICAL_NR as PHARMACEUTICAL for branch-head matching
    effective_branch = branch
    if branch == Branch.PHARMACEUTICAL_NR:
        effective_branch = Branch.PHARMACEUTICAL

    # Filter by branch – HOD/Deputy may not have a branch (org-wide)
    for head in heads:
        if head.id == exclude_user_id:
            continue
        if head.branches and effective_branch not in head.branches and branch not in head.branches:
            continue
        create_notification(head.id, title, message, link)


def notify_sample_uploaded(sample):
    """Called when an officer uploads a new sample."""
    title = f'New Sample Registered: {sample.lab_number}'
    message = (
        f'Sample "{sample.sample_name}" (Lab# {sample.lab_number}) '
        f'of type {sample.sample_type.value} has been registered and '
        f'is awaiting assignment.'
    )
    link = f'/samples/{sample.id}'
    notify_branch_heads(sample.sample_type, title, message, link)


def notify_sample_assigned(assignment):
    """Called when a senior chemist assigns a sample to a chemist."""
    title = f'Sample Assigned: {assignment.sample.lab_number}'
    message = (
        f'You have been assigned test "{assignment.test_name}" for '
        f'sample "{assignment.sample.sample_name}" '
        f'(Lab# {assignment.sample.lab_number}).'
    )
    link = f'/samples/assignment/{assignment.id}'
    create_notification(assignment.chemist_id, title, message, link)


def notify_report_submitted(assignment):
    """Called when a chemist submits a report."""
    sample = assignment.sample
    # Notify the officer who uploaded the sample (for preliminary review)
    title = f'Report Submitted: {sample.lab_number}'
    message = (
        f'Analyst {assignment.chemist.full_name} has submitted a report '
        f'for test "{assignment.test_name}" on sample '
        f'"{sample.sample_name}" (Lab# {sample.lab_number}). '
        f'A preliminary review is required.'
    )
    link = f'/samples/assignment/{assignment.id}'
    create_notification(sample.uploaded_by, title, message, link)

    # For pharmaceutical samples, also notify the Senior Chemist
    from app.models import AssignmentStatus, Branch
    pharma_types = (Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR)
    if sample.sample_type in pharma_types:
        notify_branch_heads(
            sample.sample_type,
            f'Pharmaceutical Report Submitted: {sample.lab_number}',
            f'Analyst {assignment.chemist.full_name} has submitted a report '
            f'for test "{assignment.test_name}" on pharmaceutical sample '
            f'"{sample.sample_name}" (Lab# {sample.lab_number}). '
            f'Senior Chemist review required.',
            link,
            exclude_user_id=sample.uploaded_by,
        )

    # If returning directly to technical review, also notify branch heads
    if assignment.status == AssignmentStatus.UNDER_TECHNICAL_REVIEW:
        notify_branch_heads(
            sample.sample_type,
            f'Report Ready for Technical Review: {sample.lab_number}',
            f'Analyst {assignment.chemist.full_name} has resubmitted '
            f'report for test "{assignment.test_name}" on sample '
            f'"{sample.sample_name}" (Lab# {sample.lab_number}). '
            f'Technical review required.',
            link,
        )


def notify_preliminary_review_completed(assignment, action):
    """Called when an Officer completes preliminary review."""
    sample = assignment.sample
    action_text = 'approved' if action == 'approved' else 'returned for correction'

    # Notify the chemist
    title = f'Preliminary Review – {action_text.title()}: {sample.lab_number}'
    message = (
        f'Your report for test "{assignment.test_name}" on sample '
        f'"{sample.sample_name}" (Lab# {sample.lab_number}) has been '
        f'{action_text} during preliminary review.'
    )
    if assignment.preliminary_review_comments:
        message += f'\n\nComments: {assignment.preliminary_review_comments}'
    link = f'/samples/assignment/{assignment.id}'
    create_notification(assignment.chemist_id, title, message, link)

    # If approved, notify Senior Chemist for technical review
    if action == 'approved':
        notify_branch_heads(
            sample.sample_type,
            f'Report Ready for Technical Review: {sample.lab_number}',
            f'Report for test "{assignment.test_name}" on sample '
            f'"{sample.sample_name}" (Lab# {sample.lab_number}) '
            f'has passed preliminary review and is ready for '
            f'technical review.',
            link,
        )


def notify_report_reviewed(assignment, action):
    """Called when a Senior Chemist completes technical review."""
    sample = assignment.sample
    action_text = {
        'accepted': 'accepted',
        'rejected': 'rejected',
        'returned': 'returned for correction',
    }.get(action, action)

    title = f'Technical Review – {action_text.title()}: {sample.lab_number}'
    message = (
        f'Your report for test "{assignment.test_name}" on sample '
        f'"{sample.sample_name}" (Lab# {sample.lab_number}) has been '
        f'{action_text} during technical review.'
    )
    if assignment.review_comments:
        message += f'\n\nComments: {assignment.review_comments}'

    link = f'/samples/assignment/{assignment.id}'
    create_notification(assignment.chemist_id, title, message, link)

    # Also notify the uploading officer
    create_notification(
        sample.uploaded_by,
        title,
        f'Report for sample "{sample.sample_name}" '
        f'(Lab# {sample.lab_number}) has been {action_text} '
        f'during technical review.',
        link,
    )

    # If all assignments accepted, notify Senior Chemist to submit to Deputy
    if action == 'accepted':
        from app.models import AssignmentStatus
        all_accepted = all(
            a.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.COMPLETED)
            for a in sample.assignments.all()
        )
        if all_accepted:
            sample_link = f'/samples/{sample.id}'
            notify_branch_heads(
                sample.sample_type,
                f'All Reports Accepted: {sample.lab_number}',
                f'All analyst reports for sample "{sample.sample_name}" '
                f'(Lab# {sample.lab_number}) have been accepted. '
                f'Please prepare submission for the Deputy Government '
                f'Chemist.',
                sample_link,
            )


def notify_submitted_to_deputy(sample):
    """Called when Senior Chemist submits reports to Deputy."""
    title = f'Reports Submitted for Review: {sample.lab_number}'
    message = (
        f'Reports for sample "{sample.sample_name}" '
        f'(Lab# {sample.lab_number}) have been submitted for '
        f'review by the Deputy Government Chemist.'
    )
    link = f'/samples/{sample.id}'
    # Notify Deputy and HOD
    from app.models import Role
    deputies = User.query.join(user_roles).filter(
        user_roles.c.role.in_([Role.DEPUTY, Role.HOD]),
        User.is_active_user.is_(True),
    ).distinct().all()
    for user in deputies:
        create_notification(user.id, title, message, link)


def notify_deputy_review_completed(sample, action):
    """Called when Deputy completes review."""
    action_text = 'approved' if action == 'approved' else 'returned to Senior Chemist'
    title = f'Deputy Review – {action_text.title()}: {sample.lab_number}'
    message = (
        f'Sample "{sample.sample_name}" (Lab# {sample.lab_number}) '
        f'has been {action_text} by the Deputy Government Chemist.'
    )
    if sample.deputy_review_comments:
        message += f'\n\nComments: {sample.deputy_review_comments}'
    link = f'/samples/{sample.id}'

    # Notify branch heads (Senior Chemist)
    notify_branch_heads(sample.sample_type, title, message, link)

    # Notify the uploading officer
    create_notification(sample.uploaded_by, title, message, link)


def notify_certificate_prepared(sample):
    """Called when Deputy prepares Certificate of Analysis."""
    title = f'Certificate Ready for Review: {sample.lab_number}'
    message = (
        f'Certificate of Analysis for sample "{sample.sample_name}" '
        f'(Lab# {sample.lab_number}) has been prepared and is '
        f'awaiting review and signing by the Government Chemist.'
    )
    link = f'/samples/{sample.id}'
    # Notify HOD
    from app.models import Role
    hods = User.query.join(user_roles).filter(
        user_roles.c.role == Role.HOD,
        User.is_active_user.is_(True),
    ).distinct().all()
    for hod in hods:
        create_notification(hod.id, title, message, link)


def notify_certificate_signed(sample, action):
    """Called when Government Chemist reviews the certificate."""
    if action == 'sign':
        title = f'Certificate Signed: {sample.lab_number}'
        message = (
            f'Certificate of Analysis for sample "{sample.sample_name}" '
            f'(Lab# {sample.lab_number}) has been signed by the '
            f'Government Chemist. The sample analysis process is complete.'
        )
    else:  # returned
        title = f'Certificate Returned: {sample.lab_number}'
        message = (
            f'Certificate of Analysis for sample "{sample.sample_name}" '
            f'(Lab# {sample.lab_number}) has been returned by the '
            f'Government Chemist for correction.'
        )
        if sample.hod_review_comments:
            message += f'\n\nComments: {sample.hod_review_comments}'

    link = f'/samples/{sample.id}'

    # Notify Deputy, branch heads, and the uploading officer
    from app.models import Role
    deputies = User.query.join(user_roles).filter(
        user_roles.c.role == Role.DEPUTY,
        User.is_active_user.is_(True),
    ).distinct().all()
    for dep in deputies:
        create_notification(dep.id, title, message, link)

    notify_branch_heads(sample.sample_type, title, message, link)
    create_notification(sample.uploaded_by, title, message, link)
