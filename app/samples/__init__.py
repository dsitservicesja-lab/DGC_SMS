from flask import Blueprint

samples_bp = Blueprint('samples', __name__, template_folder='../templates/samples')

from app.samples import routes  # noqa: E402, F401
