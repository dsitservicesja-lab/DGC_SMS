from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app import db
from app.auth import auth_bp
from app.forms import LoginForm, UserCreateForm, UserEditForm
from app.models import User, Role, Branch


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
