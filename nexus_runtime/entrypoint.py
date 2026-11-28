"""Runtime startup orchestration helpers."""


def start_runtime_services(keep_alive, signal_module, sigterm_handler):
    """Start the HTTP server before registering the graceful SIGTERM handler."""
    keep_alive()
    signal_module.signal(signal_module.SIGTERM, sigterm_handler)
