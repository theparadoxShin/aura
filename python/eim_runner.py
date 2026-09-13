"""
Minimal Edge Impulse .eim model runner.

An .eim file is a self-contained executable: it is started with the path of a
Unix socket as its only argument, and then speaks newline-free JSON over that
socket - a {"hello": 1} handshake that returns the model parameters, then
{"classify": [features]} requests that return per-label scores.

The official `edge-impulse-linux` SDK does the same thing but drags in audio and
vision dependencies AURA does not need, so this runner reimplements just the
classification path with the standard library. Nothing here is AURA-specific.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time


class EimError(Exception):
    pass


class EimRunner:
    """Owns the model process and its socket. Not thread-safe: call from one thread."""

    def __init__(self, model_path):
        self._proc = None
        self._sock = None
        self._msg_id = 0
        self.model_path = self._ensure_executable(model_path)
        self._socket_path = os.path.join(
            tempfile.gettempdir(), f"aura-eim-{os.getpid()}.sock"
        )
        self._start()
        self.params = self._hello()

    @staticmethod
    def _ensure_executable(path):
        """
        Make sure the model can be executed.

        The app folder may be mounted without the exec bit honoured, so if setting
        it there is not enough the model is copied to the temp directory, which is
        always executable.
        """
        if not os.path.isfile(path):
            raise EimError(f"model not found: {path}")
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
            if os.access(path, os.X_OK):
                return path
        except OSError:
            pass
        fallback = os.path.join(tempfile.gettempdir(), os.path.basename(path))
        shutil.copy2(path, fallback)
        os.chmod(fallback, 0o755)
        return fallback

    def _start(self):
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        try:
            self._proc = subprocess.Popen(
                [self.model_path, self._socket_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:  # wrong architecture, missing loader, ...
            raise EimError(f"cannot execute model: {error}")

        deadline = time.time() + 10.0
        while not os.path.exists(self._socket_path):
            if self._proc.poll() is not None:
                raise EimError(f"model exited at start (code {self._proc.returncode})")
            if time.time() > deadline:
                self.close()
                raise EimError("model did not open its socket within 10 s")
            time.sleep(0.05)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(10.0)
        self._sock.connect(self._socket_path)

    def _request(self, payload):
        self._msg_id += 1
        payload = {**payload, "id": self._msg_id}
        self._sock.sendall(json.dumps(payload).encode("utf-8"))
        # Responses are a single JSON document, possibly terminated by NUL bytes:
        # accumulate until the buffer parses.
        buffer = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise EimError("model closed the socket")
            buffer += chunk
            text = buffer.replace(b"\x00", b"").strip()
            if not text:
                continue
            try:
                reply = json.loads(text.decode("utf-8"))
            except ValueError:
                continue  # incomplete: keep reading
            if not reply.get("success", False):
                raise EimError(str(reply.get("error", "model returned an error")))
            return reply

    def _hello(self):
        reply = self._request({"hello": 1})
        params = reply.get("model_parameters", {})
        if not params.get("labels"):
            raise EimError("model reported no labels")
        return params

    @property
    def labels(self):
        return list(self.params.get("labels", []))

    @property
    def axis_count(self):
        return int(self.params.get("axis_count", 0) or 0)

    @property
    def window_sample_count(self):
        axes = self.axis_count
        total = int(self.params.get("input_features_count", 0) or 0)
        return total // axes if axes else 0

    def classify(self, features):
        """features: flat list, samples interleaved by axis, oldest sample first."""
        expected = int(self.params.get("input_features_count", 0) or 0)
        if expected and len(features) != expected:
            raise EimError(f"expected {expected} features, got {len(features)}")
        reply = self._request({"classify": [float(f) for f in features]})
        return dict(reply.get("result", {}).get("classification", {}))

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass
