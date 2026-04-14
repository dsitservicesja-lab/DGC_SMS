from flask import (
    render_template, redirect, url_for, flash, jsonify, request,
    current_app, Response, abort,
)
from flask_login import login_required, current_user
from datetime import datetime, timezone, date
import csv
import io

from app import db
from app.main import main_bp
from app.models import (
    Sample, SampleAssignment, SampleHistory, Notification, User,
    Role, SampleStatus, AssignmentStatus, Setting, Branch,
    KpiTarget, KPI_METRICS, AUTO_ACTUAL_KEYS,
    NonWorkingDay, calculate_working_days, jamaica_now,
    DocumentVersion, BackDateRequest,
    fiscal_year_for_date, fiscal_quarter_for_date,
    fiscal_quarter_months, fiscal_year_date_range,
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
    if notif.link:
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
        year = int(request.form.get('year', year))
        quarter = int(request.form.get('quarter', quarter))
        for key, _label in KPI_METRICS:
            target_raw = request.form.get(f'target_{key}', '').strip()
            actual_raw = request.form.get(f'actual_{key}', '').strip()

            target_val = float(target_raw) if target_raw else None
            actual_val = float(actual_raw) if actual_raw else None

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
                'name': a.chemist.full_name,
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
            a.chemist.full_name,
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
    SampleHistory.query.delete()
    SampleAssignment.query.delete()
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
        sample = db.session.get(Sample, bdr.sample_id)
        if sample and hasattr(sample, bdr.field_name):
            from datetime import datetime as dt
            try:
                new_date = dt.strptime(bdr.proposed_date, '%Y-%m-%d').date()
                setattr(sample, bdr.field_name, new_date)
            except (ValueError, AttributeError):
                pass

    # Log the decision
    db.session.add(SampleHistory(
        sample_id=bdr.sample_id,
        action=f'Back-date request {decision}',
        details=(f'Field: {bdr.field_name}, Original: {bdr.original_date}, '
                 f'Proposed: {bdr.proposed_date}, Decision: {decision}'
                 f'{", Comments: " + comments if comments else ""}'),
        performed_by=current_user.id,
        action_type=f'Back-Date {decision.title()}',
        object_affected='Sample',
        change_description=(f'{bdr.field_name}: {bdr.original_date} → {bdr.proposed_date} '
                           f'({decision} by {current_user.full_name})'),
    ))
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
    upload_folder = current_app.config['UPLOAD_FOLDER']
    filepath = os.path.join(upload_folder, filename)
    if not os.path.isfile(filepath):
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
