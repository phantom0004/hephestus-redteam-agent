"""Own the optional remote connection and its short-lived private credentials."""

import atexit
from io import BytesIO
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from hephestus import config
from hephestus.relay.app import create_relay_app
from hephestus.relay.secrets import load_key_file, write_secret_bundle, _harden_acl


def read_tunnel_url(process, timeout=25):
    lines = queue.Queue(maxsize=128)
    done = threading.Event()

    def collect():
        for line in process.stdout:
            if not done.is_set():
                try:
                    lines.put_nowait(line)
                except queue.Full:
                    pass

    threading.Thread(target=collect, daemon=True).start()
    deadline = time.monotonic() + timeout
    public_url = None
    registered = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b', line)
            if match:
                public_url = match.group(0)
            if 'Registered tunnel connection' in line:
                registered = True
            if public_url and registered:
                return public_url
        raise RuntimeError('Could not establish the temporary HTTPS tunnel. Check cloudflared, DNS and outbound TCP/443 access, then retry Remote.')
    finally:
        done.set()


class RemoteSession:
    def __init__(self, app):
        self.app = app
        self.directory = None
        self.tunnel = None
        self.server = None
        self.thread = None
        self.store = None
        self.public_url = None
        self._old_config = None
        atexit.register(self.close)

    def _create_credentials(self):
        # Use the OS private temporary area; never put credentials in a source archive.
        self.directory = Path(tempfile.mkdtemp(prefix='hephestus-remote-'))
        _harden_acl(self.directory)
        write_secret_bundle(self.directory / 'worker.key', self.directory / 'relay-auth.key')

    def start(self):
        if self.ready():
            return
        self.close()
        if config.COMMAND_TIMEOUT_SECONDS >= config.RELAY_JOB_TTL_SECONDS:
            raise RuntimeError(f'Remote command timeout must be below {config.RELAY_JOB_TTL_SECONDS}s. Reduce it in Settings before selecting Remote.')
        cloudflared = shutil.which('cloudflared')
        if not cloudflared:
            raise RuntimeError('Remote mode requires cloudflared on PATH. Install it using the README, restart Hephestus, then select Remote again. Local mode needs no tunnel.')
        try:
            from hephestus.relay.server import BoundedRelayServer
            self._create_credentials()
            relay_app = create_relay_app(load_key_file(self.directory / 'relay-auth.key'))
            self.store = relay_app.extensions['hephestus_relay_store']
            # Allocate a free loopback port; no fixed-port collision or LAN listener.
            self.server = BoundedRelayServer('127.0.0.1', 0, relay_app)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            relay_url = f'http://127.0.0.1:{self.server.server_port}'
            self.tunnel = subprocess.Popen(
                [cloudflared, 'tunnel', '--protocol', 'http2', '--url', relay_url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.public_url = read_tunnel_url(self.tunnel)
            self._old_config = (config.RELAY_URL, config.RELAY_MASTER_KEY_PATH)
            config.RELAY_URL = relay_url
            config.RELAY_MASTER_KEY_PATH = str(self.directory / 'worker.key')
            self.app.extensions['relay_store'] = self.store
        except BaseException:
            self.close()
            raise

    def ready(self):
        return bool(self.public_url and self.tunnel and self.tunnel.poll() is None)

    def status(self):
        return dict(mode='remote' if config.BACKEND == 'relay' else 'local',
                    ready=self.ready(), url=self.public_url if self.ready() else None,
                    worker_online=bool(self.ready() and self.store and self.store.worker_online()))

    def bundle(self):
        if not self.ready() or self.directory is None:
            raise RuntimeError('Select Remote to create a new connection first.')
        stream = BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('worker.key', (self.directory / 'worker.key').read_bytes())
            worker = Path(__file__).resolve().parents[1] / 'worker' / 'remote_handler.ps1'
            archive.writestr('remote_handler.ps1', worker.read_bytes())
            archive.writestr('Connect-Hephestus.ps1',
                '$ErrorActionPreference = "Stop"\r\n'
                f'& (Join-Path $PSScriptRoot "remote_handler.ps1") -RelayUrl "{self.public_url}" '
                '-SecretPath (Join-Path $PSScriptRoot "worker.key") '
                f'-CommandTimeoutSeconds {config.COMMAND_TIMEOUT_SECONDS} '
                f'-MaxOutputChars {config.OUTPUT_MAX_CHARS}\r\n')
            archive.writestr('README.txt',
                'PRIVATE CONNECTION BUNDLE - do not commit or share publicly.\r\n'
                '1. Transfer this ZIP securely to the authorized Windows target.\r\n'
                '2. Extract ALL files into one private folder.\r\n'
                '3. In PowerShell run .\\Connect-Hephestus.ps1; or open that file in ISE and press F5.\r\n'
                '4. Keep it running. Review each full proposal: Y approve, N reject, U edit, Q quit.\r\n'
                '5. Return to the controller and start a task when the worker shows connected.\r\n'
                '6. When finished, select Local on the controller and delete this ZIP and extracted folder.\r\n'
                'The bundle expires when this remote connection ends. A new connection needs a new bundle.\r\n'
                'Follow your organization\'s script execution policy; no administrator privileges are required.\r\n')
        return stream.getvalue()

    def close(self):
        # Each stage runs even if another resource has already vanished or fails.
        errors = []
        def attempt(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        if self.store:
            attempt(self.store.disconnect)
        if self.tunnel:
            process = self.tunnel
            def stop_tunnel():
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        # Termination can race with process exit. Escalate if necessary.
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)
            attempt(stop_tunnel)
            if process.stdout:
                attempt(process.stdout.close)
        if self.server:
            if self.thread and self.thread.is_alive():
                attempt(self.server.shutdown)
            attempt(self.server.server_close)
        if self.thread:
            attempt(lambda: self.thread.join(timeout=3))
        if self.app.extensions.get('relay_store') is self.store:
            self.app.extensions.pop('relay_store', None)
        if self._old_config:
            config.RELAY_URL, config.RELAY_MASTER_KEY_PATH = self._old_config
            config.BACKEND = 'local'
        if self.directory:
            directory = self.directory.resolve()
            def remove_credentials():
                if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith('hephestus-remote-'):
                    raise RuntimeError('Refusing to remove a directory outside the managed credential area')
                if directory.exists():
                    shutil.rmtree(directory)
            attempt(remove_credentials)
        self.directory = self.tunnel = self.server = self.thread = self.store = None
        self.public_url = self._old_config = None
        if errors:
            raise RuntimeError('Remote cleanup could not finish. Check the controller terminal and remove leftover private credentials from the OS temporary directory.') from errors[0]
