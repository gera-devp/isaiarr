import logging

from waitress import serve

from app import create_app
from app.config import Config

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    logging.info("Isaiarr listening on :%s (music: %s)", Config.PORT, Config.MUSIC_DIR)
    serve(app, host="0.0.0.0", port=Config.PORT, threads=8)
