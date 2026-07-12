import os

from stock_analyzer.app import create_app


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("DEBUG", "0") == "1"
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=debug,
        use_reloader=debug and os.getenv("FLASK_USE_RELOADER", "0") == "1",
    )
