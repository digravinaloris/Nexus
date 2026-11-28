"""Process signal handlers for graceful Nexus shutdown."""


def handle_sigterm(signum, frame, logger, interrupt=KeyboardInterrupt):
    """Log SIGTERM and raise KeyboardInterrupt for normal bot cleanup."""
    print("[SHUTDOWN] Received SIGTERM, shutting down gracefully...", flush=True)
    logger("info", "shutdown_sigterm")
    raise interrupt()
