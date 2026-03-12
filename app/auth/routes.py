from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app import db
from app.auth import auth_bp
from app.forms import LoginForm, UserCreateForm, UserEditForm, ForgotPasswordForm, ResetPasswordForm
from app.models import User, Role, Branch
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
    if current_user.role not in (Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    users = User.query.order_by(User.last_name).all()
    return render_template('auth/user_list.html', users=users)


@auth_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
def user_create():
    if current_user.role not in (Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    form = UserCreateForm()
    if form.validate_on_submit():
        user = User(
            first_name=form.first_name.data,
            last_name=form.last_name.data,
            username=form.username.data,
            email=form.email.data,
            role=Role[form.role.data],
            branch=Branch[form.branch.data] if form.branch.data else None,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash(f'User {user.username} created successfully.', 'success')
        return redirect(url_for('auth.user_list'))
    return render_template('auth/user_form.html', form=form, title='Create User')


@auth_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def user_edit(user_id):
    if current_user.role not in (Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    user = User.query.get_or_404(user_id)
    form = UserEditForm(obj=user)
    if request.method == 'GET':
        form.role.data = user.role.name
        form.branch.data = user.branch.name if user.branch else ''
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
            user.role = Role[form.role.data]
            user.branch = Branch[form.branch.data] if form.branch.data else None
            user.is_active_user = form.is_active_user.data
            if form.new_password.data:
                user.set_password(form.new_password.data)
            db.session.commit()
            flash(f'User {user.username} updated.', 'success')
            return redirect(url_for('auth.user_list'))
    return render_template('auth/user_form.html', form=form, title='Edit User', user=user)


@auth_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def user_delete(user_id):
    if current_user.role != Role.ADMIN:
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('auth.user_list'))
    # Check for related records
    if user.uploaded_samples.count() or user.assignments.count():
        flash(
            f'Cannot delete {user.username} — they have samples or assignments. '
            'Deactivate the account instead.',
            'warning',
        )
        return redirect(url_for('auth.user_list'))
    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.username} has been deleted.', 'success')
    return redirect(url_for('auth.user_list'))
