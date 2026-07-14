from stock_analyzer.app import create_app
from stock_analyzer import config


app = create_app()


if __name__ == "__main__":
    app.run(
        host=str(config.SERVER_HOST),
        port=int(config.SERVER_PORT),
        debug=bool(config.SERVER_DEBUG),
        use_reloader=bool(config.SERVER_DEBUG) and bool(config.SERVER_USE_RELOADER),
    )
