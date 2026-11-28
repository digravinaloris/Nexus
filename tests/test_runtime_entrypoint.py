"""Tests for runtime entrypoint orchestration."""

from nexus_runtime.entrypoint import start_runtime_services


def test_start_runtime_services_starts_server_before_registering_signal():
    calls = []

    class SignalModule:
        SIGTERM = object()

        @staticmethod
        def signal(signum, handler):
            calls.append(("signal", signum, handler))

    handler = object()

    def start_server():
        calls.append(("server",))

    start_runtime_services(start_server, SignalModule, handler)

    assert calls == [("server",), ("signal", SignalModule.SIGTERM, handler)]
