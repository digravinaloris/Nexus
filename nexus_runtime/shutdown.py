"""Best-effort cleanup helpers for Nexus runtime shutdown."""


def close_mongo_connection(client):
    """Close the optional Mongo client without masking shutdown errors."""
    if client is None:
        return
    try:
        client.close()
        print("[SHUTDOWN] MongoDB connection closed.", flush=True)
    except Exception:
        # Preserve the existing best-effort shutdown behavior.
        pass
