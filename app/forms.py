"""WTForms for the DGC Samples Management System."""

from datetime import date as date_today
from markupsafe import Markup
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

FORMULATION_TYPE_CHOICES = [
    ('', '-- Select Formulation --'),
    ('Capsule', 'Capsule'),
    ('Tablet', 'Tablet'),
    ('Cream', 'Cream'),
    ('Ointment', 'Ointment'),
    ('Oral Solution', 'Oral Solution'),
    ('Suspension', 'Suspension'),
    ('Solution', 'Solution'),
    ('Injection', 'Injection'),
    ('Powder', 'Powder'),
]

# Predefined test names per sample type (Branch)
TOXICOLOGY_TEST_NAMES = [
    ('Salicylate Test- Direct', 'Salicylate Test- Direct'),
    ('Salicylate Test- Confirmatory', 'Salicylate Test- Confirmatory'),
    ('Test for Phenothiazine', 'Test for Phenothiazine'),
    ('Toxicology Screen for Urine', 'Toxicology Screen for Urine'),
    ('Toxicology Screen for Blood', 'Toxicology Screen for Blood'),
    ('Toxicology Screen for Serum', 'Toxicology Screen for Serum'),
    ('Toxicology Screen for Stomach Content', 'Toxicology Screen for Stomach Content'),
    ('Drugs of Abuse', 'Drugs of Abuse'),
    ('Volatile Reducing Substances', 'Volatile Reducing Substances'),
    ('Formaldehyde in Fish', 'Formaldehyde in Fish'),
    ('Detection of Isovaleric Acid in Blood', 'Detection of Isovaleric Acid in Blood'),
    ('Investigation for Paraquat/ Diquat', 'Investigation for Paraquat/ Diquat'),
    ('Analysis of Unknown/ Food Samples', 'Analysis of Unknown/ Food Samples'),
    ('Test for Arsenic, Antimony, Bismuth and Mercury', 'Test for Arsenic, Antimony, Bismuth and Mercury'),
]

TOXICOLOGY_TEST_REFERENCES = [
    ("Clarke's Isolation and Identification of Drugs, 2nd Edition",
     "Clarke's Isolation and Identification of Drugs, 2nd Edition"),
    ('DGC SOP', 'DGC SOP'),
]

FOOD_MILK_TEST_NAMES = [
    ('% Fats', '% Fats'),
    ('% SNF', '% SNF'),
    ('% Total Solids', '% Total Solids'),
    ('Other', 'Other'),
]

FOOD_MILK_TEST_REFERENCES = [
    ('Chemical Analysis of Foods, Eighth Edition, Pearson',
     'Chemical Analysis of Foods, Eighth Edition, Pearson'),
    ('DGC Standard Operating Procedure, FAP-002 Revision 01',
     'DGC Standard Operating Procedure, FAP-002 Revision 01'),
]

FOOD_ALCOHOL_TEST_NAMES = [
    ('Assay for Denatonium Benzoate', 'Assay for Denatonium Benzoate'),
    ('Alcohol Content/ Determination', 'Alcohol Content/ Determination'),
]

FOOD_ALCOHOL_TEST_REFERENCES = [
    ('DGC SOP', 'DGC SOP'),
    ('U.S.P.', 'U.S.P.'),
    ('Other', 'Other'),
]

PHARMACEUTICAL_TEST_NAMES = [
    ('Acidity', 'Acidity'),
    ('Alcohol Content/ Determination', 'Alcohol Content/ Determination'),
    ('Assay by HPLC', 'Assay by HPLC'),
    ('Assay by polarimetry', 'Assay by polarimetry'),
    ('Assay by Titration', 'Assay by Titration'),
    ('Assay by UV', 'Assay by UV'),
    ('Assay Potentiometric Titration', 'Assay Potentiometric Titration'),
    ('Average Weight', 'Average Weight'),
    ('Conductivity', 'Conductivity'),
    ('Deliverable Volume Stage 1', 'Deliverable Volume Stage 1'),
    ('Deliverable Volume Stage 2', 'Deliverable Volume Stage 2'),
    ('Density', 'Density'),
    ('Disintegration (Tablets and Capsule)', 'Disintegration (Tablets and Capsule)'),
    ('Dissolution by HPLC Analysis', 'Dissolution by HPLC Analysis'),
    ('Dissolution UV Analysis', 'Dissolution UV Analysis'),
    ('Dose and Uniformity of Dose of Oral Drops', 'Dose and Uniformity of Dose of Oral Drops'),
    ('Identification by Chemical Reaction', 'Identification by Chemical Reaction'),
    ('Identification by HPLC', 'Identification by HPLC'),
    ('Identification by IR', 'Identification by IR'),
    ('Identification by Thin Layer Chromatography (TLC)', 'Identification by Thin Layer Chromatography (TLC)'),
    ('Identification by UV', 'Identification by UV'),
    ('Impurities by TLC', 'Impurities by TLC'),
    ('Impurities by UV', 'Impurities by UV'),
    ('Impurities by HPLC', 'Impurities by HPLC'),
    ('Loss on Drying', 'Loss on Drying'),
    ('Minimum Fill', 'Minimum Fill'),
    ('Neutralizing Capacity by Titration', 'Neutralizing Capacity by Titration'),
    ('Non Volatile matter', 'Non Volatile matter'),
    ('Organic Stabilizer', 'Organic Stabilizer'),
    ('pH', 'pH'),
    ('Related Substances by Thin Layer Chromatography', 'Related Substances by Thin Layer Chromatography'),
    ('Residue on Ignition', 'Residue on Ignition'),
    ('Specific Gravity', 'Specific Gravity'),
    ('TLC', 'TLC'),
    ('Uniformity of Content by HPLC', 'Uniformity of Content by HPLC'),
    ('Uniformity of Content by UV', 'Uniformity of Content by UV'),
    ('Uniformity and Accuracy of Delivered Doses from Multidose Containers',
     'Uniformity and Accuracy of Delivered Doses from Multidose Containers'),
    ('Uniformity of Weight (Capsules and Tablets)', 'Uniformity of Weight (Capsules and Tablets)'),
    ('Weight Variation', 'Weight Variation'),
    ('Weight per mL', 'Weight per mL'),
    ('Other', 'Other'),
]

PHARMACEUTICAL_TEST_REFERENCES = [
    ('U.S.P.', 'U.S.P.'),
    ('B.P.', 'B.P.'),
    ('Manufacturers method', 'Manufacturers method'),
    ('J.P.', 'J.P.'),
    ('I.P.', 'I.P.'),
    ('DGC SOP', 'DGC SOP'),
]

# Map Branch enum names to their predefined test names/references
BRANCH_TEST_NAMES = {
    'TOXICOLOGY': TOXICOLOGY_TEST_NAMES,
    'FOOD_MILK': FOOD_MILK_TEST_NAMES,
    'FOOD_ALCOHOL': FOOD_ALCOHOL_TEST_NAMES,
    'PHARMACEUTICAL': PHARMACEUTICAL_TEST_NAMES,
    'PHARMACEUTICAL_NR': PHARMACEUTICAL_TEST_NAMES,
}

BRANCH_TEST_REFERENCES = {
    'TOXICOLOGY': TOXICOLOGY_TEST_REFERENCES,
    'FOOD_MILK': FOOD_MILK_TEST_REFERENCES,
    'FOOD_ALCOHOL': FOOD_ALCOHOL_TEST_REFERENCES,
    'PHARMACEUTICAL': PHARMACEUTICAL_TEST_REFERENCES,
    'PHARMACEUTICAL_NR': PHARMACEUTICAL_TEST_REFERENCES,
}


# ---------------------------------------------------------------------------
# Auth forms
# ---------------------------------------------------------------------------

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')


class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send Reset Link')


class ResetPasswordForm(FlaskForm):
    password = PasswordField('New Password', validators=[DataRequired(), Length(min=6)])
    password2 = PasswordField(
        'Confirm Password', validators=[DataRequired(), EqualTo('password')]
    )
    submit = SubmitField('Reset Password')


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    password = PasswordField('New Password', validators=[DataRequired(), Length(min=6)])
    password2 = PasswordField(
        'Confirm New Password', validators=[DataRequired(), EqualTo('password')]
    )
    submit = SubmitField('Change Password')


class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()

    def pre_validate(self, form):
        valid = {v for v, _ in self.choices}
        for d in self.data:
            if d not in valid:
                raise ValidationError(f'Invalid choice: {d}')


class UserCreateForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=120)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=120)])
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=120)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    password2 = PasswordField(
        'Confirm Password', validators=[DataRequired(), EqualTo('password')]
    )
    roles = MultiCheckboxField('Roles', choices=[(r.name, r.value) for r in Role])
    branches = MultiCheckboxField(
        'Branches / Labs',
        choices=[(b.name, b.value) for b in Branch],
    )
    submit = SubmitField('Create User')

    def validate_roles(self, field):
        if not field.data:
            raise ValidationError('Please select at least one role.')

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
    roles = MultiCheckboxField('Roles', choices=[(r.name, r.value) for r in Role])
    branches = MultiCheckboxField(
        'Branches / Labs',
        choices=[(b.name, b.value) for b in Branch],
    )
    is_active_user = BooleanField('Active')

    def validate_roles(self, field):
        if not field.data:
            raise ValidationError('Please select at least one role.')
    new_password = PasswordField('New Password', validators=[Optional(), Length(min=6)])
    confirm_password = PasswordField(
        'Confirm New Password',
        validators=[Optional(), EqualTo('new_password', message='Passwords must match.')],
    )
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
    expected_report_date = None
    scanned_file = FileField(
        'Scanned Document',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'],
            'Only PDF, image, and Word document files allowed.'
        )],
    )
    submit = SubmitField('Register Sample')

    def validate_lab_number(self, field):
        from app.models import Sample
        existing = Sample.query.filter_by(lab_number=field.data).first()
        if existing:
            raise ValidationError('Lab number already exists.')


# ---------------------------------------------------------------------------
# Type-specific registration forms (for different lab types/sample types)
# ---------------------------------------------------------------------------

class ToxicologySampleRegisterForm(SampleRegisterForm):
    """Registration form for Toxicology samples.
    Uses volume (not quantity), no parish, no expected report date.
    """
    # Override quantity field to remove it (use volume instead)
    quantity = None
    parish = None
    expected_report_date = None

    patient_name = StringField(
        'Patient Name',
        validators=[Optional(), Length(max=255)]
    )
    volume = StringField(
        'Volume',
        validators=[Optional(), Length(max=100)]
    )


class PharmaceuticalSampleRegisterForm(SampleRegisterForm):
    """Registration form for Pharmaceutical samples.
    No parish, add Formulation Type, lab_number auto-generated,
    date_received auto-set from date_registered.
    """
    # Remove parish
    parish = None

    date_received = DateField('Date Received', validators=[DataRequired()])

    # lab_number is auto-generated (not required on form)
    lab_number = StringField('Lab Number', validators=[Optional(), Length(max=50)])

    quantity = StringField(
        'No./Quantity of Sample',
        validators=[Optional(), Length(max=100)]
    )
    formulation_type = SelectField(
        'Formulation Type',
        choices=FORMULATION_TYPE_CHOICES,
        validators=[Optional()],
    )

    def validate_lab_number(self, field):
        """Override: allow empty lab_number (will be auto-generated)."""
        if field.data:
            from app.models import Sample
            existing = Sample.query.filter_by(lab_number=field.data).first()
            if existing:
                raise ValidationError('Lab number already exists.')


class FoodMilkSampleRegisterForm(SampleRegisterForm):
    """Registration form for Food (Milk) samples.
    Uses volume, no parish, no source.
    """
    # Remove parish
    parish = None

    # Rename sample_name to display as "Source" for Milk samples
    sample_name = StringField('Source', validators=[DataRequired(), Length(max=255)])

    # Use volume instead of quantity
    quantity = None
    volume = StringField(
        'Volume',
        validators=[Optional(), Length(max=100)]
    )
    milk_type = SelectField(
        'Milk Type',
        choices=[
            ('R', 'Raw Milk'),
            ('P', 'Processed Milk'),
        ],
        validators=[Optional()]
    )


class FoodAlcoholSampleRegisterForm(SampleRegisterForm):
    """Registration form for Food (Alcohol) samples.
    No parish, alcohol type dropdown, Claim/Butt # field.
    """
    # Remove parish
    parish = None

    quantity = StringField(
        'No./Quantity of Sample',
        validators=[Optional(), Length(max=100)]
    )
    alcohol_type = SelectField(
        'Alcohol Type',
        choices=[
            ('', '-- Select Type --'),
            ('Alcohol Determination', 'Alcohol Determination'),
            ('Denatured Alcohol (bitrex)', 'Denatured Alcohol (bitrex)'),
            ('Alcohol Determination and Denatured', 'Alcohol Determination and Denatured'),
        ],
        validators=[Optional()]
    )
    claim_butt_number = StringField(
        'Claim/Butt #',
        validators=[Optional(), Length(max=100)]
    )


def get_sample_register_form(sample_type):
    """
    Factory function to get the appropriate registration form
    based on the sample type (Branch enum).
    
    Args:
        sample_type: Branch enum value or string
        
    Returns:
        Form class appropriate for the sample type
    """
    from app.models import Branch
    
    # Handle string input (convert to Branch enum)
    if isinstance(sample_type, str):
        try:
            sample_type = Branch[sample_type]
        except (KeyError, TypeError):
            return SampleRegisterForm
    
    form_map = {
        Branch.TOXICOLOGY: ToxicologySampleRegisterForm,
        Branch.PHARMACEUTICAL: PharmaceuticalSampleRegisterForm,
        Branch.PHARMACEUTICAL_NR: PharmaceuticalSampleRegisterForm,
        Branch.FOOD_MILK: FoodMilkSampleRegisterForm,
        Branch.FOOD_ALCOHOL: FoodAlcoholSampleRegisterForm,
    }
    
    return form_map.get(sample_type, SampleRegisterForm)


class SampleEditForm(FlaskForm):
    sample_name = StringField('Sample Name', validators=[DataRequired(), Length(max=255)])
    sample_type = SelectField(
        'Sample Type',
        choices=[(b.name, b.value) for b in Branch],
        validators=[DataRequired()],
    )
    description = TextAreaField('Description', validators=[Optional()])
    quantity = StringField('Quantity / Volume', validators=[Optional(), Length(max=100)])
    volume = StringField('Volume', validators=[Optional(), Length(max=100)])
    parish = StringField('Parish', validators=[Optional(), Length(max=100)])
    patient_name = StringField('Patient Name (Toxicology)', validators=[Optional(), Length(max=255)])
    source = StringField('Source', validators=[Optional(), Length(max=255)])
    formulation_type = SelectField(
        'Formulation Type',
        choices=FORMULATION_TYPE_CHOICES,
        validators=[Optional()],
    )
    alcohol_type = SelectField(
        'Alcohol Type',
        choices=[
            ('', '-- Select Type --'),
            ('Alcohol Determination', 'Alcohol Determination'),
            ('Denatured Alcohol (bitrex)', 'Denatured Alcohol (bitrex)'),
            ('Alcohol Determination and Denatured', 'Alcohol Determination and Denatured'),
        ],
        validators=[Optional()]
    )
    claim_butt_number = StringField('Claim/Butt #', validators=[Optional(), Length(max=100)])
    milk_type = SelectField(
        'Milk Type',
        choices=[
            ('', '-- Select Type --'),
            ('R', 'Raw Milk'),
            ('P', 'Processed Milk'),
        ],
        validators=[Optional()]
    )
    expected_report_date = DateField('Expected Report Date', validators=[Optional()])
    scanned_file = FileField(
        'Replace Scanned Document',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'],
            'Only PDF, image, and Word document files allowed.'
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
        return Markup(''.join(html))


class SampleAssignForm(FlaskForm):
    chemist_ids = SelectMultipleField(
        'Assign to Chemist(s)',
        coerce=int,
        widget=CheckboxSelectMultiple(),
        validators=[DataRequired(message='Select at least one chemist.')],
    )
    # For sample types with predefined test names, use multi-select dropdown
    test_names = SelectMultipleField(
        'Test Name(s)',
        choices=[],
        validators=[Optional()],
    )
    # Fallback free-text field for sample types without predefined tests
    test_name = StringField('Test Name', validators=[Optional(), Length(max=255)])
    # For sample types with predefined references, use a dropdown
    test_reference_select = SelectField(
        'Test Reference',
        choices=[],
        validators=[Optional()],
    )
    # Fallback free-text reference
    test_reference = StringField('Test Reference', validators=[Optional(), Length(max=255)])
    expected_completion = DateField('Expected Completion Date', validators=[Optional()])
    comments = TextAreaField('Comments', validators=[Optional()])
    quantity_volume = StringField('Quantity / Volume', validators=[Optional(), Length(max=100)])
    submit = SubmitField('Assign Sample')

    def validate_expected_completion(self, field):
        if field.data:
            if field.data < date_today.today():
                raise ValidationError('Expected completion date cannot be in the past.')


# ---------------------------------------------------------------------------
# Report forms
# ---------------------------------------------------------------------------

class ReportSubmitForm(FlaskForm):
    report_text = TextAreaField('Report / Findings', validators=[DataRequired()])
    report_file = FileField(
        'Attach Report File',
        validators=[
            DataRequired(message='A report file is required before submitting.'),
            FileAllowed(
                ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'],
                'Only PDF, image, and Word document files allowed.'
            ),
        ],
    )
    all_samples_returned = SelectField(
        'All Samples Returned?',
        choices=[
            ('', '-- Select --'),
            ('Yes', 'Yes'),
            ('No', 'No'),
        ],
        validators=[Optional()],
    )
    return_quantity = StringField('Quantity Returned', validators=[Optional(), Length(max=100)])
    submit = SubmitField('Submit Report')


class PreliminaryReviewForm(FlaskForm):
    """Preliminary administrative / completeness review by Officer."""

    # -- Corrections --
    chk_original_entry_visible = BooleanField('Original entry remains visible')
    chk_no_correction_fluid = BooleanField('No correction fluid used')

    # -- Signatures and Review --
    chk_entries_signed = BooleanField('Entries signed/initialed by analyst')
    chk_date_recorded = BooleanField('Date of entry recorded')
    chk_supervisor_review = BooleanField('Supervisor review present')

    # -- Attachments --
    chk_printouts_attached = BooleanField('Printouts/graphs attached securely')
    chk_attachments_labeled = BooleanField('Attachments labeled and dated')
    chk_analyst_initials = BooleanField('Analyst initials across attachment')

    # -- General Documentation --
    chk_entries_contemporaneous = BooleanField('Entries made contemporaneously')
    chk_writing_legible = BooleanField('Writing clear and legible')
    chk_standard_abbreviations = BooleanField('Standard abbreviations used')

    action = SelectField(
        'Decision',
        choices=[
            ('', '-- Select Action --'),
            ('approved', 'Approve – Forward to Senior Chemist'),
            ('returned', 'Return for Correction'),
        ],
        validators=[DataRequired()],
    )
    review_comments = TextAreaField('Comments', validators=[Optional()])
    submit = SubmitField('Submit Review')

    # Ordered checklist items grouped by category for template rendering
    CHECKLIST_CATEGORIES = [
        ('Corrections', [
            'chk_original_entry_visible',
            'chk_no_correction_fluid',
        ]),
        ('Signatures and Review', [
            'chk_entries_signed',
            'chk_date_recorded',
            'chk_supervisor_review',
        ]),
        ('Attachments', [
            'chk_printouts_attached',
            'chk_attachments_labeled',
            'chk_analyst_initials',
        ]),
        ('General Documentation', [
            'chk_entries_contemporaneous',
            'chk_writing_legible',
            'chk_standard_abbreviations',
        ]),
    ]


class ReportReviewForm(FlaskForm):
    """Senior Chemist Review."""
    action = SelectField(
        'Decision',
        choices=[
            ('', '-- Select Action --'),
            ('accepted', 'Accept Report'),
            ('returned', 'Return for Correction'),
            ('rejected', 'Reject Report'),
        ],
        validators=[DataRequired()],
    )
    review_comments = TextAreaField('Comments', validators=[Optional()])
    submit = SubmitField('Submit Review')


class SubmitToDeputyForm(FlaskForm):
    """Senior Chemist submits report package to Deputy Government Chemist."""
    summary_report = TextAreaField(
        'Summary Report (required for Pharmaceutical samples)',
        validators=[Optional()],
    )
    summary_report_file = FileField(
        'Attach Summary Report File',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'],
            'Only PDF, image, and Word document files allowed.'
        )],
    )
    submit = SubmitField('Submit to Deputy Government Chemist')


class DeputyReviewForm(FlaskForm):
    """Deputy Government Chemist reviews submission."""
    action = SelectField(
        'Decision',
        choices=[
            ('', '-- Select Action --'),
            ('approved', 'Approve – Proceed to Certificate'),
            ('returned', 'Return to Senior Chemist'),
        ],
        validators=[DataRequired()],
    )
    review_comments = TextAreaField('Comments', validators=[Optional()])
    submit = SubmitField('Submit Review')


class CertificateForm(FlaskForm):
    """Deputy Government Chemist prepares Certificate of Analysis."""
    certificate_text = TextAreaField(
        'Certificate of Analysis', validators=[DataRequired()]
    )
    certificate_file = FileField(
        'Attach Certificate File',
        validators=[FileAllowed(
            ['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'doc', 'docx'],
            'Only PDF, image, and Word document files allowed.'
        )],
    )
    submit = SubmitField('Submit Certificate for HOD Review')


class HODReviewForm(FlaskForm):
    """Government Chemist (HOD) reviews and signs Certificate of Analysis."""
    action = SelectField(
        'Decision',
        choices=[
            ('', '-- Select Action --'),
            ('sign', 'Sign Certificate – Complete Process'),
            ('returned', 'Return to Deputy for Correction'),
        ],
        validators=[DataRequired()],
    )
    review_comments = TextAreaField('Comments', validators=[Optional()])
    submit = SubmitField('Submit Decision')
