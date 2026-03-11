"""WTForms for the DGC Samples Management System."""

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import (
    StringField, PasswordField, SelectField, TextAreaField,
    DateField, SubmitField, BooleanField, SelectMultipleField,
    widgets,
)
from wtforms.validators import (
    DataRequired, Email, EqualTo, Length, Optional, ValidationError,
)

from app.models import Role, Branch, User


# ---------------------------------------------------------------------------
# Auth forms
# ---------------------------------------------------------------------------

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')


class UserCreateForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=120)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=120)])
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=120)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    password2 = PasswordField(
        'Confirm Password', validators=[DataRequired(), EqualTo('password')]
    )
    role = SelectField('Role', choices=[(r.name, r.value) for r in Role], validators=[DataRequired()])
    branch = SelectField(
        'Branch / Lab',
        choices=[('', '-- Select --')] + [(b.name, b.value) for b in Branch],
        validators=[Optional()],
    )
    submit = SubmitField('Create User')

    def validate_username(self, field):
        if User.query.filter_by(username=field.data).first():
            raise ValidationError('Username already in use.')

    def validate_email(self, field):
        if User.query.filter_by(email=field.data).first():
            raise ValidationError('Email already registered.')


class UserEditForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=120)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=120)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=255)])
    role = SelectField('Role', choices=[(r.name, r.value) for r in Role], validators=[DataRequired()])
    branch = SelectField(
        'Branch / Lab',
        choices=[('', '-- Select --')] + [(b.name, b.value) for b in Branch],
        validators=[Optional()],
    )
    is_active_user = BooleanField('Active')
    submit = SubmitField('Update User')


# ---------------------------------------------------------------------------
# Sample forms
# ---------------------------------------------------------------------------

class SampleRegisterForm(FlaskForm):
    lab_number = StringField('Lab Number', validators=[DataRequired(), Length(max=50)])
    sample_name = StringField('Sample Name', validators=[DataRequired(), Length(max=255)])
    sample_type = SelectField(
        'Sample Type',
        choices=[(b.name, b.value) for b in Branch],
        validators=[DataRequired()],
    )
    description = TextAreaField('Description', validators=[Optional()])
    quantity = StringField('Quantity / Volume', validators=[Optional(), Length(max=100)])
    parish = StringField('Parish', validators=[Optional(), Length(max=100)])
    patient_name = StringField('Patient Name (Toxicology)', validators=[Optional(), Length(max=255)])
    source = StringField('Source', validators=[Optional(), Length(max=255)])
    date_received = DateField('Date Received', validators=[DataRequired()])
    expected_report_date = DateField('Expected Report Date', validators=[Optional()])
    scanned_file = FileField(
        'Scanned Document',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp'],
            'Only PDF and image files allowed.'
        )],
    )
    submit = SubmitField('Register Sample')

    def validate_lab_number(self, field):
        from app.models import Sample
        existing = Sample.query.filter_by(lab_number=field.data).first()
        if existing:
            raise ValidationError('Lab number already exists.')


class SampleEditForm(FlaskForm):
    sample_name = StringField('Sample Name', validators=[DataRequired(), Length(max=255)])
    description = TextAreaField('Description', validators=[Optional()])
    quantity = StringField('Quantity / Volume', validators=[Optional(), Length(max=100)])
    parish = StringField('Parish', validators=[Optional(), Length(max=100)])
    patient_name = StringField('Patient Name (Toxicology)', validators=[Optional(), Length(max=255)])
    source = StringField('Source', validators=[Optional(), Length(max=255)])
    expected_report_date = DateField('Expected Report Date', validators=[Optional()])
    scanned_file = FileField(
        'Replace Scanned Document',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp'],
            'Only PDF and image files allowed.'
        )],
    )
    submit = SubmitField('Update Sample')


class CheckboxSelectMultiple(widgets.ListWidget):
    def __call__(self, field, **kwargs):
        kwargs.setdefault('id', field.id)
        html = ['<div class="row">']
        for val, label, selected, render_kw in field.iter_choices():
            html.append(
                f'<div class="col-md-6 mb-2">'
                f'<div class="form-check">'
                f'<input class="form-check-input" type="checkbox" '
                f'name="{field.name}" value="{val}" '
                f'id="{field.id}-{val}" '
                f'{"checked" if selected else ""}>'
                f'<label class="form-check-label" for="{field.id}-{val}">'
                f'{label}</label></div></div>'
            )
        html.append('</div>')
        return ''.join(html)


class SampleAssignForm(FlaskForm):
    chemist_ids = SelectMultipleField(
        'Assign to Chemist(s)',
        coerce=int,
        widget=CheckboxSelectMultiple(),
        validators=[DataRequired(message='Select at least one chemist.')],
    )
    test_name = StringField('Test Name', validators=[DataRequired(), Length(max=255)])
    test_reference = StringField('Test Reference', validators=[Optional(), Length(max=255)])
    expected_completion = DateField('Expected Completion Date', validators=[Optional()])
    submit = SubmitField('Assign Sample')


# ---------------------------------------------------------------------------
# Report forms
# ---------------------------------------------------------------------------

class ReportSubmitForm(FlaskForm):
    report_text = TextAreaField('Report / Findings', validators=[DataRequired()])
    report_file = FileField(
        'Attach Report File',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp'],
            'Only PDF and image files allowed.'
        )],
    )
    submit = SubmitField('Submit Report')


class ReportReviewForm(FlaskForm):
    action = SelectField(
        'Decision',
        choices=[
            ('', '-- Select Action --'),
            ('accepted', 'Accept Report'),
            ('returned', 'Return for Correction'),
            ('rejected', 'Reject Report'),
            ('completed', 'Mark as Completed'),
        ],
        validators=[DataRequired()],
    )
    review_comments = TextAreaField('Comments', validators=[Optional()])
    submit = SubmitField('Submit Review')
