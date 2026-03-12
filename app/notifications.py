"""Email and in-app notification helpers."""

from threading import Thread

from flask import current_app, render_template_string
from flask_mail import Message

from app import db, mail
from app.models import Notification, User, Branch, Role


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
    head_roles = [Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD]
    heads = User.query.filter(
        User.role.in_(head_roles),
        User.is_active_user.is_(True),
    ).all()

    # Filter by branch – HOD/Deputy may not have a branch (org-wide)
    for head in heads:
        if head.id == exclude_user_id:
            continue
        if head.branch is not None and head.branch != branch:
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
    # Notify the officer who uploaded the sample
    title = f'Report Submitted: {sample.lab_number}'
    message = (
        f'Analyst {assignment.chemist.full_name} has submitted a report '
        f'for test "{assignment.test_name}" on sample '
        f'"{sample.sample_name}" (Lab# {sample.lab_number}).'
    )
    link = f'/samples/assignment/{assignment.id}'
    create_notification(sample.uploaded_by, title, message, link)

    # Notify branch heads for review
    notify_branch_heads(
        sample.sample_type,
        f'Report Ready for Review: {sample.lab_number}',
        message,
        link,
    )


def notify_report_reviewed(assignment, action):
    """Called when a branch head reviews a report."""
    sample = assignment.sample
    action_text = {
        'accepted': 'accepted',
        'rejected': 'rejected',
        'returned': 'returned for correction',
        'completed': 'marked as completed',
    }.get(action, action)

    title = f'Report {action_text.title()}: {sample.lab_number}'
    message = (
        f'Your report for test "{assignment.test_name}" on sample '
        f'"{sample.sample_name}" (Lab# {sample.lab_number}) has been '
        f'{action_text}.'
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
        f'(Lab# {sample.lab_number}) has been {action_text}.',
        link,
    )
