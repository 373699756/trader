import os

from stock_analyzer.app import create_app
from stock_analyzer import config


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST") or os.getenv("FLASK_RUN_HOST") or str(config.SERVER_HOST)
    port_raw = os.getenv("PORT") or os.getenv("FLASK_RUN_PORT") or str(config.SERVER_PORT)
    port = int(port_raw)

    app.run(
        host=host,
        port=port,
        debug=bool(config.SERVER_DEBUG),
        use_reloader=bool(config.SERVER_DEBUG) and bool(config.SERVER_USE_RELOADER),
    )
