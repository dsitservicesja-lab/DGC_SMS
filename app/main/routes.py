from flask import (
    render_template, redirect, url_for, flash, jsonify, request,
    current_app, Response, abort, send_file,
)
from flask_login import login_required, current_user
from datetime import datetime, timezone, date
import csv
import enum
import io

from app import db
from app.main import main_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, Notification, User,
    Role, SampleStatus, AssignmentStatus, Setting, Branch, Permission,
    KpiTarget, KPI_METRICS, AUTO_ACTUAL_KEYS,
    NonWorkingDay, calculate_working_days, jamaica_now,
    DocumentVersion, BackDateRequest,
    fiscal_year_for_date, fiscal_quarter_for_date,
    fiscal_quarter_months, fiscal_year_date_range,
    SupportingDocument, ReviewHistory, AuditLog,
    user_roles, user_branches, user_permissions,
)


def _current_fiscal_year():
    """Return the current fiscal year (April-March)."""
    return fiscal_year_for_date(jamaica_now())


def _available_fiscal_years():
    """Return sorted list of fiscal years with data, plus the current one."""
    from sqlalchemy import extract
    rows = db.session.query(
        extract('year', Sample.date_registered).label('yr'),
        extract('month', Sample.date_registered).label('mo'),
    ).distinct().all()
    fy_set = set()
    for r in rows:
        if r.yr and r.mo:
            if int(r.mo) >= 4:
                fy_set.add(int(r.yr))
            else:
                fy_set.add(int(r.yr) - 1)
    fy_set.add(_current_fiscal_year())
    return sorted(fy_set)


def _fiscal_year_filter(query, date_column, year, quarter=None):
    """Apply fiscal year (and optional quarter) filter to a query.
    Financial year: April 1 of `year` to March 31 of `year+1`.
    Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar."""
    start, end = fiscal_year_date_range(year, quarter if quarter in (1, 2, 3, 4) else None)
    return query.filter(date_column >= start, date_column <= end)


def _maybe_send_report_reminders():
    """Run report-date reminders at most once per calendar day.

    Uses the 'last_reminder_date' setting to avoid duplicate runs.
    """
    today_str = date.today().isoformat()
    if Setting.get('last_reminder_date') == today_str:
        return
    try:
        from app.notifications import send_report_date_reminders
        send_report_date_reminders()
        Setting.set('last_reminder_date', today_str)
        db.session.commit()
    except Exception:
        current_app.logger.exception('Failed to send report date reminders')


@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('auth.login'))


@main_bp.route('/dashboard')
@login_required
def dashboard():
    stats = {}

    # Trigger reminder check (at most once per day, stored in settings)
    _maybe_send_report_reminders()

    if current_user.has_role(Role.CHEMIST) and not current_user.has_any_role(Role.OFFICER, Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        my_assignments = SampleAssignment.query.filter_by(
            chemist_id=current_user.id
        )
        stats['total_assignments'] = my_assignments.count()
        stats['pending'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.IN_PROGRESS,
                AssignmentStatus.RETURNED,
            ])
        ).count()
        stats['submitted'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_PRELIMINARY_REVIEW,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = my_assignments.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.COMPLETED,
            ])
        ).count()

    elif current_user.has_role(Role.OFFICER) and not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.DEPUTY, Role.HOD, Role.ADMIN):
        my_samples = Sample.query.filter_by(uploaded_by=current_user.id)
        stats['total_samples'] = my_samples.count()
        stats['registered'] = my_samples.filter_by(
            status=SampleStatus.REGISTERED
        ).count()
        stats['preliminary_review'] = my_samples.filter(
            Sample.status.in_([
                SampleStatus.REPORT_SUBMITTED,
            ])
        ).count()
        stats['in_progress'] = my_samples.filter(
            Sample.status.in_([
                SampleStatus.ASSIGNED,
                SampleStatus.IN_PROGRESS,
                SampleStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = my_samples.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    elif current_user.has_role(Role.DEPUTY) and not current_user.has_any_role(Role.HOD, Role.ADMIN):
        query = Sample.query
        stats['total_samples'] = query.count()
        stats['deputy_review'] = query.filter_by(
            status=SampleStatus.DEPUTY_REVIEW
        ).count()
        stats['certificate_prep'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFICATE_PREPARATION,
                SampleStatus.HOD_RETURNED,
            ])
        ).count()
        stats['completed'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    else:
        # Branch heads, HOD, Admin
        query = Sample.query
        if current_user.branches and current_user.has_role(Role.SENIOR_CHEMIST):
            query = query.filter(Sample.sample_type.in_(current_user.branches))

        stats['total_samples'] = query.count()
        stats['awaiting_assignment'] = query.filter_by(
            status=SampleStatus.REGISTERED
        ).count()
        stats['reports_pending_review'] = SampleAssignment.query.filter(
            SampleAssignment.status.in_([
                AssignmentStatus.REPORT_SUBMITTED,
                AssignmentStatus.UNDER_TECHNICAL_REVIEW,
            ])
        ).count()
        stats['completed'] = query.filter(
            Sample.status.in_([
                SampleStatus.CERTIFIED,
                SampleStatus.COMPLETED,
            ])
        ).count()

    # Recent notifications
    notifications = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).limit(10).all()

    # Upcoming report deadlines (within 7 days)
    from datetime import timedelta
    today = date.today()
    terminal_statuses = [
        SampleStatus.CERTIFIED, SampleStatus.COMPLETED, SampleStatus.REJECTED,
    ]
    deadline_samples = Sample.query.filter(
        Sample.expected_report_date.isnot(None),
        Sample.expected_report_date >= today,
        Sample.expected_report_date <= today + timedelta(days=7),
        Sample.status.notin_(terminal_statuses),
    ).order_by(Sample.expected_report_date.asc()).limit(10).all()

    status_colors = {
        'Registered': 'secondary', 'Assigned': 'primary', 'In Progress': 'info',
        'Report Submitted': 'warning', 'Preliminary Review': 'warning',
        'Technical Review': 'warning', 'Returned for Correction': 'danger',
        'Accepted': 'success', 'Deputy Review': 'info',
        'Returned by Deputy': 'danger', 'Certificate Preparation': 'info',
        'HOD Review': 'info', 'Returned by HOD': 'danger',
    }
    upcoming_deadlines = []
    for s in deadline_samples:
        days_remaining = (s.expected_report_date - today).days
        upcoming_deadlines.append({
            'sample': s,
            'days_remaining': days_remaining,
            'status_color': status_colors.get(s.status.value, 'secondary'),
        })

    return render_template(
        'dashboard.html', stats=stats, notifications=notifications,
        upcoming_deadlines=upcoming_deadlines,
    )


@main_bp.route('/notifications')
@login_required
def notifications():
    notifs = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).all()
    return render_template('notifications.html', notifications=notifs)


@main_bp.route('/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    notif = db.get_or_404(Notification, notif_id)
    if notif.user_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('main.notifications'))
    notif.is_read = True
    db.session.commit()
    if notif.link and notif.link.startswith('/'):
        return redirect(notif.link)
    return redirect(url_for('main.notifications'))


@main_bp.route('/api/notifications/unread-count')
@login_required
def unread_notification_count():
    count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).count()
    return jsonify({'count': count})


@main_bp.route('/api/notifications/latest')
@login_required
def latest_notifications():
    """Return recent unread notifications for live preview."""
    notifs = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).order_by(Notification.created_at.desc()).limit(5).all()
    data = [
        {
            'id': n.id,
            'title': n.title,
            'message': n.message[:120] + ('...' if len(n.message) > 120 else ''),
            'link': n.link,
            'created_at': n.created_at.strftime('%d %b %Y %H:%M') if n.created_at else '',
        }
        for n in notifs
    ]
    return jsonify({'notifications': data})


@main_bp.route('/api/keep-alive')
@login_required
def keep_alive():
    """Lightweight endpoint that refreshes the session cookie."""
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Quarterly KPI Dashboard
# ---------------------------------------------------------------------------

@main_bp.route('/kpi')
@login_required
def kpi():
    from sqlalchemy import extract, func

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    sort_by = request.args.get('sort', 'quarter')
    sort_dir = request.args.get('dir', 'asc')

    available_years = _available_fiscal_years()

    # Quarterly stats using fiscal year quarters
    quarters_data = []
    fiscal_q_labels = {1: 'Q1 (Apr-Jun)', 2: 'Q2 (Jul-Sep)',
                       3: 'Q3 (Oct-Dec)', 4: 'Q4 (Jan-Mar)'}
    for q in range(1, 5):
        start, end = fiscal_year_date_range(year, q)

        base_q = Sample.query.filter(
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        total = base_q.count()
        certified = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()
        in_progress = base_q.filter(
            Sample.status.notin_([
                SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
                SampleStatus.REJECTED
            ])
        ).count()
        rejected = base_q.filter(
            Sample.status == SampleStatus.REJECTED
        ).count()

        # Turnaround: average days from date_registered to certified_at
        avg_tat = None
        certified_samples = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        ).all()
        if certified_samples:
            days_list = []
            for s in certified_samples:
                if s.certified_at and s.date_registered:
                    delta_days = calculate_working_days(s.date_registered, s.certified_at)
                    days_list.append(delta_days) if delta_days is not None else None
            avg_tat = round(sum(days_list) / len(days_list), 1) if days_list else None

        # By branch
        by_branch = {}
        for branch in Branch:
            by_branch[branch.value] = base_q.filter(
                Sample.sample_type == branch
            ).count()

        quarters_data.append({
            'quarter': q,
            'label': fiscal_q_labels[q],
            'total': total,
            'certified': certified,
            'in_progress': in_progress,
            'rejected': rejected,
            'avg_tat': avg_tat,
            'by_branch': by_branch,
        })

    # Sorting
    sort_key = sort_by if sort_by in ('quarter', 'total', 'certified', 'in_progress', 'rejected', 'avg_tat') else 'quarter'
    reverse = (sort_dir == 'desc')
    quarters_data.sort(key=lambda x: (x[sort_key] is None, x[sort_key] if x[sort_key] is not None else 0), reverse=reverse)

    return render_template(
        'kpi.html',
        quarters_data=quarters_data,
        year=year,
        available_years=available_years,
        sort_by=sort_by,
        sort_dir=sort_dir,
        Branch=Branch,
    )


# ---------------------------------------------------------------------------
# KPI Report  (Target / Actual / Variance)
# ---------------------------------------------------------------------------

def _auto_actuals(year, quarter):
    """Return a dict of auto-computed KPI actual values for *year* / *quarter*.
    Uses fiscal year quarters (Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar)."""
    start, end = fiscal_year_date_range(year, quarter)

    def _base(branch_filter):
        q = Sample.query.filter(
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        if isinstance(branch_filter, (list, tuple)):
            q = q.filter(Sample.sample_type.in_(branch_filter))
        else:
            q = q.filter(Sample.sample_type == branch_filter)
        return q

    def _count(branch_filter):
        return _base(branch_filter).filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()

    def _avg_tat(branch_filter):
        samples = _base(branch_filter).filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        ).all()
        days = [
            calculate_working_days(s.date_registered, s.certified_at)
            for s in samples
            if s.certified_at and s.date_registered
        ]
        return round(sum(days) / len(days), 1) if days else None

    pharma_branches = [Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]
    return {
        'pharma_coas':           _count(pharma_branches),
        'milk_coas':             _count(Branch.FOOD_MILK),
        'toxicology_roas':       _count(Branch.TOXICOLOGY),
        'alcohol_coas':          _count(Branch.FOOD_ALCOHOL),
        'avg_days_pharma_coa':   _avg_tat(pharma_branches),
        'avg_days_milk_coa':     _avg_tat(Branch.FOOD_MILK),
        'avg_days_toxicology_roa': _avg_tat(Branch.TOXICOLOGY),
    }


@main_bp.route('/kpi/report')
@login_required
def kpi_report():
    """KPI Target vs Actual report (quarterly)."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    # Available years (fiscal years from sample data + any year that has KPI targets)
    available_years = _available_fiscal_years()
    target_years = {
        r.year for r in db.session.query(KpiTarget.year).distinct().all()
    }
    available_years = sorted(set(available_years) | target_years | {year})

    # Load saved targets for this year/quarter
    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }

    # Auto-computed actual values
    auto = _auto_actuals(year, quarter)

    # Build report rows
    rows = []
    for key, label in KPI_METRICS:
        t_obj = targets.get(key)
        target_val = t_obj.target_value if t_obj else None
        if key in AUTO_ACTUAL_KEYS:
            actual_val = auto.get(key)
        else:
            actual_val = t_obj.actual_override if t_obj else None

        if target_val is not None and actual_val is not None:
            variance = round(actual_val - target_val, 2)
        else:
            variance = None

        rows.append({
            'key': key,
            'label': label,
            'target': target_val,
            'actual': actual_val,
            'variance': variance,
        })

    return render_template(
        'kpi_report.html',
        rows=rows,
        year=year,
        quarter=quarter,
        available_years=available_years,
    )


@main_bp.route('/kpi/report/download')
@login_required
def kpi_report_download():
    """Download the KPI report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=jamaica_now().year)
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }
    auto = _auto_actuals(year, quarter)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['KPI', 'Target', 'Actual', 'Variance'])
    for key, label in KPI_METRICS:
        t_obj = targets.get(key)
        target_val = t_obj.target_value if t_obj else ''
        if key in AUTO_ACTUAL_KEYS:
            actual_val = auto.get(key)
            actual_val = actual_val if actual_val is not None else ''
        else:
            actual_val = t_obj.actual_override if t_obj and t_obj.actual_override is not None else ''

        if target_val != '' and actual_val != '':
            variance = round(float(actual_val) - float(target_val), 2)
        else:
            variance = ''
        writer.writerow([label, target_val, actual_val, variance])

    filename = f'KPI_Report_{year}_Q{quarter}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# KPI Targets Management  (Admin / HOD)
# ---------------------------------------------------------------------------

@main_bp.route('/kpi/targets', methods=['GET', 'POST'])
@login_required
def kpi_targets():
    """Set KPI targets and manual actuals for a given year/quarter."""
    if not current_user.has_any_role(Role.HOD, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=1)
    if quarter not in (1, 2, 3, 4):
        quarter = 1

    if request.method == 'POST':
        try:
            year = int(request.form.get('year', year))
            quarter = int(request.form.get('quarter', quarter))
        except (ValueError, TypeError):
            flash('Invalid year or quarter.', 'danger')
            return redirect(url_for('main.kpi_targets'))
        if quarter not in (1, 2, 3, 4):
            quarter = 1
        for key, _label in KPI_METRICS:
            target_raw = request.form.get(f'target_{key}', '').strip()
            actual_raw = request.form.get(f'actual_{key}', '').strip()

            try:
                target_val = float(target_raw) if target_raw else None
                actual_val = float(actual_raw) if actual_raw else None
            except (ValueError, TypeError):
                continue  # skip malformed values

            existing = KpiTarget.query.filter_by(
                year=year, quarter=quarter, kpi_key=key
            ).first()
            if existing:
                existing.target_value = target_val
                existing.actual_override = actual_val
            else:
                db.session.add(KpiTarget(
                    year=year, quarter=quarter, kpi_key=key,
                    target_value=target_val, actual_override=actual_val,
                ))
        db.session.commit()
        flash('KPI targets saved.', 'success')
        return redirect(url_for('main.kpi_targets', year=year, quarter=quarter))

    targets = {
        t.kpi_key: t
        for t in KpiTarget.query.filter_by(year=year, quarter=quarter).all()
    }

    # Available years (fiscal years)
    available_years = _available_fiscal_years()
    target_years = {
        r.year for r in db.session.query(KpiTarget.year).distinct().all()
    }
    fy = _current_fiscal_year()
    available_years = sorted(
        set(available_years) | target_years | {fy, fy + 1}
    )

    return render_template(
        'kpi_targets.html',
        kpi_metrics=KPI_METRICS,
        auto_keys=AUTO_ACTUAL_KEYS,
        targets=targets,
        year=year,
        quarter=quarter,
        available_years=available_years,
    )


# ---------------------------------------------------------------------------
# Pharmaceutical Reports
# ---------------------------------------------------------------------------

@main_bp.route('/reports/pharma')
@login_required
def pharma_report():
    """Pharmaceutical sample report with filtering and download."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    status_filter = request.args.get('status', '')

    q = Sample.query.filter(
        Sample.sample_type.in_([Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]),
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter else None)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    # Summary stats
    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    tat_days = [
        calculate_working_days(s.date_registered, s.certified_at)
        for s in samples
        if s.certified_at and s.date_registered
        and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    ]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    return render_template(
        'pharma_report.html',
        samples=samples,
        year=year,
        quarter=quarter,
        status_filter=status_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        SampleStatus=SampleStatus,
    )


@main_bp.route('/reports/pharma/download')
@login_required
def pharma_report_download():
    """Download pharmaceutical report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)

    q = Sample.query.filter(
        Sample.sample_type.in_([Branch.PHARMACEUTICAL, Branch.PHARMACEUTICAL_NR]),
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    samples = q.order_by(Sample.date_registered.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Type', 'Formulation',
        'Status', 'Date Received', 'Date Registered',
        'Expected Report Date', 'Certified Date', 'Turnaround (days)',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at)
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.sample_type.value if s.sample_type else '',
            s.formulation_type or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.expected_report_date.isoformat() if s.expected_report_date else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    filename = f'Pharmaceutical_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Milk Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/milk')
@login_required
def milk_report():
    """Milk sample report with filtering and download."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    status_filter = request.args.get('status', '')

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_MILK,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter else None)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    # Summary stats
    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    tat_days = [
        calculate_working_days(s.date_registered, s.certified_at)
        for s in samples
        if s.certified_at and s.date_registered
        and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    ]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    return render_template(
        'milk_report.html',
        samples=samples,
        year=year,
        quarter=quarter,
        status_filter=status_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        SampleStatus=SampleStatus,
    )


@main_bp.route('/reports/milk/download')
@login_required
def milk_report_download():
    """Download milk report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_MILK,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    samples = q.order_by(Sample.date_registered.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Source', 'Milk Type', 'Volume',
        'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (days)',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at)
        milk_type_label = ''
        if s.milk_type == 'R':
            milk_type_label = 'Raw Milk'
        elif s.milk_type == 'P':
            milk_type_label = 'Processed Milk'
        writer.writerow([
            s.lab_number,
            s.sample_name,
            milk_type_label,
            s.volume or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    filename = f'Milk_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Toxicology Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/toxicology')
@login_required
def toxicology_report():
    """Toxicology sample report with filtering."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    status_filter = request.args.get('status', '')

    q = Sample.query.filter(
        Sample.sample_type == Branch.TOXICOLOGY,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter else None)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    tat_days = [
        calculate_working_days(s.date_registered, s.certified_at)
        for s in samples
        if s.certified_at and s.date_registered
        and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    ]
    tat_days = [d for d in tat_days if d is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    available_years = _available_fiscal_years()

    return render_template(
        'toxicology_report.html',
        samples=samples,
        year=year,
        quarter=quarter,
        status_filter=status_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        SampleStatus=SampleStatus,
    )


@main_bp.route('/reports/toxicology/download')
@login_required
def toxicology_report_download():
    """Download toxicology report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)

    q = Sample.query.filter(
        Sample.sample_type == Branch.TOXICOLOGY,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    samples = q.order_by(Sample.date_registered.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Sample Type', 'Patient Name',
        'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (working days)',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at) or ''
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.toxicology_sample_type_name or '',
            s.patient_name or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    filename = f'Toxicology_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Alcohol Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/alcohol')
@login_required
def alcohol_report():
    """Alcohol sample report with filtering and download."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    status_filter = request.args.get('status', '')

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_ALCOHOL,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter else None)

    if status_filter:
        try:
            st = SampleStatus(status_filter)
            q = q.filter(Sample.status == st)
        except ValueError:
            pass

    samples = q.order_by(Sample.date_registered.desc()).all()

    total = len(samples)
    certified = sum(
        1 for s in samples
        if s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    )
    in_progress = sum(
        1 for s in samples
        if s.status not in (
            SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
            SampleStatus.REJECTED,
        )
    )
    rejected = sum(1 for s in samples if s.status == SampleStatus.REJECTED)

    tat_days = [
        calculate_working_days(s.date_registered, s.certified_at)
        for s in samples
        if s.certified_at and s.date_registered
        and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)
    ]
    tat_days = [d for d in tat_days if d is not None]
    avg_tat = round(sum(tat_days) / len(tat_days), 1) if tat_days else None

    available_years = _available_fiscal_years()

    return render_template(
        'alcohol_report.html',
        samples=samples,
        year=year,
        quarter=quarter,
        status_filter=status_filter,
        available_years=available_years,
        total=total,
        certified=certified,
        in_progress=in_progress,
        rejected=rejected,
        avg_tat=avg_tat,
        SampleStatus=SampleStatus,
    )


@main_bp.route('/reports/alcohol/download')
@login_required
def alcohol_report_download():
    """Download alcohol report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)

    q = Sample.query.filter(
        Sample.sample_type == Branch.FOOD_ALCOHOL,
    )
    q = _fiscal_year_filter(q, Sample.date_registered, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    samples = q.order_by(Sample.date_registered.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Lab Number', 'Sample Name', 'Alcohol Type', 'Claim/Butt #',
        'Batch/Lot #', 'Status', 'Date Received', 'Date Registered',
        'Certified Date', 'Turnaround (working days)',
    ])
    for s in samples:
        tat = ''
        if (s.certified_at and s.date_registered
                and s.status in (SampleStatus.CERTIFIED, SampleStatus.COMPLETED)):
            tat = calculate_working_days(s.date_registered, s.certified_at) or ''
        writer.writerow([
            s.lab_number,
            s.sample_name,
            s.alcohol_type or '',
            s.claim_butt_number or '',
            s.batch_lot_number or '',
            s.status.value if s.status else '',
            s.date_received.isoformat() if s.date_received else '',
            s.date_registered.strftime('%Y-%m-%d') if s.date_registered else '',
            s.certified_at.strftime('%Y-%m-%d') if s.certified_at else '',
            tat,
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    filename = f'Alcohol_Report_{year}{q_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Toxicology KPI Report
# ---------------------------------------------------------------------------

@main_bp.route('/kpi/toxicology')
@login_required
def kpi_toxicology():
    """Toxicology-specific KPI report."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int, default=_current_fiscal_year())
    available_years = _available_fiscal_years()

    fiscal_q_labels = {1: 'Q1 (Apr-Jun)', 2: 'Q2 (Jul-Sep)',
                       3: 'Q3 (Oct-Dec)', 4: 'Q4 (Jan-Mar)'}
    quarters_data = []
    for q_num in range(1, 5):
        start, end = fiscal_year_date_range(year, q_num)

        base_q = Sample.query.filter(
            Sample.sample_type == Branch.TOXICOLOGY,
            Sample.date_registered >= start,
            Sample.date_registered <= end,
        )
        total = base_q.count()
        certified = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED])
        ).count()
        in_progress = base_q.filter(
            Sample.status.notin_([
                SampleStatus.CERTIFIED, SampleStatus.COMPLETED,
                SampleStatus.REJECTED
            ])
        ).count()
        rejected = base_q.filter(
            Sample.status == SampleStatus.REJECTED
        ).count()

        cert_samples = base_q.filter(
            Sample.status.in_([SampleStatus.CERTIFIED, SampleStatus.COMPLETED]),
            Sample.certified_at.isnot(None),
        ).all()
        if cert_samples:
            days_list = [
                calculate_working_days(s.date_registered, s.certified_at)
                for s in cert_samples
                if s.certified_at and s.date_registered
            ]
            days_list = [d for d in days_list if d is not None]
            avg_tat = round(sum(days_list) / len(days_list), 1) if days_list else None
        else:
            avg_tat = None

        quarters_data.append({
            'quarter': q_num,
            'label': fiscal_q_labels[q_num],
            'total': total,
            'certified': certified,
            'in_progress': in_progress,
            'rejected': rejected,
            'avg_tat': avg_tat,
        })

    return render_template(
        'kpi_toxicology.html',
        quarters_data=quarters_data,
        year=year,
        available_years=available_years,
    )


# ---------------------------------------------------------------------------
# Analyst Performance Report
# ---------------------------------------------------------------------------

@main_bp.route('/reports/analysts')
@login_required
def analyst_report():
    """Analyst performance report: tests completed per analyst with filters."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)  # 0 = all
    branch_filter = request.args.get('branch', '')

    # Build base query on assignments
    q = SampleAssignment.query.join(
        Sample, SampleAssignment.sample_id == Sample.id
    )
    q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                            quarter if quarter else None)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    assignments = q.order_by(SampleAssignment.assigned_date.desc()).all()

    # Group by analyst
    analyst_data = {}
    for a in assignments:
        cid = a.chemist_id
        if cid not in analyst_data:
            analyst_data[cid] = {
                'name': a.chemist.full_name if a.chemist else 'Unknown',
                'total': 0,
                'completed': 0,
                'in_progress': 0,
                'tests': [],
            }
        entry = analyst_data[cid]
        entry['total'] += 1
        if a.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.COMPLETED):
            entry['completed'] += 1
        elif a.status != AssignmentStatus.REJECTED:
            entry['in_progress'] += 1
        entry['tests'].append(a)

    # Sort analysts by completed tests descending
    sort_by = request.args.get('sort', 'completed')
    sort_dir = request.args.get('dir', 'desc')
    reverse = (sort_dir == 'desc')
    if sort_by == 'name':
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['name'].lower(), reverse=reverse)
    elif sort_by == 'total':
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['total'], reverse=reverse)
    else:
        analyst_list = sorted(analyst_data.values(), key=lambda x: x['completed'], reverse=reverse)

    # Available years (fiscal)
    available_years = _available_fiscal_years()

    # Summary totals
    total_assignments = len(assignments)
    total_completed = sum(d['completed'] for d in analyst_data.values())

    return render_template(
        'analyst_report.html',
        analyst_list=analyst_list,
        year=year,
        quarter=quarter,
        branch_filter=branch_filter,
        available_years=available_years,
        total_assignments=total_assignments,
        total_completed=total_completed,
        total_analysts=len(analyst_data),
        Branch=Branch,
        sort_by=sort_by,
        sort_dir=sort_dir,
        AssignmentStatus=AssignmentStatus,
    )


@main_bp.route('/reports/analysts/download')
@login_required
def analyst_report_download():
    """Download analyst performance report as CSV."""
    if not current_user.has_any_role(Role.SENIOR_CHEMIST, Role.HOD,
                                     Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    year = request.args.get('year', type=int,
                            default=_current_fiscal_year())
    quarter = request.args.get('quarter', type=int, default=0)
    branch_filter = request.args.get('branch', '')

    q = SampleAssignment.query.join(
        Sample, SampleAssignment.sample_id == Sample.id
    ).join(
        User, SampleAssignment.chemist_id == User.id
    )
    q = _fiscal_year_filter(q, SampleAssignment.assigned_date, year,
                            quarter if quarter in (1, 2, 3, 4) else None)

    if branch_filter:
        try:
            br = Branch[branch_filter]
            q = q.filter(Sample.sample_type == br)
        except KeyError:
            pass

    assignments = q.order_by(User.last_name, SampleAssignment.assigned_date.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Analyst', 'Lab Number', 'Sample Name', 'Laboratory',
        'Test Name', 'Status', 'Assigned Date', 'Date Completed',
    ])
    for a in assignments:
        writer.writerow([
            a.chemist.full_name if a.chemist else 'Unknown',
            a.sample.lab_number,
            a.sample.sample_name,
            a.sample.sample_type.value if a.sample.sample_type else '',
            a.test_name,
            a.status.value if a.status else '',
            a.assigned_date.strftime('%Y-%m-%d') if a.assigned_date else '',
            a.date_completed.strftime('%Y-%m-%d') if a.date_completed else '',
        ])

    q_label = f'_Q{quarter}' if quarter in (1, 2, 3, 4) else ''
    b_label = f'_{branch_filter}' if branch_filter else ''
    filename = f'Analyst_Report_{year}{q_label}{b_label}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Non-Working Days Calendar Management
# ---------------------------------------------------------------------------

@main_bp.route('/calendar', methods=['GET', 'POST'])
@login_required
def calendar_management():
    """Calendar interface for managing non-working days (Admin/HOD only)."""
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    from app.forms import NonWorkingDayForm
    form = NonWorkingDayForm()

    if form.validate_on_submit():
        existing = NonWorkingDay.query.filter_by(date=form.date.data).first()
        if existing:
            flash('This date is already marked as a non-working day.', 'warning')
        else:
            nwd = NonWorkingDay(
                date=form.date.data,
                description=form.description.data,
                day_type=form.day_type.data,
                created_by=current_user.id,
            )
            db.session.add(nwd)
            db.session.commit()
            flash('Non-working day added.', 'success')
        return redirect(url_for('main.calendar_management'))

    year = request.args.get('year', type=int, default=jamaica_now().year)
    non_working_days = NonWorkingDay.query.filter(
        db.extract('year', NonWorkingDay.date) == year
    ).order_by(NonWorkingDay.date).all()

    return render_template(
        'calendar.html',
        form=form,
        non_working_days=non_working_days,
        year=year,
    )


@main_bp.route('/calendar/<int:nwd_id>/delete', methods=['POST'])
@login_required
def delete_non_working_day(nwd_id):
    """Delete a non-working day entry."""
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    nwd = db.get_or_404(NonWorkingDay, nwd_id)
    db.session.delete(nwd)
    db.session.commit()
    flash('Non-working day removed.', 'success')
    return redirect(url_for('main.calendar_management'))


# ---------------------------------------------------------------------------
# Admin Settings
# ---------------------------------------------------------------------------

@main_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if not current_user.has_any_role(Role.ADMIN, Role.HOD):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        email_enabled = 'email_enabled' in request.form
        Setting.set('email_enabled', str(email_enabled).lower())
        db.session.commit()
        flash('Settings updated.', 'success')
        return redirect(url_for('main.settings'))

    email_enabled = Setting.get_bool('email_enabled', default=True)
    sample_count = Sample.query.count()
    return render_template('settings.html', email_enabled=email_enabled,
                           sample_count=sample_count)


@main_bp.route('/clear-sample-data', methods=['POST'])
@login_required
def clear_sample_data():
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    # Remove uploaded files
    import shutil, os
    upload_folder = current_app.config.get('UPLOAD_FOLDER')
    if upload_folder and os.path.isdir(upload_folder):
        for entry in os.listdir(upload_folder):
            path = os.path.join(upload_folder, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)

    # Delete in order respecting FK constraints
    Notification.query.filter(
        Notification.link.like('%/samples/%')
    ).delete(synchronize_session=False)
    BackDateRequest.query.delete()
    DocumentVersion.query.delete()
    SampleHistory.query.delete()
    SampleAssignment.query.delete()
    from app.models import SupportingDocument
    SupportingDocument.query.delete()
    Sample.query.delete()
    db.session.commit()

    flash('All sample data has been cleared.', 'success')
    return redirect(url_for('main.settings'))


# ---------------------------------------------------------------------------
# Back-Dating Request & Approval
# ---------------------------------------------------------------------------

@main_bp.route('/backdate-requests')
@login_required
def backdate_requests():
    """View pending back-date requests (HOD/Deputy only)."""
    if not current_user.has_any_role(Role.HOD, Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    status_filter = request.args.get('status', 'pending')
    q = BackDateRequest.query
    if status_filter in ('pending', 'approved', 'denied'):
        q = q.filter_by(status=status_filter)
    requests_list = q.order_by(BackDateRequest.requested_at.desc()).all()

    return render_template(
        'backdate_requests.html',
        requests=requests_list,
        status_filter=status_filter,
    )


@main_bp.route('/backdate-requests/<int:req_id>/decide', methods=['POST'])
@login_required
def decide_backdate(req_id):
    """Approve or deny a back-date request."""
    if not current_user.has_any_role(Role.HOD, Role.DEPUTY, Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    bdr = db.get_or_404(BackDateRequest, req_id)
    if bdr.status != 'pending':
        flash('This request has already been decided.', 'warning')
        return redirect(url_for('main.backdate_requests'))

    decision = request.form.get('decision')
    comments = request.form.get('comments', '')

    if decision not in ('approved', 'denied'):
        flash('Invalid decision.', 'danger')
        return redirect(url_for('main.backdate_requests'))

    bdr.status = decision
    bdr.decided_by = current_user.id
    bdr.decided_at = jamaica_now()
    bdr.decision_comments = comments

    # If approved, apply the back-dated value
    if decision == 'approved':
        from datetime import datetime as dt
        try:
            new_date = dt.strptime(bdr.proposed_date, '%Y-%m-%d').date()

            # Assignment-level fields
            assignment_fields = {
                'assigned_date', 'expected_completion',
                'report_submitted_at', 'test_date',
            }

            # Sample-level DateTime fields (need to preserve the time)
            sample_datetime_fields = {
                'date_registered', 'certificate_prepared_at', 'certified_at',
            }

            if bdr.field_name in assignment_fields and bdr.assignment_id:
                asgn = db.session.get(SampleAssignment, bdr.assignment_id)
                if asgn:
                    if bdr.field_name in ('assigned_date', 'report_submitted_at'):
                        # DateTime columns – preserve the time
                        old_val = getattr(asgn, bdr.field_name, None)
                        if old_val and hasattr(old_val, 'time'):
                            new_value = dt.combine(new_date, old_val.time())
                        else:
                            new_value = dt.combine(new_date, dt.min.time())
                        setattr(asgn, bdr.field_name, new_value)
                    else:
                        setattr(asgn, bdr.field_name, new_date)
            else:
                # Sample-level fields
                sample = db.session.get(Sample, bdr.sample_id)
                if sample and hasattr(sample, bdr.field_name):
                    if bdr.field_name in sample_datetime_fields:
                        # DateTime columns – preserve the time
                        old_val = getattr(sample, bdr.field_name, None)
                        if old_val and hasattr(old_val, 'time'):
                            new_value = dt.combine(new_date, old_val.time())
                        else:
                            new_value = dt.combine(new_date, dt.min.time())
                        setattr(sample, bdr.field_name, new_value)
                    else:
                        setattr(sample, bdr.field_name, new_date)
        except (ValueError, AttributeError):
            current_app.logger.error(
                'Failed to apply back-date for request %d: field=%s, proposed=%s',
                bdr.id, bdr.field_name, bdr.proposed_date,
            )
            flash('Back-date approved but could not be applied automatically. '
                  'Please update the date manually.', 'warning')

    # Log the decision
    requester_name = bdr.requester.full_name if bdr.requester else 'Unknown'
    db.session.add(SampleHistory(
        sample_id=bdr.sample_id,
        action=f'Back-date request {decision}',
        details=(f'Field: {bdr.field_name}, Original: {bdr.original_date}, '
                 f'Proposed: {bdr.proposed_date}, Decision: {decision}. '
                 f'Requested by: {requester_name}'
                 f'{", Comments: " + comments if comments else ""}'),
        performed_by=current_user.id,
        action_type=f'Back-Date {decision.title()}',
        object_affected='Sample' if not bdr.assignment_id else 'Assignment',
        change_description=(f'{bdr.field_name}: {bdr.original_date} → {bdr.proposed_date} '
                           f'({decision} by {current_user.full_name}, '
                           f'requested by {requester_name})'),
    ))
    db.session.commit()

    from app.notifications import notify_backdate_request_decided
    notify_backdate_request_decided(bdr)
    db.session.commit()

    flash(f'Back-date request {decision}.', 'success')
    return redirect(url_for('main.backdate_requests'))


# ---------------------------------------------------------------------------
# Activity History PDF Export
# ---------------------------------------------------------------------------

@main_bp.route('/samples/<int:sample_id>/history/pdf')
@login_required
def export_history_pdf(sample_id):
    """Export sample activity history as a simple HTML-based printable page."""
    sample = db.get_or_404(Sample, sample_id)
    history = SampleHistory.query.filter_by(
        sample_id=sample_id
    ).order_by(SampleHistory.created_at.asc()).all()

    return render_template(
        'history_export.html',
        sample=sample,
        history=history,
        now=jamaica_now(),
    )


# ---------------------------------------------------------------------------
# Document Preview
# ---------------------------------------------------------------------------

@main_bp.route('/preview/<path:filename>')
@login_required
def preview_file(filename):
    """Serve a file for inline preview."""
    import os
    from werkzeug.security import safe_join
    upload_folder = current_app.config['UPLOAD_FOLDER']
    filepath = safe_join(upload_folder, filename)
    if filepath is None or not os.path.isfile(filepath):
        abort(404)

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    # Determine MIME type for inline preview
    mime_map = {
        'pdf': 'application/pdf',
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif': 'image/gif',
        'bmp': 'image/bmp',
        'tiff': 'image/tiff',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }
    mime_type = mime_map.get(ext, 'application/octet-stream')

    from flask import send_from_directory
    return send_from_directory(
        upload_folder, filename,
        mimetype=mime_type,
        as_attachment=False,
    )


# ---------------------------------------------------------------------------
# Data Export / Import  (Admin only)
# ---------------------------------------------------------------------------

def _serialize_value(val):
    """Convert a Python value to a JSON-safe representation."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, enum.Enum):
        return val.value
    return val


def _table_to_dicts(model_class):
    """Serialize all rows of a SQLAlchemy model to a list of dicts."""
    rows = []
    mapper = db.inspect(model_class)
    columns = [c.key for c in mapper.columns]
    for obj in model_class.query.all():
        row = {}
        for col in columns:
            row[col] = _serialize_value(getattr(obj, col))
        rows.append(row)
    return rows


def _assoc_table_to_dicts(table):
    """Serialize an association table to a list of dicts."""
    rows = []
    result = db.session.execute(table.select()).fetchall()
    col_names = [c.name for c in table.columns]
    for r in result:
        row = {}
        for i, name in enumerate(col_names):
            row[name] = _serialize_value(r[i])
        rows.append(row)
    return rows


@main_bp.route('/export-data')
@login_required
def export_data():
    """Export all application data as a ZIP file (JSON + uploaded files).
    Admin-only."""
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    import json
    import zipfile
    import os

    # Build the JSON payload with all tables
    data = {
        'export_version': 2,
        'exported_at': jamaica_now().isoformat(),
        'tables': {
            'users': _table_to_dicts(User),
            'user_roles': _assoc_table_to_dicts(user_roles),
            'user_branches': _assoc_table_to_dicts(user_branches),
            'user_permissions': _assoc_table_to_dicts(user_permissions),
            'settings': _table_to_dicts(Setting),
            'samples': _table_to_dicts(Sample),
            'sample_assignments': _table_to_dicts(SampleAssignment),
            'sample_history': _table_to_dicts(SampleHistory),
            'review_history': _table_to_dicts(ReviewHistory),
            'notifications': _table_to_dicts(Notification),
            'kpi_targets': _table_to_dicts(KpiTarget),
            'non_working_days': _table_to_dicts(NonWorkingDay),
            'supporting_documents': _table_to_dicts(SupportingDocument),
            'document_versions': _table_to_dicts(DocumentVersion),
            'back_date_requests': _table_to_dicts(BackDateRequest),
            'audit_log': _table_to_dicts(AuditLog),
        },
    }

    # Counts for quick verification
    data['row_counts'] = {k: len(v) for k, v in data['tables'].items()}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('data.json', json.dumps(data, indent=2, default=str))

        # Bundle uploaded files
        upload_folder = current_app.config.get('UPLOAD_FOLDER', '')
        if upload_folder and os.path.isdir(upload_folder):
            for root, _dirs, files in os.walk(upload_folder):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(full_path, upload_folder)
                    zf.write(full_path, f'uploads/{arc_name}')

    buf.seek(0)
    timestamp = jamaica_now().strftime('%Y%m%d_%H%M%S')
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'dgc_sms_export_{timestamp}.zip',
    )


def _parse_date(val):
    """Parse an ISO date string to a date object, or return None."""
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_datetime(val):
    """Parse an ISO datetime string to a datetime object, or return None."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_enum(val, enum_class):
    """Convert a string value to the corresponding enum member, or None."""
    if val is None:
        return None
    for member in enum_class:
        if member.value == val:
            return member
    return None


# Column type hints for correct deserialization
_DATE_COLUMNS = {
    'date_received', 'expected_report_date', 'expiration_date',
    'expected_completion', 'test_date', 'date',
}
_DATETIME_COLUMNS = {
    'created_at', 'date_registered', 'summary_report_at',
    'deputy_reviewed_at', 'certificate_prepared_at',
    'hod_reviewed_at', 'certified_at', 'assigned_date',
    'date_completed', 'report_submitted_at',
    'preliminary_reviewed_at', 'reviewed_at', 'uploaded_at',
    'reviewed_at', 'requested_at', 'decided_at',
    'performed_at', 'locked_until',
}


def _coerce_row(table_name, row):
    """Coerce string values back to proper Python types for a given table."""
    import copy
    row = copy.copy(row)

    # Enum columns per table
    enum_map = {
        'users': {'role': Role, 'branch': Branch},
        'user_roles': {'role': Role},
        'user_branches': {'branch': Branch},
        'user_permissions': {'permission': Permission},
        'samples': {
            'sample_type': Branch,
            'status': SampleStatus,
        },
        'sample_assignments': {'status': AssignmentStatus},
    }

    enums = enum_map.get(table_name, {})
    for col, val in list(row.items()):
        if col in enums:
            row[col] = _parse_enum(val, enums[col])
        elif col in _DATE_COLUMNS:
            row[col] = _parse_date(val)
        elif col in _DATETIME_COLUMNS:
            row[col] = _parse_datetime(val)
        elif isinstance(val, str) and val == '':
            # Keep empty strings as-is for text columns
            pass

    # Boolean columns
    bool_cols = {
        'is_active_user', 'must_change_password', 'is_read',
        'email_sent', 'out_of_spec',
    }
    for col in bool_cols:
        if col in row and row[col] is not None:
            row[col] = bool(row[col])

    return row


@main_bp.route('/import-data', methods=['GET', 'POST'])
@login_required
def import_data():
    """Import application data from a previously exported ZIP file.
    Admin-only. This REPLACES all data in the database."""
    if not current_user.has_role(Role.ADMIN):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'GET':
        return redirect(url_for('main.settings'))

    import json
    import zipfile
    import os
    import shutil

    f = request.files.get('import_file')
    if not f or not f.filename:
        flash('No file selected.', 'warning')
        return redirect(url_for('main.settings'))

    if not f.filename.lower().endswith('.zip'):
        flash('Please upload a .zip export file.', 'danger')
        return redirect(url_for('main.settings'))

    try:
        zf = zipfile.ZipFile(f.stream)
    except zipfile.BadZipFile:
        flash('Invalid ZIP file.', 'danger')
        return redirect(url_for('main.settings'))

    if 'data.json' not in zf.namelist():
        flash('Invalid export file — missing data.json.', 'danger')
        return redirect(url_for('main.settings'))

    try:
        raw = zf.read('data.json')
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError):
        flash('Corrupt data.json in export file.', 'danger')
        return redirect(url_for('main.settings'))

    if 'tables' not in data:
        flash('Invalid export format — missing tables key.', 'danger')
        return redirect(url_for('main.settings'))

    tables = data['tables']

    # --- Wipe existing data in reverse dependency order ---
    # Disable FK checks for the duration of the import
    try:
        # Delete in FK-safe order (children first)
        AuditLog.query.delete()
        BackDateRequest.query.delete()
        DocumentVersion.query.delete()
        SupportingDocument.query.delete()
        ReviewHistory.query.delete()
        Notification.query.delete()
        SampleHistory.query.delete()
        SampleAssignment.query.delete()
        Sample.query.delete()
        KpiTarget.query.delete()
        NonWorkingDay.query.delete()
        Setting.query.delete()
        db.session.execute(user_roles.delete())
        db.session.execute(user_branches.delete())
        db.session.execute(user_permissions.delete())
        User.query.delete()
        db.session.flush()

        # --- Insert in FK-safe order (parents first) ---

        # 1. Users (without roles/branches association — those come next)
        for row in tables.get('users', []):
            row = _coerce_row('users', row)
            user = User(
                id=row.get('id'),
                email=row['email'],
                username=row['username'],
                first_name=row['first_name'],
                last_name=row['last_name'],
                password_hash=row['password_hash'],
                role=row.get('role'),
                branch=row.get('branch'),
                is_active_user=row.get('is_active_user', True),
                must_change_password=row.get('must_change_password', False),
                created_at=row.get('created_at'),
                failed_login_attempts=row.get('failed_login_attempts', 0),
                locked_until=row.get('locked_until'),
            )
            db.session.add(user)
        db.session.flush()

        # 2. User roles & branches
        for row in tables.get('user_roles', []):
            row = _coerce_row('user_roles', row)
            if row.get('role') is not None:
                db.session.execute(user_roles.insert().values(
                    user_id=row['user_id'], role=row['role']
                ))

        for row in tables.get('user_branches', []):
            row = _coerce_row('user_branches', row)
            if row.get('branch') is not None:
                db.session.execute(user_branches.insert().values(
                    user_id=row['user_id'], branch=row['branch']
                ))

        for row in tables.get('user_permissions', []):
            row = _coerce_row('user_permissions', row)
            if row.get('permission') is not None:
                db.session.execute(user_permissions.insert().values(
                    user_id=row['user_id'], permission=row['permission']
                ))
        db.session.flush()

        # 3. Settings
        for row in tables.get('settings', []):
            db.session.add(Setting(key=row['key'], value=row.get('value', '')))
        db.session.flush()

        # 4. Samples
        for row in tables.get('samples', []):
            row = _coerce_row('samples', row)
            s = Sample()
            for col, val in row.items():
                if hasattr(s, col):
                    setattr(s, col, val)
            db.session.add(s)
        db.session.flush()

        # 5. Sample Assignments
        for row in tables.get('sample_assignments', []):
            row = _coerce_row('sample_assignments', row)
            sa = SampleAssignment()
            for col, val in row.items():
                if hasattr(sa, col):
                    setattr(sa, col, val)
            db.session.add(sa)
        db.session.flush()

        # 6. Sample History
        for row in tables.get('sample_history', []):
            row = _coerce_row('sample_history', row)
            sh = SampleHistory()
            for col, val in row.items():
                if hasattr(sh, col):
                    setattr(sh, col, val)
            db.session.add(sh)

        # 7. Review History
        for row in tables.get('review_history', []):
            row = _coerce_row('review_history', row)
            rh = ReviewHistory()
            for col, val in row.items():
                if hasattr(rh, col):
                    setattr(rh, col, val)
            db.session.add(rh)

        # 8. Notifications
        for row in tables.get('notifications', []):
            row = _coerce_row('notifications', row)
            n = Notification()
            for col, val in row.items():
                if hasattr(n, col):
                    setattr(n, col, val)
            db.session.add(n)

        # 9. KPI Targets
        for row in tables.get('kpi_targets', []):
            row = _coerce_row('kpi_targets', row)
            kt = KpiTarget()
            for col, val in row.items():
                if hasattr(kt, col):
                    setattr(kt, col, val)
            db.session.add(kt)

        # 10. Non-Working Days
        for row in tables.get('non_working_days', []):
            row = _coerce_row('non_working_days', row)
            nwd = NonWorkingDay()
            for col, val in row.items():
                if hasattr(nwd, col):
                    setattr(nwd, col, val)
            db.session.add(nwd)

        # 11. Supporting Documents
        for row in tables.get('supporting_documents', []):
            row = _coerce_row('supporting_documents', row)
            sd = SupportingDocument()
            for col, val in row.items():
                if hasattr(sd, col):
                    setattr(sd, col, val)
            db.session.add(sd)

        # 12. Document Versions
        for row in tables.get('document_versions', []):
            row = _coerce_row('document_versions', row)
            dv = DocumentVersion()
            for col, val in row.items():
                if hasattr(dv, col):
                    setattr(dv, col, val)
            db.session.add(dv)

        # 13. Back-Date Requests
        for row in tables.get('back_date_requests', []):
            row = _coerce_row('back_date_requests', row)
            bdr = BackDateRequest()
            for col, val in row.items():
                if hasattr(bdr, col):
                    setattr(bdr, col, val)
            db.session.add(bdr)

        # 14. Audit Log
        for row in tables.get('audit_log', []):
            row = _coerce_row('audit_log', row)
            al = AuditLog()
            for col, val in row.items():
                if hasattr(al, col):
                    setattr(al, col, val)
            db.session.add(al)

        db.session.commit()

        # --- Restore uploaded files ---
        upload_folder = current_app.config.get('UPLOAD_FOLDER', '')
        if upload_folder:
            # Clear existing uploads
            if os.path.isdir(upload_folder):
                for entry in os.listdir(upload_folder):
                    path = os.path.join(upload_folder, entry)
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)

            # Extract uploaded files from ZIP
            for name in zf.namelist():
                if name.startswith('uploads/') and not name.endswith('/'):
                    rel_path = name[len('uploads/'):]
                    # Sanitize: prevent directory traversal attacks
                    if '..' in rel_path or rel_path.startswith('/'):
                        continue
                    from werkzeug.utils import secure_filename
                    # Secure each path component individually
                    parts = rel_path.replace('\\', '/').split('/')
                    safe_parts = [secure_filename(p) for p in parts]
                    safe_parts = [p for p in safe_parts if p]  # drop empty
                    if not safe_parts:
                        continue
                    safe_rel = os.path.join(*safe_parts)
                    safe_dest = os.path.join(upload_folder, safe_rel)
                    # Final check: resolved path must be inside upload_folder
                    real_dest = os.path.realpath(safe_dest)
                    real_upload = os.path.realpath(upload_folder)
                    if not real_dest.startswith(real_upload + os.sep):
                        continue
                    os.makedirs(os.path.dirname(safe_dest), exist_ok=True)
                    with zf.open(name) as src, open(safe_dest, 'wb') as dst:
                        dst.write(src.read())

        zf.close()

        row_counts = data.get('row_counts', {})
        total = sum(row_counts.values()) if row_counts else '?'
        flash(f'Data imported successfully — {total} records restored.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Data import failed')
        flash(f'Import failed: {e}', 'danger')

    return redirect(url_for('main.settings'))
