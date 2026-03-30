import io
import json
from datetime import date
from app import db
from app.models import (
    Sample, SampleAssignment, User, Role, Branch,
    SampleStatus, AssignmentStatus,
)
from tests.conftest import _create_user, _login


# Minimal valid PDF bytes for use in file upload tests
_MINIMAL_PDF = (
    b'%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
    b'2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
    b'3 0 obj<</Type/Page/MediaBox[0 0 612 792]>>endobj\n'
    b'xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n'
    b'0000000058 00000 n\n0000000115 00000 n\n'
    b'trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF'
)


def _report_file():
    """Return a (BytesIO, filename) tuple suitable for test file uploads."""
    return (io.BytesIO(_MINIMAL_PDF), 'report.pdf')


def _setup_users(app):
    """Create officer, senior chemist, chemist, deputy, and hod."""
    with app.app_context():
        officer = _create_user(Role.OFFICER, username='officer')
        sc = _create_user(
            Role.SENIOR_CHEMIST, Branch.TOXICOLOGY, username='senior'
        )
        chemist = _create_user(
            Role.CHEMIST, Branch.TOXICOLOGY, username='chemist'
        )
        deputy = _create_user(Role.DEPUTY, username='deputy')
        hod = _create_user(Role.HOD, username='hod')
        return officer.id, sc.id, chemist.id, deputy.id, hod.id


def _register_sample(client):
    """Register a sample via the form."""
    return client.post('/samples/register', data={
        'lab_number': 'TOX-001',
        'sample_name': 'Test Substance',
        'sample_type': 'TOXICOLOGY',
        'date_received': '2026-01-15',
        'description': 'Test sample',
        'quantity': '50ml',
    }, follow_redirects=True)


def test_register_sample(app, client):
    _setup_users(app)
    _login(client, 'officer')
    resp = _register_sample(client)
    assert resp.status_code == 200
    assert b'TOX-001' in resp.data


def test_sample_list(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    resp = client.get('/samples/')
    assert resp.status_code == 200
    assert b'TOX-001' in resp.data


def test_sample_detail(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    with app.app_context():
        sample = Sample.query.first()
    resp = client.get(f'/samples/{sample.id}')
    assert resp.status_code == 200
    assert b'Test Substance' in resp.data


def test_assign_sample(app, client):
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Screening Test',
        'test_reference': 'REF-001',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'assigned successfully' in resp.data


def test_submit_report(app, client):
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    # Assign
    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Blood Analysis',
    }, follow_redirects=True)
    client.get('/auth/logout')

    # Submit report
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'No harmful substances detected.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200
    assert b'submitted successfully' in resp.data

    # Verify assignment is now REPORT_SUBMITTED (awaiting preliminary review)
    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.REPORT_SUBMITTED


def test_preliminary_review(app, client):
    """Test that Officer can do preliminary review after analyst submits."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Analysis',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Test results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Officer does preliminary review
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
        'review_comments': 'Complete and well-documented.',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'approved and forwarded' in resp.data

    # Verify assignment is now UNDER_TECHNICAL_REVIEW
    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.UNDER_TECHNICAL_REVIEW


def test_preliminary_review_return(app, client):
    """Test that Officer can return report during preliminary review."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Analysis',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Incomplete.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Officer returns for correction
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'returned',
        'review_comments': 'Missing sections.',
    }, follow_redirects=True)
    assert b'returned for correction' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.RETURNED
        assert assignment.return_stage == 'preliminary'

    # Chemist resubmits → goes back to REPORT_SUBMITTED (preliminary)
    client.get('/auth/logout')
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Complete findings with all sections.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert b'submitted successfully' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.REPORT_SUBMITTED


def test_preliminary_review_checklist(app, client):
    """Test that preliminary review checklist items are saved."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Analysis',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Test results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Officer does preliminary review with checklist items
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
        'review_comments': 'All checks passed.',
        'chk_original_entry_visible': 'y',
        'chk_entries_signed': 'y',
        'chk_date_recorded': 'y',
        'chk_conclusions_signed_dated': 'y',
        'chk_report_signed_dated': 'y',
        'chk_printouts_attached': 'y',
        'chk_attachments_labeled': 'y',
        'chk_analyst_initials': 'y',
        'chk_templates_completed': 'y',
        'chk_writing_legible': 'y',
        'chk_logbooks_updated': 'y',
        'chk_toc_updated': 'y',
        'chk_pages_numbered': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'approved and forwarded' in resp.data

    # Verify checklist was saved
    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.preliminary_review_checklist is not None
        checklist = json.loads(assignment.preliminary_review_checklist)
        assert checklist['chk_original_entry_visible'] is True
        assert checklist['chk_entries_signed'] is True


def test_preliminary_review_checklist_partial(app, client):
    """Test that unchecked checklist items are saved as False."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Analysis',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Test results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Officer does preliminary review with only some checklist items checked
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'returned',
        'review_comments': 'Missing signatures.',
        'chk_original_entry_visible': 'y',
        'chk_entries_signed': 'y',
        # others not checked
    }, follow_redirects=True)
    assert b'returned for correction' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        checklist = json.loads(assignment.preliminary_review_checklist)
        assert checklist['chk_original_entry_visible'] is True
        assert checklist['chk_entries_signed'] is True
        assert checklist['chk_date_recorded'] is False


def test_technical_review(app, client):
    """Test full flow: submit → preliminary approve → technical accept."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Purity Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Results within acceptable range.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Preliminary review
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
        'review_comments': 'OK',
    })
    client.get('/auth/logout')

    # Technical review
    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
        'review_comments': 'Looks good.',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'accepted' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.ACCEPTED
        sample = Sample.query.first()
        assert sample.status == SampleStatus.ACCEPTED


def test_technical_review_return(app, client):
    """Test technical review return → analyst resubmits → goes to tech review."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Quality Check',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Initial findings.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Preliminary approve
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
    })
    client.get('/auth/logout')

    # Technical review → return
    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'returned',
        'review_comments': 'Please add more detail.',
    }, follow_redirects=True)
    assert b'returned' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.return_stage == 'technical'

    # Chemist resubmits → goes directly to technical review (skips preliminary)
    client.get('/auth/logout')
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Updated detailed findings.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert b'submitted successfully' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.UNDER_TECHNICAL_REVIEW


def test_full_workflow(app, client):
    """Test the complete 26-step workflow end to end."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)

    # 1. Register sample
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    # 2. Assign
    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Full Analysis',
    })
    client.get('/auth/logout')

    # 3. Submit report
    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Comprehensive analysis results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # 4. Preliminary review (Officer)
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
    })
    client.get('/auth/logout')

    # 5. Technical review (Senior Chemist)
    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
        'review_comments': 'Excellent work.',
    })

    # 6. Submit to Deputy
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/submit-to-deputy', data={},
                       follow_redirects=True)
    assert b'submitted to Deputy' in resp.data
    client.get('/auth/logout')

    # 7. Deputy review
    _login(client, 'deputy')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/deputy-review', data={
        'action': 'approved',
        'review_comments': 'All in order.',
    }, follow_redirects=True)
    assert b'approved' in resp.data
    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.CERTIFICATE_PREPARATION

    # 8. Prepare certificate (Deputy)
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/prepare-certificate', data={
        'certificate_text': 'This certifies the sample has been analysed.',
    }, follow_redirects=True)
    assert b'Certificate of Analysis submitted' in resp.data
    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.HOD_REVIEW
    client.get('/auth/logout')

    # 9. HOD signs certificate
    _login(client, 'hod')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/hod-review', data={
        'action': 'sign',
        'review_comments': 'Verified.',
    }, follow_redirects=True)
    assert b'Certificate of Analysis signed' in resp.data

    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.CERTIFIED
        assert sample.certified_by == hod_id


def test_full_workflow_pharma_with_summary(app, client):
    """Test pharmaceutical workflow requires summary report."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)

    # Register pharma sample
    _login(client, 'officer')
    client.post('/samples/register', data={
        'lab_number': 'PHARMA-001',
        'sample_name': 'Test Drug',
        'sample_type': 'PHARMACEUTICAL',
        'date_received': '2026-01-15',
    }, follow_redirects=True)
    client.get('/auth/logout')

    # Assign (need a pharma senior chemist)
    with app.app_context():
        sc_pharma = _create_user(
            Role.SENIOR_CHEMIST, Branch.PHARMACEUTICAL, username='sc_pharma'
        )
        chem_pharma = _create_user(
            Role.CHEMIST, Branch.PHARMACEUTICAL, username='chem_pharma'
        )
        sc_pharma_id = sc_pharma.id
        chem_pharma_id = chem_pharma.id

    _login(client, 'sc_pharma')
    with app.app_context():
        sample = Sample.query.filter_by(lab_number='PHARMA-001').first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chem_pharma_id],
        'test_name': 'Drug Purity',
    })
    client.get('/auth/logout')

    # Submit report
    _login(client, 'chem_pharma')
    with app.app_context():
        assignment = SampleAssignment.query.filter_by(
            sample_id=sample.id
        ).first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Drug meets purity standards.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Preliminary review
    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.filter_by(
            sample_id=sample.id
        ).first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
    })
    client.get('/auth/logout')

    # Technical review
    _login(client, 'sc_pharma')
    with app.app_context():
        assignment = SampleAssignment.query.filter_by(
            sample_id=sample.id
        ).first()
    client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
    })

    # Submit to Deputy WITH summary (required for pharma)
    with app.app_context():
        sample = Sample.query.filter_by(lab_number='PHARMA-001').first()

    # First try without summary → should fail
    resp = client.post(f'/samples/{sample.id}/submit-to-deputy', data={},
                       follow_redirects=True)
    assert b'Summary report is required' in resp.data

    # Now with summary
    resp = client.post(f'/samples/{sample.id}/submit-to-deputy', data={
        'summary_report': 'Summary: Drug meets all pharma standards.',
    }, follow_redirects=True)
    assert b'submitted to Deputy' in resp.data

    with app.app_context():
        sample = Sample.query.filter_by(lab_number='PHARMA-001').first()
        assert sample.summary_report is not None


def test_deputy_return(app, client):
    """Test that Deputy can return submission to Senior Chemist."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
    })
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
    })
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/submit-to-deputy', data={})
    client.get('/auth/logout')

    # Deputy returns
    _login(client, 'deputy')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/deputy-review', data={
        'action': 'returned',
        'review_comments': 'Needs clarification.',
    }, follow_redirects=True)
    assert b'returned to Senior Chemist' in resp.data

    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.DEPUTY_RETURNED

    # Senior Chemist resubmits
    client.get('/auth/logout')
    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/resubmit-to-deputy',
                       follow_redirects=True)
    assert b'Resubmitted to Deputy' in resp.data

    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.DEPUTY_REVIEW


def test_hod_return_certificate(app, client):
    """Test that HOD can return certificate to Deputy."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Results.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    _login(client, 'officer')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
    })
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/review', data={
        'action': 'accepted',
    })
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/submit-to-deputy', data={})
    client.get('/auth/logout')

    _login(client, 'deputy')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/deputy-review', data={
        'action': 'approved',
    })
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/prepare-certificate', data={
        'certificate_text': 'Certificate of Analysis.',
    })
    client.get('/auth/logout')

    # HOD returns certificate
    _login(client, 'hod')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/hod-review', data={
        'action': 'returned',
        'review_comments': 'Fix formatting.',
    }, follow_redirects=True)
    assert b'returned to Deputy' in resp.data

    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.HOD_RETURNED

    # Deputy revises certificate
    client.get('/auth/logout')
    _login(client, 'deputy')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/prepare-certificate', data={
        'certificate_text': 'Revised Certificate of Analysis.',
    }, follow_redirects=True)
    assert b'Certificate of Analysis submitted' in resp.data

    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.HOD_REVIEW


def test_chemist_cannot_register_sample(app, client):
    _setup_users(app)
    _login(client, 'chemist')
    resp = client.get('/samples/register', follow_redirects=True)
    assert b'Only officers can register samples' in resp.data


def test_officer_cannot_assign(app, client):
    _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    with app.app_context():
        sample = Sample.query.first()
    resp = client.get(f'/samples/{sample.id}/assign', follow_redirects=True)
    assert b'Senior Chemists' in resp.data


def test_remove_assignment_by_admin(app, client):
    """Test that Admin can remove an assignment."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Test to Remove',
    }, follow_redirects=True)
    client.get('/auth/logout')

    # Admin removes the assignment
    with app.app_context():
        admin = _create_user(Role.ADMIN, username='admin')
    _login(client, 'admin')
    with app.app_context():
        assignment = SampleAssignment.query.first()
        assignment_id = assignment.id
    resp = client.post(f'/samples/assignment/{assignment_id}/remove',
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b'has been removed' in resp.data

    # Sample should revert to REGISTERED since no assignments left
    with app.app_context():
        sample = Sample.query.first()
        assert sample.status == SampleStatus.REGISTERED
        assert SampleAssignment.query.count() == 0


def test_remove_assignment_by_assigner(app, client):
    """Test that the user who assigned can remove an assignment."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Test to Remove',
    }, follow_redirects=True)

    # Same senior chemist removes the assignment
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/remove',
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b'has been removed' in resp.data


def test_remove_assignment_denied_for_chemist(app, client):
    """Test that a regular chemist cannot remove assignments."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Test',
    }, follow_redirects=True)
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/remove',
                       follow_redirects=True)
    assert b'do not have permission' in resp.data


def test_remove_assignment_audited(app, client):
    """Test that assignment removal is recorded in history."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Audit Test',
    }, follow_redirects=True)

    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/remove',
                follow_redirects=True)

    with app.app_context():
        from app.models import SampleHistory
        history = SampleHistory.query.filter_by(action='Assignment Removed').first()
        assert history is not None
        assert 'Audit Test' in history.details


def test_senior_chemist_can_do_preliminary_review(app, client):
    """Test that Senior Chemist can perform preliminary review."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'SC Prelim Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Test results for SC review.',
        'report_file': _report_file(),
    }, content_type='multipart/form-data')
    client.get('/auth/logout')

    # Senior Chemist does preliminary review (not just Officer)
    _login(client, 'senior')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/preliminary-review', data={
        'action': 'approved',
        'review_comments': 'Senior Chemist preliminary approval.',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'approved and forwarded' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.status == AssignmentStatus.UNDER_TECHNICAL_REVIEW


def test_submit_report_with_return_fields(app, client):
    """Test that report submission saves all_samples_returned and return_quantity."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Return Test',
    })
    client.get('/auth/logout')

    _login(client, 'chemist')
    with app.app_context():
        assignment = SampleAssignment.query.first()
    resp = client.post(f'/samples/assignment/{assignment.id}/report', data={
        'report_text': 'Test report with return info.',
        'report_file': _report_file(),
        'all_samples_returned': 'Yes',
        'return_quantity': '45ml',
    }, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200
    assert b'submitted successfully' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.all_samples_returned == 'Yes'
        assert assignment.return_quantity == '45ml'


def test_assign_with_comments_and_quantity(app, client):
    """Test that assignment saves comments and quantity_volume."""
    officer_id, sc_id, chemist_id, deputy_id, hod_id = _setup_users(app)
    _login(client, 'officer')
    _register_sample(client)
    client.get('/auth/logout')

    _login(client, 'senior')
    with app.app_context():
        sample = Sample.query.first()
    resp = client.post(f'/samples/{sample.id}/assign', data={
        'chemist_ids': [chemist_id],
        'test_name': 'Analysis',
        'comments': 'Handle with care, priority sample.',
        'quantity_volume': '100ml',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'assigned successfully' in resp.data

    with app.app_context():
        assignment = SampleAssignment.query.first()
        assert assignment.comments == 'Handle with care, priority sample.'
        assert assignment.quantity_volume == '100ml'
