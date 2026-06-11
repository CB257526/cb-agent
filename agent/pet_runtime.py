"""Live2D desktop pet sidecar for cb-agent.

The runtime keeps the useful BongoCat idea without embedding the whole Tauri
app: a tiny Python process owns the floating WebView window, Pixi/easy-live2d
renders Cubism models, and pynput forwards global mouse/keyboard events.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import mimetypes
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse

import webview
from pynput import keyboard, mouse

try:  # BongoCat uses JSON5 for model3 files; keep plain json as a fallback.
    import json5  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency guard
    json5 = None  # type: ignore[assignment]


MAX_MOUSE_FPS = 60
WINDOWS_KEY_AUTO_RELEASE_MS = 3000
DEFAULT_WINDOW_WIDTH = 420
DEFAULT_WINDOW_HEIGHT = 420
DEFAULT_MAX_WINDOW_EDGE = 420
MAX_INITIAL_MODEL_LOAD_SECONDS = 15
DEFAULT_WINDOW_X = 80
DEFAULT_WINDOW_Y = 80
MIN_WINDOW_EDGE = 120
MAX_WINDOW_EDGE = 1600
WHEEL_SIZE_STEP = 40


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            if json5 is None:
                return None
            value = json5.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _norm_rel(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _repair_mojibake(value: str) -> str:
    candidates = [value]
    for encoding in ("gbk", "cp936"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except Exception:
            continue
        if repaired not in candidates:
            candidates.append(repaired)
    return " ".join(candidates)


def _looks_like_transparency_control(identifier: str, name: str) -> bool:
    haystack = _repair_mojibake(f"{identifier} {name}").lower()
    return any(
        token in haystack
        for token in (
            "透明",
            "透出",
            "背景",
            "桌子",
            "桌面",
            "底板",
            "background",
            "transparent",
            "transparency",
            "table",
            "desk",
        )
    )


def _looks_like_opaque_background_part(identifier: str, name: str) -> bool:
    haystack = _repair_mojibake(f"{identifier} {name}").lower()
    return any(
        token in haystack
        for token in (
            "不透明",
            "白底",
            "底板",
            "opaque",
            "solid background",
        )
    )


def _url_for(base_url: str, rel: str, version: str) -> str:
    parts = [quote(part) for part in _norm_rel(rel).split("/") if part]
    return f"{base_url}/{'/'.join(parts)}?v={quote(version)}"


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _apply_layered_color_key(hwnd: int, red: int = 240, green: int = 240, blue: int = 240) -> None:
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        gwl_exstyle = -20
        ws_ex_layered = 0x00080000
        lwa_colorkey = 0x00000001
        colorref = int(red) | (int(green) << 8) | (int(blue) << 16)
        style = user32.GetWindowLongW(hwnd, gwl_exstyle)
        user32.SetWindowLongW(hwnd, gwl_exstyle, style | ws_ex_layered)
        user32.SetLayeredWindowAttributes(hwnd, colorref, 255, lwa_colorkey)
    except Exception:
        pass


def _monitor_rect(x: int, y: int) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    try:
        point = POINT(int(x), int(y))
        monitor = ctypes.windll.user32.MonitorFromPoint(point, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        pass

    try:
        width = int(ctypes.windll.user32.GetSystemMetrics(0))
        height = int(ctypes.windll.user32.GetSystemMetrics(1))
    except Exception:
        width, height = 1920, 1080
    return (0, 0, width, height)


def _cursor_position() -> tuple[int, int]:
    if sys.platform != "win32":
        return (0, 0)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return (int(point.x), int(point.y))
    except Exception:
        pass
    return (0, 0)


class _AssetRequestHandler(BaseHTTPRequestHandler):
    server: "_AssetServer"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        rel = unquote(parsed.path).lstrip("/")
        if not rel or rel == "health":
            payload = b"ok"
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if rel.startswith("__cbagent_web__/"):
            root = self.server.web_root
            rel = rel.removeprefix("__cbagent_web__/")
        else:
            root = self.server.asset_root

        if root is None:
            self.send_error(404)
            return

        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except Exception:
            self.send_error(403)
            return

        if not target.is_file():
            self.send_error(404)
            return

        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            self.server.request_count += 1
            self.server.recent_requests.append(rel)
            self.server.recent_requests = self.server.recent_requests[-20:]
            size = target.stat().st_size
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with target.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except BrokenPipeError:
            return

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _AssetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _AssetRequestHandler)
        self.web_root: Optional[Path] = None
        self.asset_root: Optional[Path] = None
        self.request_count = 0
        self.recent_requests: list[str] = []


def _key_name(key: Any) -> Optional[str]:
    char = getattr(key, "char", None)
    if char:
        if char.isalpha():
            return f"Key{char.upper()}"
        if char.isdigit():
            return f"Num{char}"
        if char == " ":
            return "Space"
        if char == "`":
            return "BackQuote"
        if char == "/":
            return "Slash"

    mapping = {
        keyboard.Key.space: "Space",
        keyboard.Key.enter: "Return",
        keyboard.Key.tab: "Tab",
        keyboard.Key.esc: "Escape",
        keyboard.Key.backspace: "Backspace",
        keyboard.Key.delete: "Delete",
        keyboard.Key.caps_lock: "CapsLock",
        keyboard.Key.shift: "Shift",
        keyboard.Key.shift_l: "Shift",
        keyboard.Key.shift_r: "Shift",
        keyboard.Key.ctrl: "Control",
        keyboard.Key.ctrl_l: "Control",
        keyboard.Key.ctrl_r: "Control",
        keyboard.Key.alt: "Alt",
        keyboard.Key.alt_l: "Alt",
        keyboard.Key.alt_r: "Alt",
        keyboard.Key.cmd: "Meta",
        keyboard.Key.cmd_l: "Meta",
        keyboard.Key.cmd_r: "Meta",
    }
    if key in mapping:
        return mapping[key]
    name = getattr(key, "name", "")
    if name and name.startswith("f") and name[1:].isdigit():
        return "Fn"
    return None


class PetRuntimeApi:
    def __init__(self, runtime: "PetRuntime") -> None:
        self.runtime = runtime

    def petWheel(self, delta_y: Any) -> bool:
        self.runtime.resize_from_wheel_delta(delta_y, source="dom")
        return True


class PetRuntime:
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent.parent
        self.web_dir = Path(__file__).resolve().parent / "pet_web"
        self.window: Any = None
        self.visible = True
        self.ready = threading.Event()
        self.last_mouse_emit = 0.0
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None
        self.asset_server: Optional[_AssetServer] = None
        self.asset_thread: Optional[threading.Thread] = None
        self.asset_version = "0"
        self._closed = False
        self.max_size = self._initial_max_size()
        self.model_size: Optional[tuple[float, float]] = None
        self.api = PetRuntimeApi(self)

    def _log(self, message: str) -> None:
        print(f"pet runtime: {message}", file=sys.stderr, flush=True)

    def _state_path(self) -> Path:
        return Path.home() / ".cbagent" / "pet" / "state.json"

    def _read_runtime_state(self) -> Dict[str, Any]:
        return _read_json(self._state_path()) or {}

    def _initial_max_size(self) -> int:
        env_value = os.environ.get("CBAGENT_PET_MAX_SIZE")
        if env_value:
            try:
                return max(MIN_WINDOW_EDGE, min(MAX_WINDOW_EDGE, int(float(env_value))))
            except ValueError:
                pass
        state = self._read_runtime_state()
        try:
            return max(MIN_WINDOW_EDGE, min(MAX_WINDOW_EDGE, int(float(state.get("max_size") or DEFAULT_MAX_WINDOW_EDGE))))
        except (TypeError, ValueError):
            return DEFAULT_MAX_WINDOW_EDGE

    def _persist_max_size(self) -> None:
        path = self._state_path()
        data = self._read_runtime_state()
        data["max_size"] = self.max_size
        data["updated_at"] = int(time.time() * 1000)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".pet.", suffix=".json", dir=str(path.parent), text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2))
                fh.write("\n")
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def run(self) -> None:
        _set_dpi_awareness()
        web_url = self._web_url()
        self._log(f"starting webview url={web_url}")
        self.window = webview.create_window(
            "cb-agent pet",
            url=web_url,
            width=DEFAULT_WINDOW_WIDTH,
            height=DEFAULT_WINDOW_HEIGHT,
            x=DEFAULT_WINDOW_X,
            y=DEFAULT_WINDOW_Y,
            frameless=True,
            easy_drag=True,
            on_top=True,
            transparent=True,
            background_color="#000000",
            hidden=True,
            js_api=self.api,
        )
        self.window.events.closed += self._on_window_closed
        self._log("webview window object created")
        webview.start(self._on_started, debug=False, gui="qt")

    def _on_started(self) -> None:
        self._log("webview event loop started")
        # pywebview's Qt backend already applies WA_TranslucentBackground and
        # QWebEnginePage transparent background when create_window(...,
        # transparent=True) is used. Touching Qt widgets from this worker
        # callback can crash Qt without a Python traceback.
        self._wait_for_renderer()
        self._start_input_listeners()
        threading.Thread(target=self._stdin_reader, daemon=True).start()
        self._log("ready")

    def _configure_native_transparency(self) -> None:
        if self._configure_qt_transparency():
            return
        self._configure_winforms_transparency()

    def _configure_qt_transparency(self) -> bool:
        native = getattr(self.window, "native", None) if self.window is not None else None
        if native is None:
            return False

        try:
            from qtpy import QtCore, QtGui  # type: ignore[import-not-found]
        except Exception:
            return False

        def apply() -> None:
            transparent = QtGui.QColor(0, 0, 0, 0)
            widgets = [
                native,
                getattr(native, "webview", None),
                getattr(getattr(native, "browser", None), "webview", None),
                getattr(native, "browser", None),
            ]
            for widget in widgets:
                if widget is None:
                    continue
                try:
                    widget.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
                    widget.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
                    widget.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, False)
                except Exception:
                    pass
                try:
                    widget.setAutoFillBackground(False)
                except Exception:
                    pass
                try:
                    palette = widget.palette()
                    palette.setColor(widget.backgroundRole(), transparent)
                    widget.setPalette(palette)
                except Exception:
                    pass
                try:
                    widget.setStyleSheet("background: transparent;")
                except Exception:
                    pass

            webview_widget = getattr(native, "webview", None)
            try:
                page = webview_widget.page()
                page.setBackgroundColor(transparent)
            except Exception:
                pass
            try:
                native.update()
            except Exception:
                pass

        try:
            apply()
        except Exception as exc:
            try:
                QtCore.QTimer.singleShot(0, apply)
            except Exception:
                print(f"pet qt transparent window setup failed: {exc}", file=sys.stderr)
                return False
        return True

    def _configure_winforms_transparency(self) -> None:
        if sys.platform != "win32" or self.window is None:
            return

        try:
            from System import Action  # type: ignore[import-not-found]
            from System.Drawing import Color  # type: ignore[import-not-found]
        except Exception:
            return

        native = getattr(self.window, "native", None)
        if native is None:
            return

        def apply() -> None:
            key = Color.FromArgb(240, 240, 240)
            try:
                native.BackColor = key
                native.TransparencyKey = key
            except Exception:
                pass

            for target in (
                getattr(native, "webview", None),
                getattr(getattr(native, "browser", None), "webview", None),
                getattr(native, "browser", None),
            ):
                if target is None:
                    continue
                try:
                    target.DefaultBackgroundColor = Color.Transparent
                except Exception:
                    pass

            handle = getattr(native, "Handle", None)
            try:
                hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle.ToInt32())
            except Exception:
                hwnd = 0
            _apply_layered_color_key(hwnd)

        try:
            if getattr(native, "InvokeRequired", False):
                native.Invoke(Action(apply))
            else:
                apply()
        except Exception as exc:
            print(f"pet transparent window setup failed: {exc}", file=sys.stderr)

    def _on_window_closed(self) -> None:
        self._log("window closed")
        self._closed = True
        self._cleanup()

    def _wait_for_renderer(self) -> None:
        for _ in range(100):
            try:
                if self.window.evaluate_js("Boolean(window.cbPet)"):
                    self._log("renderer bridge ready")
                    self.ready.set()
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self._log("renderer bridge wait timed out")
        self.ready.set()

    def _stdin_reader(self) -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._handle_message(message)
        finally:
            if not self._closed:
                self.shutdown()

    def _handle_message(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self._log(f"received {method}")
        if method == "pet.load":
            self.load_package(params)
        elif method == "pet.set_visible":
            self.set_visible(bool(params.get("visible", True)))
        elif method == "pet.set_state":
            self._call("setState", str(params.get("state") or "idle"))
        elif method == "pet.set_size":
            self.set_size(params)
        elif method == "pet.set_hidden_drawables":
            self._call(
                "setHiddenDrawables",
                _as_list(params.get("indices")),
                _as_list(params.get("ids")),
            )
        elif method == "pet.debug_status":
            status = self._eval_value("JSON.stringify(window.cbPet.getStatus())")
            print(f"pet renderer status: {status}", file=sys.stderr)
        elif method == "pet.shutdown":
            self.shutdown()

    def load_package(self, params: Dict[str, Any]) -> None:
        path = params.get("path")
        if not path:
            self._log("load ignored: missing path")
            return
        root = Path(str(path)).expanduser().resolve()
        if not root.is_dir():
            self._log(f"load ignored: not a directory path={root}")
            return
        self._apply_max_size_param(params)
        self._log(f"loading package path={root}")
        manifest = _read_json(root / "pet.json") or {}
        if manifest.get("renderer") == "spritesheet" or params.get("renderer") == "spritesheet":
            payload = self._build_spritesheet_payload(root, manifest)
        else:
            payload = self._build_live2d_payload(root, manifest)
        if payload:
            self.set_visible(True)
            time.sleep(0.1)
            self._eval(f"window.cbPet.load({json.dumps(payload, ensure_ascii=False)})")
            status = self._wait_for_loaded_renderer(str(payload.get("renderer") or ""))
            if status:
                self._resize_window_to_status(status)
            self.set_visible(True)
        else:
            self._log(f"load failed: could not build payload path={root}")

    def _build_live2d_payload(self, root: Path, manifest: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        model_file = next(iter(sorted(root.glob("*.model3.json"))), None)
        if model_file is None:
            return None
        model_json = _read_json(model_file)
        if model_json is None:
            return None

        manifest = manifest or {}
        show_background = bool(manifest.get("showBackground") or manifest.get("show_background"))
        transparent_parameters = self._transparent_parameter_ids(root, model_json, manifest)
        hidden_parts = self._hidden_part_ids(root, model_json, manifest)
        base_url = self._set_asset_root(root)
        keys: Dict[str, Dict[str, str]] = {}
        resources = root / "resources"
        for side in ("left", "right"):
            group_dir = resources / f"{side}-keys"
            if not group_dir.is_dir():
                continue
            for image in group_dir.glob("*.png"):
                rel = f"resources/{side}-keys/{image.name}"
                keys[image.stem] = {"image": _url_for(base_url, rel, self.asset_version), "side": side}

        background = resources / "background.png"
        return {
            "renderer": "live2d",
            "modelJson": model_json,
            "assetBaseUrl": base_url,
            "assetVersion": self.asset_version,
            "background": _url_for(base_url, "resources/background.png", self.asset_version) if show_background and background.is_file() else None,
            "keys": keys,
            "transparentParameterIds": transparent_parameters,
            "hiddenPartIds": hidden_parts,
        }

    def _transparent_parameter_ids(
        self,
        root: Path,
        model_json: Dict[str, Any],
        manifest: Dict[str, Any],
    ) -> list[str]:
        explicit = manifest.get("transparentParameterIds") or manifest.get("transparent_parameters")
        if isinstance(explicit, list):
            return [str(item) for item in explicit if str(item).strip()]
        auto_enabled = bool(
            manifest.get("autoTransparentParameters")
            or manifest.get("auto_transparent_parameters")
            or manifest.get("autoTransparentModelBackground")
            or manifest.get("auto_transparent_model_background")
        )
        if not auto_enabled:
            return []

        refs = model_json.get("FileReferences") if isinstance(model_json.get("FileReferences"), dict) else {}
        display_info = refs.get("DisplayInfo")
        if not display_info:
            return []

        data = _read_json(root / _norm_rel(display_info))
        parameters = _as_list(data.get("Parameters") if data else [])
        matches: list[str] = []
        for item in parameters:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("Id") or "").strip()
            if not identifier:
                continue
            name = str(item.get("Name") or "")
            if _looks_like_transparency_control(identifier, name):
                matches.append(identifier)
        return matches

    def _hidden_part_ids(
        self,
        root: Path,
        model_json: Dict[str, Any],
        manifest: Dict[str, Any],
    ) -> list[str]:
        explicit = manifest.get("hiddenPartIds") or manifest.get("hidden_part_ids")
        if isinstance(explicit, list):
            return [str(item) for item in explicit if str(item).strip()]
        auto_enabled = bool(manifest.get("autoHideOpaqueParts") or manifest.get("auto_hide_opaque_parts"))
        if not auto_enabled:
            return []

        refs = model_json.get("FileReferences") if isinstance(model_json.get("FileReferences"), dict) else {}
        display_info = refs.get("DisplayInfo")
        if not display_info:
            return []

        data = _read_json(root / _norm_rel(display_info))
        parts = _as_list(data.get("Parts") if data else [])
        matches: list[str] = []
        for item in parts:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("Id") or "").strip()
            if not identifier:
                continue
            name = str(item.get("Name") or "")
            if _looks_like_opaque_background_part(identifier, name):
                matches.append(identifier)
        return matches

    def _build_spritesheet_payload(self, root: Path, manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sprite_rel = manifest.get("spritesheetPath") or manifest.get("spritesheet")
        if not sprite_rel:
            return None
        sprite_path = root / str(sprite_rel)
        if not sprite_path.is_file():
            return None
        atlas = manifest.get("atlas")
        states = manifest.get("states")
        if not isinstance(atlas, dict) or not isinstance(states, dict):
            return None
        base_url = self._set_asset_root(root)
        return {
            "renderer": "spritesheet",
            "image": _url_for(base_url, str(sprite_rel), self.asset_version),
            "atlas": atlas,
            "states": states,
        }

    def _set_asset_root(self, root: Path) -> str:
        server = self._ensure_asset_server()
        server.asset_root = root.resolve()
        self.asset_version = str(int(time.time() * 1000))
        host, port = server.server_address
        return f"http://{host}:{port}"

    def _ensure_asset_server(self) -> _AssetServer:
        if self.asset_server is None:
            self.asset_server = _AssetServer()
            self.asset_server.web_root = self.web_dir.resolve()
            self.asset_thread = threading.Thread(target=self.asset_server.serve_forever, daemon=True)
            self.asset_thread.start()
        return self.asset_server

    def _web_url(self) -> str:
        server = self._ensure_asset_server()
        host, port = server.server_address
        return f"http://{host}:{port}/__cbagent_web__/index.html"

    def _renderer_status(self) -> Optional[Dict[str, Any]]:
        value = self._eval_value("JSON.stringify(window.cbPet.getStatus())")
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return value if isinstance(value, dict) else None

    def _wait_for_loaded_renderer(self, renderer: str) -> Optional[Dict[str, Any]]:
        deadline = time.time() + MAX_INITIAL_MODEL_LOAD_SECONDS
        last_status: Optional[Dict[str, Any]] = None
        while time.time() < deadline:
            status = self._renderer_status()
            if status:
                last_status = status
                if status.get("error"):
                    self._log(f"renderer load failed: {status.get('error')}")
                    return status
                if renderer == "live2d" and status.get("live2dReady"):
                    self._log(f"renderer ready: {json.dumps(status, ensure_ascii=False)}")
                    return status
                if renderer == "spritesheet" and status.get("renderer") == "spritesheet":
                    self._log(f"renderer ready: {json.dumps(status, ensure_ascii=False)}")
                    return status
            time.sleep(0.1)
        if last_status:
            self._log(f"renderer load timed out: {json.dumps(last_status, ensure_ascii=False)}")
        return last_status

    def _resize_window_to_status(self, status: Dict[str, Any]) -> None:
        width_value = status.get("modelNaturalWidth") or status.get("modelWidth")
        height_value = status.get("modelNaturalHeight") or status.get("modelHeight")
        try:
            width = float(width_value)
            height = float(height_value)
        except (TypeError, ValueError):
            return
        if width <= 0 or height <= 0:
            return
        self.model_size = (width, height)
        self._resize_to_model_size(width, height)

    def _resize_to_model_size(self, width: float, height: float) -> None:
        scale_text = os.environ.get("CBAGENT_PET_SCALE")
        if scale_text:
            try:
                scale = max(0.1, float(scale_text))
            except ValueError:
                scale = 1.0
        else:
            max_edge = float(self.max_size)
            scale = min(1.0, max_edge / max(width, height))

        target_width = max(80, int(round(width * scale)))
        target_height = max(80, int(round(height * scale)))
        try:
            self.window.resize(target_width, target_height)
            self._move_window_into_view(target_width, target_height)
            self._log(f"window resized width={target_width} height={target_height}")
        except Exception as exc:
            self._log(f"window resize failed: {exc}")

    def _apply_max_size_param(self, params: Dict[str, Any]) -> None:
        value = params.get("max_size") or params.get("maxSize")
        if value is None:
            return
        try:
            self.max_size = max(MIN_WINDOW_EDGE, min(MAX_WINDOW_EDGE, int(float(value))))
        except (TypeError, ValueError):
            return

    def set_size(self, params: Dict[str, Any]) -> None:
        self._apply_max_size_param(params)
        if self.model_size is not None:
            self._resize_to_model_size(*self.model_size)
        else:
            try:
                self.window.resize(self.max_size, self.max_size)
                self._move_window_into_view(self.max_size, self.max_size)
                self._log(f"window resized width={self.max_size} height={self.max_size}")
            except Exception as exc:
                self._log(f"window resize failed: {exc}")

    def _move_window_into_view(self, width: int, height: int) -> None:
        _ = (width, height)
        try:
            target_x = int(os.environ.get("CBAGENT_PET_X", str(DEFAULT_WINDOW_X)))
        except ValueError:
            target_x = DEFAULT_WINDOW_X
        try:
            target_y = int(os.environ.get("CBAGENT_PET_Y", str(DEFAULT_WINDOW_Y)))
        except ValueError:
            target_y = DEFAULT_WINDOW_Y
        try:
            self.window.move(int(target_x), int(target_y))
            self._log(f"window moved x={target_x} y={target_y}")
        except Exception as exc:
            self._log(f"window move failed: {exc}")

    def set_visible(self, visible: bool) -> None:
        self.visible = visible
        try:
            if visible:
                self.window.show()
                self._log("window show requested")
            else:
                self.window.hide()
                self._log("window hide requested")
        except Exception:
            self._log("window visibility change failed")

    def shutdown(self) -> None:
        self._cleanup()
        if self._closed:
            return
        try:
            self.window.destroy()
        except Exception:
            pass

    def _cleanup(self) -> None:
        self._stop_input_listeners()
        if self.asset_server is not None:
            try:
                self.asset_server.shutdown()
                self.asset_server.server_close()
            except Exception:
                pass
            self.asset_server = None

    def _start_input_listeners(self) -> None:
        if self.keyboard_listener or self.mouse_listener:
            return
        try:
            self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press, on_release=self._on_key_release)
            self.mouse_listener = mouse.Listener(
                on_move=self._on_mouse_move,
                on_click=self._on_mouse_click,
                on_scroll=self._on_mouse_scroll,
            )
            self.keyboard_listener.start()
            self.mouse_listener.start()
        except Exception as exc:
            print(f"pet input listener failed: {exc}", file=sys.stderr)
            self.keyboard_listener = None
            self.mouse_listener = None

    def _stop_input_listeners(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass
        self.keyboard_listener = None
        self.mouse_listener = None

    def _on_key_press(self, key: Any) -> None:
        name = _key_name(key)
        if name:
            auto_release = WINDOWS_KEY_AUTO_RELEASE_MS if sys.platform == "win32" else 0
            self._call("pressKey", name, True, auto_release)

    def _on_key_release(self, key: Any) -> None:
        name = _key_name(key)
        if name:
            self._call("pressKey", name, False)

    def _on_mouse_click(self, _x: int, _y: int, button: mouse.Button, pressed: bool) -> None:
        if button == mouse.Button.left:
            self._call("mouseButton", "left", pressed)
        elif button == mouse.Button.right:
            self._call("mouseButton", "right", pressed)

    def _window_bounds(self) -> Optional[tuple[int, int, int, int]]:
        if self.window is None:
            return None
        try:
            x = int(self.window.x)
            y = int(self.window.y)
            width = int(self.window.width)
            height = int(self.window.height)
        except Exception:
            return None
        return (x, y, x + max(1, width), y + max(1, height))

    def _is_point_in_window(self, x: int, y: int) -> bool:
        bounds = self._window_bounds()
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        return left <= x <= right and top <= y <= bottom

    def _on_mouse_scroll(self, x: int, y: int, _dx: int, dy: int) -> None:
        if dy == 0:
            return
        if not self._is_point_in_window(x, y):
            self._log(f"wheel ignored outside window x={x} y={y} bounds={self._window_bounds()}")
            return
        self.resize_from_wheel_delta(-dy, source="pynput")

    def resize_from_wheel_delta(self, delta_y: Any, *, source: str) -> None:
        try:
            delta = float(delta_y)
        except (TypeError, ValueError):
            return
        if delta == 0:
            return
        next_size = self.max_size + (WHEEL_SIZE_STEP if delta < 0 else -WHEEL_SIZE_STEP)
        self.max_size = max(MIN_WINDOW_EDGE, min(MAX_WINDOW_EDGE, next_size))
        self._persist_max_size()
        self.set_size({"max_size": self.max_size})
        self._log(f"{source} wheel resized max_size={self.max_size}")

    def _on_mouse_move(self, x: int, y: int) -> None:
        now = time.time()
        if now - self.last_mouse_emit < 1 / MAX_MOUSE_FPS:
            return
        self.last_mouse_emit = now
        left, top, right, bottom = _monitor_rect(x, y)
        width = max(1, right - left)
        height = max(1, bottom - top)
        x_ratio = max(0, min((x - left) / width, 1))
        y_ratio = max(0, min((y - top) / height, 1))
        self._call("mouseMove", x_ratio, y_ratio)

    def _call(self, name: str, *args: Any) -> None:
        encoded = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
        self._eval(f"window.cbPet.{name}({encoded})")

    def _eval(self, script: str) -> None:
        self._eval_value(script)

    def _eval_value(self, script: str) -> Any:
        self.ready.wait(timeout=10)
        try:
            return self.window.evaluate_js(script)
        except Exception:
            return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the cb-agent Live2D desktop pet runtime.")
    parser.add_argument("--stdio", action="store_true", help="accept stdio JSON-RPC control messages")
    parser.parse_args(argv)
    PetRuntime().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
