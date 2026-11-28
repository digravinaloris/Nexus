"""HTTP server runner for the Nexus Flask application."""

import os


def run_web_server(app, environ=None):
    """Serve the Flask app with Waitress, retaining the development fallback."""
    environ = os.environ if environ is None else environ
    port = int(environ.get("PORT", 8080))
    try:
        # The Flask development server is not intended for production.
        from waitress import serve

        serve(app, host="0.0.0.0", port=port, threads=16)
    except Exception as error:  # Waitress missing or incompatible at startup.
        print(
            f"[WEB] waitress indisponible ({error!r}), "
            "repli sur le serveur de développement Flask",
            flush=True,
        )
        app.run(host="0.0.0.0", port=port)


def start_web_server_thread(target, thread_factory):
    """Start the web server target in a daemon thread and return the thread."""
    thread = thread_factory(target=target)
    thread.daemon = True
    thread.start()
    return thread
