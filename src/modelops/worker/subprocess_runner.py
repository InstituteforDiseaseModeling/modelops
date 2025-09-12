#!/usr/bin/env python3
"""
Standalone subprocess runner for isolated execution.

- No dependency on ModelOps; JSON-RPC is inlined below.
- Executed by WarmProcessManager *with the venv's Python interpreter*.
- Communicates via JSON-RPC 2.0 over stdin/stdout using Content-Length framing.
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# -----------------------------------------------------------------------------
# Logging (stderr only; stdout is reserved for JSON-RPC)
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("runner")

# -----------------------------------------------------------------------------
# Minimal JSON-RPC 2.0 over stdio (LSP-style Content-Length framing)
# -----------------------------------------------------------------------------

class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

class JSONRPCProtocol:
    def __init__(self):
        # binary mode for exact byte lengths
        self._in = sys.stdin.buffer
        self._out = sys.stdout.buffer

    def _read_exactly(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            b = self._in.read(remaining)
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        data = b"".join(chunks)
        if len(data) != n:
            raise JSONRPCError(-32700, f"Incomplete message body ({len(data)}/{n} bytes)")
        return data

    def read_message(self) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        # Read headers until CRLF
        while True:
            line = self._in.readline()
            if not line:
                raise EOFError("stdin closed")
            if line == b"\r\n":
                break
            s = line.decode("utf-8").rstrip("\r\n")
            if ":" not in s:
                raise JSONRPCError(-32700, f"Invalid header: {s}")
            k, v = s.split(":", 1)
            headers[k.strip().lower()] = v.strip()

        if "content-length" not in headers:
            raise JSONRPCError(-32700, "Missing Content-Length header")
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise JSONRPCError(-32700, f"Invalid Content-Length: {headers['content-length']}")

        body = self._read_exactly(length)
        try:
            msg = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise JSONRPCError(-32700, f"Invalid JSON: {e}")
        if not isinstance(msg, dict):
            raise JSONRPCError(-32600, "Message must be an object")
        return msg

    def _write(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._out.write(f"Content-Length: {len(body)}\r\n".encode("ascii"))
        self._out.write(b"\r\n")
        self._out.write(body)
        self._out.flush()

    def send_response(self, req_id: Any, result: Any) -> None:
        if req_id is None:
            return  # notifications get no response
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def send_error(self, req_id: Any, code: int, message: str, data: Optional[Any] = None) -> None:
        if req_id is None:
            logger.error("JSON-RPC error (notification): %s (%s)", message, code)
            return
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

# -----------------------------------------------------------------------------
# Subprocess Runner
# -----------------------------------------------------------------------------

class SubprocessRunner:
    """Executes simulation tasks inside the venv interpreter."""

    def __init__(self, bundle_path: Path, venv_path: Path, bundle_digest: str):
        self.bundle_path = bundle_path
        self.venv_path = venv_path
        self.bundle_digest = bundle_digest
        self.wire_fn: Optional[Callable[[str, Dict[str, Any], int], Dict[str, bytes]]] = None
        self._setup()

    # ------------------------- setup & installs -------------------------

    def _deps_fingerprint(self) -> str:
        # Fingerprint dependency inputs (stable across filesystem path changes)
        h = hashlib.blake2b(digest_size=16)
        for name in ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml"):
            p = self.bundle_path / name
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    def _setup(self) -> None:
        logger.info("Setting up bundle %s", self.bundle_digest[:12])
        logger.info("Interpreter: %s", sys.executable)
        deps_marker = self.venv_path / ".deps_installed"
        wanted = self._deps_fingerprint()

        need_install = True
        if deps_marker.exists():
            have = deps_marker.read_text().strip()
            if have == wanted:
                need_install = False

        if need_install:
            self._install_dependencies()
            deps_marker.write_text(wanted)

        self.wire_fn = self._discover_wire_function()

    def _run(self, cmd: list[str]) -> None:
        logger.info("Running: %s", " ".join(cmd))
        env = {**os.environ, "PYTHONNOUSERSITE": "1"}  # keep user-site out
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.stdout:
            logger.debug(result.stdout)
        if result.returncode != 0:
            logger.error(result.stderr)
            raise RuntimeError(f"Install failed: {' '.join(cmd)}")

    def _install_dependencies(self) -> None:
        pyproject = self.bundle_path / "pyproject.toml"
        requirements = self.bundle_path / "requirements.txt"

        # Prefer uv if present (fast), else pip
        uv = shutil.which("uv")
        if pyproject.exists():
            if uv:
                self._run([uv, "pip", "install", "--python", sys.executable, str(self.bundle_path)])
            else:
                self._run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", str(self.bundle_path)])
        elif requirements.exists():
            if uv:
                self._run([uv, "pip", "install", "--python", sys.executable, "-r", str(requirements)])
            else:
                self._run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)])
            # Ensure bundle code itself is importable when there's no pyproject
            if str(self.bundle_path) not in sys.path:
                sys.path.insert(0, str(self.bundle_path))
        else:
            logger.warning("No dependency file found (pyproject.toml or requirements.txt)")

    # ------------------------- wire discovery --------------------------

    def _discover_wire_function(self) -> Callable:
        import importlib
        import importlib.metadata as im

        importlib.invalidate_caches()

        # Try entry points first (works naturally under venv interpreter)
        try:
            eps_list = list(im.entry_points().select(group="modelops.wire"))
        except Exception:
            eps = im.entry_points(group="modelops.wire")
            eps_list = list(eps) if eps else []

        if eps_list:
            if len(eps_list) > 1:
                names = [ep.name for ep in eps_list]
                raise RuntimeError(f"Multiple wire entry points found: {names}")
            ep = eps_list[0]
            logger.info("Using wire entry point: %s = %s", ep.name, ep.value)
            return ep.load()

        logger.info("No entry points found; trying manifest/convention fallback")
        return self._resolve_wire_fallback()

    def _resolve_wire_fallback(self) -> Callable:
        # 1) .modelops/manifest.json with {"wire": "module.or/path.py:func"}
        spec = None
        manifest = self.bundle_path / ".modelops" / "manifest.json"
        if manifest.exists():
            try:
                spec = json.loads(manifest.read_text()).get("wire")
            except Exception:
                spec = None

        # 2) .modelops/modelops.toml (optional; only if tomllib is available)
        if spec is None:
            toml_path = self.bundle_path / ".modelops" / "modelops.toml"
            if toml_path.exists():
                try:
                    import tomllib  # py>=3.11
                    spec = tomllib.loads(toml_path.read_text())["modelops"]["wire"]
                except Exception:
                    spec = None

        if spec:
            return self._load_wire_spec(spec)

        # 3) Conventional: bundle root wire.py:wire
        candidate = self.bundle_path / "wire.py"
        if candidate.exists():
            return self._load_wire_file(candidate, "wire")

        raise RuntimeError("No wire specified: install an entry point, provide .modelops manifest, or add wire.py")

    def _load_wire_spec(self, spec: str) -> Callable:
        if ":" not in spec:
            raise RuntimeError(f"Invalid wire spec '{spec}', expected 'module.or/path.py:func'")
        mod, func = spec.split(":", 1)
        if mod.endswith(".py") or "/" in mod:
            path = (self.bundle_path / mod).resolve() if not mod.startswith("/") else Path(mod)
            return self._load_wire_file(path, func)
        # dotted module
        if str(self.bundle_path) not in sys.path:
            sys.path.insert(0, str(self.bundle_path))
        import importlib
        importlib.invalidate_caches()
        m = importlib.import_module(mod)
        f = getattr(m, func, None)
        if not callable(f):
            raise RuntimeError(f"{spec} is not callable")
        return f

    def _load_wire_file(self, path: Path, func: str) -> Callable:
        import importlib.util
        if not path.exists():
            raise RuntimeError(f"Wire file not found: {path}")
        spec = importlib.util.spec_from_file_location("bundle_wire", str(path))
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(module)                 # type: ignore[assignment]
        f = getattr(module, func, None)
        if not callable(f):
            raise RuntimeError(f"{path}:{func} is not callable")
        return f

    # ------------------------- RPC methods -----------------------------

    def ready(self) -> Dict[str, Any]:
        return {
            "ready": True,
            "bundle_digest": self.bundle_digest,
            "python": sys.executable,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "pid": os.getpid(),
            "venv": str(self.venv_path),
        }

    def execute(
        self,
        entrypoint: str,
        params: Dict[str, Any],
        seed: int,
        bundle_digest: Optional[str] = None,
    ) -> Dict[str, str]:
        if bundle_digest and bundle_digest != self.bundle_digest:
            raise ValueError(f"Bundle digest mismatch: expected {self.bundle_digest}, got {bundle_digest}")

        logger.info("Executing %s (seed=%s)", entrypoint, seed)
        try:
            result_bytes = self.wire_fn(entrypoint, params, seed)  # type: ignore[misc]
            artifacts: Dict[str, str] = {}
            for name, data in result_bytes.items():
                if not isinstance(data, (bytes, bytearray)):
                    logger.warning("Converting non-bytes result for %s", name)
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    else:
                        data = json.dumps(data).encode("utf-8")
                artifacts[name] = base64.b64encode(bytes(data)).decode("ascii")
            return artifacts
        except Exception as e:
            logger.exception("Execution failed")
            err = json.dumps({"error": str(e), "type": type(e).__name__, "entrypoint": entrypoint}).encode("utf-8")
            return {"error": base64.b64encode(err).decode("ascii")}

# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone subprocess runner")
    parser.add_argument("--bundle-path", required=True, help="Path to bundle")
    parser.add_argument("--venv-path", required=True, help="Path to venv")
    parser.add_argument("--bundle-digest", required=True, help="Bundle digest")
    args = parser.parse_args()

    try:
        runner = SubprocessRunner(
            bundle_path=Path(args.bundle_path),
            venv_path=Path(args.venv_path),
            bundle_digest=args.bundle_digest,
        )

        rpc = JSONRPCProtocol()
        logger.info("JSON-RPC server started")

        while True:
            req_id = None
            try:
                msg = rpc.read_message()
                method = msg.get("method")
                params = msg.get("params", {}) or {}
                req_id = msg.get("id")

                if method == "ready":
                    rpc.send_response(req_id, runner.ready())
                elif method == "execute":
                    if not isinstance(params, dict):
                        raise JSONRPCError(-32602, "Invalid params (expected object)")
                    rpc.send_response(req_id, runner.execute(**params))
                elif method == "shutdown":
                    rpc.send_response(req_id, {"ok": True})
                    logger.info("Shutdown requested")
                    break
                else:
                    rpc.send_error(req_id, -32601, f"Method not found: {method}")

            except EOFError:
                logger.info("stdin closed; exiting")
                break
            except JSONRPCError as e:
                rpc.send_error(req_id, e.code, e.message, e.data)
            except Exception as e:
                logger.exception("Unhandled error in server loop")
                rpc.send_error(req_id, -32603, "Internal error", str(e))

    except Exception:
        logger.exception("Failed to initialize runner")
        sys.exit(1)

if __name__ == "__main__":
    main()
