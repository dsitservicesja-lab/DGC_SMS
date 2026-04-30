import logging
import os
from dotenv import load_dotenv

# python-dotenv 1.0.x emits 'could not parse statement' messages via
# logger.warning() in dotenv.main — NOT via the warnings module.
# Raise the dotenv logger threshold to ERROR so these non-fatal parse
# notices are silenced while genuine errors (encoding failures, etc.)
# remain visible.
logging.getLogger('dotenv.main').setLevel(logging.ERROR)

# Load .env before any config or app code reads os.environ, so SECRET_KEY
# and other settings are always available even when started outside systemd.
load_dotenv()

from app import create_app

app = create_app(os.environ.get('FLASK_CONFIG', 'development'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
