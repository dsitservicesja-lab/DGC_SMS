from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user

from app import db
from app.auth import auth_bp
from app.forms import LoginForm, UserCreateForm, UserEditForm, ForgotPasswordForm, ResetPasswordForm, ChangePasswordForm
from app.models import User, Role, Branch, Notification, SampleHistory, SampleAssignment, Sample
from app.notifications import send_email


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data) and user.is_active_user:
            login_user(user, remember=form.remember_me.data)
            if user.must_change_password:
                flash('Please change your password before continuing.', 'warning')
                return redirect(url_for('auth.change_password'))
            next_page = request.args.get('next')
            # Only allow relative redirects to prevent open-redirect
            if next_page and not next_page.startswith('/'):
                next_page = None
            return redirect(next_page or url_for('main.dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('auth/login.html', form=form)


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.is_active_user:
            token = user.get_reset_token()
            reset_url = url_for('auth.reset_password', token=token, _external=True)
            send_email(
                subject='[DGC SMS] Password Reset Request',
                recipients=[user.email],
                body_text=(
                    f'Hello {user.first_name},\n\n'
                    f'To reset your password, visit the following link:\n{reset_url}\n\n'
                    f'This link will expire in 30 minutes.\n'
                    f'If you did not request a password reset, please ignore this email.'
                ),
            )
        # Always show the same message to prevent user enumeration
        flash('If an account with that email exists, a password reset link has been sent.', 'info')
        return redirect(url_for('auth.login'))
    return render_template('auth/forgot_password.html', form=form)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    user = User.verify_reset_token(token)
    if not user:
        flash('The reset link is invalid or has expired.', 'danger')
        return redirect(url_for('auth.forgot_password'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash('Your password has been reset. You can now log in.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('auth/reset_password.html', form=form)


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash('Current password is incorrect.', 'danger')
        else:
            current_user.set_password(form.password.data)
            current_user.must_change_password = False
            db.session.commit()
            flash('Your password has been changed.', 'success')
            return redirect(url_for('main.dashboard'))
    return render_template('auth/change_password.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


# ---------------------------------------------------------------------------
# User management (Admin only)
# ---------------------------------------------------------------------------

@auth_bp.route('/users')
@login_required
def user_list():
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    users = User.query.order_by(User.last_name).all()
    return render_template('auth/user_list.html', users=users)


@auth_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
def user_create():
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    form = UserCreateForm()
    if form.validate_on_submit():
        user = User(
            first_name=form.first_name.data,
            last_name=form.last_name.data,
            username=form.username.data,
            email=form.email.data,
        )
        user.set_password(form.password.data)
        user.must_change_password = True
        user.roles = {Role[r] for r in form.roles.data}
        user.branches = {Branch[b] for b in (form.branches.data or [])}
        db.session.add(user)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to create user %r', form.username.data)
            flash('An error occurred while creating the user. Please try again.', 'danger')
            return render_template('auth/user_form.html', form=form, title='Create User')
        flash(f'User {user.username} created successfully.', 'success')
        return redirect(url_for('auth.user_list'))
    return render_template('auth/user_form.html', form=form, title='Create User')


@auth_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def user_edit(user_id):
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    user = db.get_or_404(User, user_id)
    form = UserEditForm(obj=user)
    if request.method == 'GET':
        form.roles.data = [r.name for r in user.roles]
        form.branches.data = [b.name for b in user.branches]
    if form.validate_on_submit():
        # Check email uniqueness
        existing = User.query.filter(
            User.email == form.email.data, User.id != user.id
        ).first()
        if existing:
            flash('Email already in use by another user.', 'danger')
        else:
            user.first_name = form.first_name.data
            user.last_name = form.last_name.data
            user.email = form.email.data
            user.roles = {Role[r] for r in form.roles.data}
            user.branches = {Branch[b] for b in form.branches.data}
            user.is_active_user = form.is_active_user.data
            if form.new_password.data:
                user.set_password(form.new_password.data)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception('Failed to update user %r', user.username)
                flash('An error occurred while updating the user. Please try again.', 'danger')
                return render_template('auth/user_form.html', form=form, title='Edit User', user=user)
            flash(f'User {user.username} updated.', 'success')
            return redirect(url_for('auth.user_list'))
    return render_template('auth/user_form.html', form=form, title='Edit User', user=user)


@auth_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def user_delete(user_id):
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('auth.user_list'))

    # Block deletion if the user owns samples, is assigned as chemist,
    # performed actions in history, or is referenced as a reviewer/assigner.
    has_samples = user.uploaded_samples.count() > 0
    has_assignments = user.assignments.count() > 0
    has_history = SampleHistory.query.filter_by(performed_by=user.id).first() is not None
    is_assigner = SampleAssignment.query.filter_by(assigned_by=user.id).first() is not None
    is_reviewer = SampleAssignment.query.filter_by(reviewed_by=user.id).first() is not None
    is_prelim_reviewer = SampleAssignment.query.filter_by(preliminary_reviewed_by=user.id).first() is not None
    is_sample_ref = (
        Sample.query.filter(
            (Sample.summary_report_by == user.id)
            | (Sample.deputy_reviewed_by == user.id)
            | (Sample.certificate_prepared_by == user.id)
            | (Sample.hod_reviewed_by == user.id)
            | (Sample.certified_by == user.id)
        ).first()
        is not None
    )

    if any((has_samples, has_assignments, has_history, is_assigner,
            is_reviewer, is_prelim_reviewer, is_sample_ref)):
        flash(
            f'Cannot delete {user.username} — they have related records. '
            'Deactivate the account instead.',
            'warning',
        )
        return redirect(url_for('auth.user_list'))

    # Safe to delete — clean up notifications and role/branch associations first
    Notification.query.filter_by(user_id=user.id).delete()
    db.session.execute(
        db.text('DELETE FROM user_roles WHERE user_id = :uid'), {'uid': user.id}
    )
    db.session.execute(
        db.text('DELETE FROM user_branches WHERE user_id = :uid'), {'uid': user.id}
    )
    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.username} has been deleted.', 'success')
    return redirect(url_for('auth.user_list'))
