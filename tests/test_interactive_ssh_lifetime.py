"""Real loopback SSH and event-controlled acquisition/relay ownership tests."""

import asyncio
import importlib.util
import socket
from types import SimpleNamespace

import asyncssh
import pytest

from proxbox_api.services import ssh_terminal as terminal
from proxbox_api.services.interactive_policy import ExecutionPolicy, InteractiveRuntime
from proxbox_api.ssrf import ResolvedAddress


class TerminalSocket:
    def __init__(self):
        self.messages = []
        self.incoming = asyncio.Queue()
        self.ready = asyncio.Event()

    async def send_json(self, message):
        self.messages.append(message)
        if message["type"] == "ready":
            self.ready.set()

    async def receive_json(self):
        return await self.incoming.get()


async def _run(runtime, socket, credential):
    manager = terminal.TerminalSessionManager()
    async with runtime.admission():
        session, _ = await manager.create_session(
            target_type="endpoint",
            endpoint_id=1,
            node_id=None,
            host="127.0.0.1",
            actor="synthetic",
            cols=80,
            rows=24,
        )
        try:
            await terminal.connect_and_relay(socket, session, credential, manager=manager)
        finally:
            await manager.release(session.session_id)


class PasswordServer(asyncssh.SSHServer):
    def __init__(self, auth_events=None):
        self.auth_events = auth_events if auth_events is not None else []

    def begin_auth(self, username):
        self.auth_events.append(("begin_auth", username))
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        self.auth_events.append(("password", username, password))
        return username == "synthetic" and password == "test-only-password"

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        self.auth_events.append(("public_key", username, key.get_fingerprint("sha256")))
        return False


async def test_real_loopback_mismatch_sends_no_auth_material_despite_ambient_trust(
    monkeypatch,
    tmp_path,
):
    """A trusted ambient key cannot bypass the same-connection fingerprint pin."""

    async def allow_loopback_fixture(_host):
        return (ResolvedAddress(socket.AF_INET, "127.0.0.1"),)

    monkeypatch.setattr(terminal, "ensure_ssh_host_allowed", allow_loopback_fixture)
    server_key = asyncssh.generate_private_key("ssh-ed25519")
    pinned_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    auth_events = []

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: PasswordServer(auth_events),
        server_host_keys=[server_key],
    )
    try:
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        public_key = server_key.export_public_key().decode().strip()
        (ssh_dir / "known_hosts").write_text(
            f"[localhost]:{server.get_port()} {public_key}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        credential = terminal.TerminalCredential(
            "endpoint",
            1,
            "localhost",
            server.get_port(),
            "synthetic",
            pinned_key.get_fingerprint("sha256"),
            password="test-only-password",
            private_key=client_key.export_private_key().decode(),
        )
        runtime = InteractiveRuntime(ExecutionPolicy("legacy", "local-test"))
        with pytest.raises(terminal.TerminalCredentialError, match="does not match"):
            await asyncio.wait_for(_run(runtime, TerminalSocket(), credential), 10)
        assert auth_events == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(
    importlib.util.find_spec("OpenSSL") is None,
    reason="PyOpenSSL is required for AsyncSSH X.509 host-certificate support",
)
async def test_real_x509_ambient_ca_cannot_receive_authentication(monkeypatch, tmp_path):
    """An ambient X.509 CA cannot bypass the request's raw-key fingerprint pin."""

    async def allow_loopback_fixture(_host):
        return (ResolvedAddress(socket.AF_INET, "127.0.0.1"),)

    monkeypatch.setattr(terminal, "ensure_ssh_host_allowed", allow_loopback_fixture)
    ca_key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    server_key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    pinned_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    ca_cert = ca_key.generate_x509_ca_certificate(ca_key, "CN=Proxbox Test CA")
    host_cert = ca_key.generate_x509_host_certificate(
        server_key,
        "CN=localhost",
        principals=["localhost"],
    )
    auth_events = []

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: PasswordServer(auth_events),
        server_host_keys=[(server_key, host_cert)],
    )
    try:
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "ca-bundle.crt").write_bytes(ca_cert.export_certificate("pem"))
        monkeypatch.setenv("HOME", str(tmp_path))
        credential = terminal.TerminalCredential(
            "endpoint",
            1,
            "localhost",
            server.get_port(),
            "synthetic",
            pinned_key.get_fingerprint("sha256"),
            password="test-only-password",
            private_key=client_key.export_private_key().decode(),
        )
        runtime = InteractiveRuntime(ExecutionPolicy("legacy", "local-test"))

        with pytest.raises(terminal.TerminalCredentialError, match="does not match"):
            await asyncio.wait_for(_run(runtime, TerminalSocket(), credential), 10)

        assert auth_events == []
    finally:
        server.close()
        await server.wait_closed()


async def test_real_loopback_matching_pin_authenticates_and_opens_pty(monkeypatch):
    async def allow_loopback_fixture(_host):
        return (ResolvedAddress(socket.AF_INET, "127.0.0.1"),)

    monkeypatch.setattr(terminal, "ensure_ssh_host_allowed", allow_loopback_fixture)
    key = asyncssh.generate_private_key("ssh-ed25519")
    auth_events = []
    opened = []

    async def shell(process):
        opened.append(process)
        try:
            line = await process.stdin.readline()
            process.stdout.write("echo:" + line)
            process.exit(0)
        except asyncssh.SignalReceived:
            process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: PasswordServer(auth_events),
        server_host_keys=[key],
        process_factory=shell,
    )
    try:
        credential = terminal.TerminalCredential(
            "endpoint",
            1,
            "localhost",
            server.get_port(),
            "synthetic",
            key.get_fingerprint("sha256"),
            password="test-only-password",
        )
        runtime = InteractiveRuntime(ExecutionPolicy("legacy", "local-test"))
        terminal_socket = TerminalSocket()
        task = asyncio.create_task(_run(runtime, terminal_socket, credential))
        await asyncio.wait_for(terminal_socket.ready.wait(), 10)
        terminal_socket.incoming.put_nowait({"type": "input", "data": "synthetic-input\n"})
        await asyncio.wait_for(task, 10)
        assert ("password", "synthetic", "test-only-password") in auth_events
        assert opened
        assert any(
            "echo:synthetic-input" in frame.get("data", "") for frame in terminal_socket.messages
        )
        assert terminal_socket.messages[-1] == {"type": "exit", "status": 0}
        assert runtime.status()["active"] == 0
        assert runtime.status()["cleanup_active"] == 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("boundary", ["connect", "pty"])
async def test_quiesce_owns_blocked_connect_and_pty_without_delivery(monkeypatch, boundary):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    key = asyncssh.generate_private_key("ssh-ed25519")

    class Process:
        def terminate(self):
            calls.append("terminate")

        async def wait(self):
            calls.append("wait")
            return SimpleNamespace(exit_status=-1)

    class Connection:
        def get_server_host_key(self):
            return key

        async def create_process(self, **kwargs):
            calls.append("pty")
            started.set()
            await release.wait()
            return Process()

        def close(self):
            calls.append("close")

        async def wait_closed(self):
            calls.append("wait-closed")

    async def connect(credential):
        calls.append("connect")
        if boundary == "connect":
            started.set()
            await release.wait()
        return Connection()

    monkeypatch.setattr(terminal, "_connect_terminal", connect)
    credential = terminal.TerminalCredential(
        "endpoint",
        1,
        "127.0.0.1",
        22,
        "synthetic",
        key.get_fingerprint("sha256"),
        password="test-only-password",
    )
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "local-test"))
    socket = TerminalSocket()
    task = asyncio.create_task(_run(runtime, socket, credential))
    await started.wait()
    quiesce = asyncio.create_task(runtime.quiesce())
    await asyncio.sleep(0)
    release.set()
    await quiesce
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.messages == []
    assert calls.count("close") == 1
    assert calls.count("wait-closed") == 1
    assert calls.count("pty") == (1 if boundary == "pty" else 0)
    if boundary == "pty":
        assert calls.count("terminate") == calls.count("wait") == 1
        assert runtime.status()["remote_outcome_unknown"] is True


async def test_local_close_reports_unknown_exit_and_sticky_uncertainty(monkeypatch):
    key = asyncssh.generate_private_key("ssh-ed25519")

    class Process:
        stdout = SimpleNamespace(read=lambda _length: b"")
        stdin = SimpleNamespace(write=lambda _value: None)

        def terminate(self):
            return None

        async def wait(self):
            return SimpleNamespace(exit_status=-1)

    class Connection:
        def get_server_host_key(self):
            return key

        async def create_process(self, **kwargs):
            return Process()

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def connect(_credential):
        return Connection()

    async def relay(*_args):
        return None

    monkeypatch.setattr(terminal, "_connect_terminal", connect)
    monkeypatch.setattr(terminal, "_relay_terminal", relay)
    credential = terminal.TerminalCredential(
        "endpoint",
        1,
        "127.0.0.1",
        22,
        "synthetic",
        key.get_fingerprint("sha256"),
        password="test-only-password",
    )
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "local-test"))
    socket = TerminalSocket()

    await _run(runtime, socket, credential)

    assert socket.messages[-1] == {"type": "exit", "status": None}
    assert runtime.status()["remote_outcome_unknown"] is True


async def test_quiesce_prevents_stdin_and_resize_after_input_wait(monkeypatch):
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    calls = []
    process = SimpleNamespace(stdin=SimpleNamespace(write=calls.append))
    socket = TerminalSocket()
    session = SimpleNamespace(cols=80, rows=24)
    async with runtime.admission():
        runtime.quiescing = True
        for message in ({"type": "input", "data": "forbidden"}, {"type": "resize", "cols": 120}):
            from proxbox_api.services.interactive_policy import InteractiveDenied

            with pytest.raises(InteractiveDenied):
                await terminal._handle_terminal_message(socket, process, session, message)
    assert calls == []


async def test_transport_pin_is_rechecked_before_pty_even_without_library_callback(monkeypatch):
    key = asyncssh.generate_private_key("ssh-ed25519")
    calls = []

    class Connection:
        def get_server_host_key(self):
            return key

        async def create_process(self, **kwargs):
            pytest.fail("An unverified transport reached PTY creation")

        def close(self):
            calls.append("close")

        async def wait_closed(self):
            calls.append("wait-closed")

    async def connect(credential):
        return Connection()

    monkeypatch.setattr(terminal, "_connect_terminal", connect)
    credential = terminal.TerminalCredential(
        "endpoint", 1, "synthetic.invalid", 22, "synthetic", "SHA256:wrong", password="synthetic"
    )
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    with pytest.raises(terminal.TerminalCredentialError):
        await _run(runtime, TerminalSocket(), credential)
    assert calls == ["close", "wait-closed"]


async def test_quiesce_after_stdout_wait_prevents_output_forwarding():
    from proxbox_api.services.interactive_policy import InteractiveDenied

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    socket = TerminalSocket()

    class Output:
        delivered = False

        async def read(self, length):
            if self.delivered:
                return b""
            self.delivered = True
            runtime.quiescing = True
            return b"synthetic-private-output"

    class Manager:
        async def mark_activity(self, session_id):
            pass

    async with runtime.admission():
        with pytest.raises(InteractiveDenied):
            await terminal._pump_process_output(
                socket,
                SimpleNamespace(stdout=Output()),
                SimpleNamespace(session_id="test"),
                Manager(),
            )
    assert socket.messages == []


async def test_stored_material_requires_netbox_but_inline_material_does_not(monkeypatch):
    async with InteractiveRuntime(ExecutionPolicy("legacy", "test")).admission():
        with pytest.raises(terminal.TerminalCredentialError, match="require NetBox configuration"):
            await terminal.fetch_terminal_credential(
                None, SimpleNamespace(one_shot_credential=None)
            )
