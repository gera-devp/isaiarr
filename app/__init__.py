import os

from flask import Flask

from . import db
from .config import Config

__version__ = "3.11.0"
REPO_URL = "https://github.com/gera-devp/isaiarr"


def create_app(start_background=True):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()
    db.conn()
    from .web import bp
    app.register_blueprint(bp)
    app.jinja_env.globals["version"] = __version__
    app.jinja_env.globals["repo_url"] = REPO_URL
    if start_background:
        from . import watcher, worker
        worker.start()
        watcher.start()
        db.log(f"Isaiarr {__version__} started — library {Config.MUSIC_DIR}")
    return app
