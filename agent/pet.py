"""Desktop pet state, package catalog, and lightweight runtime control."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import json5  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency guard
    json5 = None  # type: ignore[assignment]


PET_ACTIVITIES = {"idle", "running", "waiting", "review", "failed", "waving", "jumping"}
PET_METADATA_FILE = ".cbagent-pet.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip()).strip("-._")
    if cleaned:
        return cleaned[:96]
    digest = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:10]
    return f"pet-{digest}" if digest else f"pet-{_now_ms()}"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if json5 is None:
                return None
            data = json5.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _infer_live2d_mode(root: Path) -> str:
    right_keys = root / "resources" / "right-keys"
    right_key_files = list(right_keys.glob("*.png")) if right_keys.is_dir() else []
    if any("East" in item.stem for item in right_key_files):
        return "gamepad"
    if right_key_files:
        return "keyboard"
    return "standard"


def detect_pet_package(path: Path) -> Optional[Dict[str, Any]]:
    """Return a normalized package summary for a BongoCat-compatible pet folder."""
    root = path.expanduser().resolve()
    if not root.is_dir():
        return None

    metadata = _read_json(root / PET_METADATA_FILE) or {}
    manifest = _read_json(root / "pet.json")
    if manifest and str(manifest.get("renderer") or "").lower() == "spritesheet":
        sprite = root / str(manifest.get("spritesheetPath") or manifest.get("spritesheet") or "")
        return {
            "id": _safe_id(str(manifest.get("id") or root.name)),
            "displayName": str(metadata.get("displayName") or manifest.get("displayName") or manifest.get("name") or root.name),
            "description": str(manifest.get("description") or ""),
            "renderer": "spritesheet",
            "path": str(root),
            "ok": sprite.is_file(),
            "issues": [] if sprite.is_file() else [f"missing spritesheet: {sprite.name}"],
        }

    model_files = sorted(root.glob("*.model3.json"))
    if model_files:
        model = _read_json(model_files[0]) or {}
        refs = model.get("FileReferences") if isinstance(model.get("FileReferences"), dict) else {}
        issues: List[str] = []
        moc = refs.get("Moc")
        if moc and not (root / str(moc)).is_file():
            issues.append(f"missing Moc: {moc}")
        textures = refs.get("Textures") if isinstance(refs.get("Textures"), list) else []
        for item in textures:
            if not (root / str(item)).is_file():
                issues.append(f"missing texture: {item}")
        cover = root / "resources" / "cover.png"
        if not cover.is_file():
            issues.append("missing resources/cover.png")
        return {
            "id": _safe_id(root.name),
            "displayName": str(metadata.get("displayName") or root.name),
            "description": "BongoCat Live2D model",
            "renderer": "live2d",
            "mode": _infer_live2d_mode(root),
            "path": str(root),
            "modelFile": model_files[0].name,
            "ok": not issues,
            "issues": issues,
        }

    return None


class PetRuntimeController:
    """Small stdio JSON-RPC client for the cb-agent Python pet sidecar."""

    STARTUP_READY_TEXT = "pet runtime: ready"
    STARTUP_TIMEOUT_SECONDS = 12.0

    def __init__(self, runtime_path: Optional[Path] = None) -> None:
        self.runtime_path = runtime_path
        self.project_root = Path(__file__).resolve().parent.parent
        self._proc: Optional[subprocess.Popen[str]] = None
        self._log_file: Any = None
        self._lock = threading.RLock()
        self.log_path = self.project_root / ".cbagent" / "logs" / "system" / "pet-runtime.log"

    def _close_log_locked(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _reap_finished_locked(self) -> Optional[int]:
        if self._proc is None:
            return None
        code = self._proc.poll()
        if code is None:
            return None
        self._proc = None
        self._close_log_locked()
        return code

    def find_runtime(self) -> Optional[Path]:
        runtime = self.runtime_path or (Path(__file__).resolve().parent / "pet_runtime.py")
        runtime = runtime.expanduser().resolve()
        return runtime if runtime.is_file() else None

    def _read_log_since(self, log_path: Path, offset: int) -> str:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                return fh.read()
        except Exception:
            return ""

    def _wait_for_startup_locked(self, log_path: Path, offset: int) -> Optional[str]:
        deadline = time.time() + self.STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            code = self._proc.poll() if self._proc is not None else None
            if code is not None:
                return f"Pet runtime exited during startup with code {code}. See {log_path}"
            text = self._read_log_since(log_path, offset)
            if self.STARTUP_READY_TEXT in text:
                return None
            time.sleep(0.1)
        return f"Pet runtime did not become ready within {self.STARTUP_TIMEOUT_SECONDS:.0f}s. See {log_path}"

    def is_running(self) -> bool:
        with self._lock:
            self._reap_finished_locked()
            return self._proc is not None and self._proc.poll() is None

    def _dependency_error(self) -> Optional[str]:
        project_root = Path(__file__).resolve().parent.parent
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import webview, pynput, json5, qtpy; "
                        "from qtpy.QtWebEngineWidgets import QWebEngineView"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(project_root),
                timeout=10,
                creationflags=creationflags,
            )
        except Exception as exc:
            return f"Unable to check pet runtime dependencies: {exc}"
        if result.returncode == 0:
            return None
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        first_line = detail[-1] if detail else "unknown import error"
        return (
            f"Missing pet runtime dependency for {sys.executable}: {first_line}\n"
            f"Run: \"{sys.executable}\" -m pip install -r \"{project_root / 'requirements.txt'}\"\n"
            "Then restart the CLI/TUI and run /pet launch again."
        )

    def launch(self) -> Dict[str, Any]:
        with self._lock:
            if self.is_running():
                return {"ok": True, "status": "running", "pid": self._proc.pid if self._proc else None}
            runtime = self.find_runtime()
            if runtime is None:
                return {
                    "ok": False,
                    "status": "missing",
                    "message": "cb-agent pet runtime not found.",
                }
            dep_error = self._dependency_error()
            if dep_error:
                return {"ok": False, "status": "missing-dependency", "message": dep_error}
            log_path = self.log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = log_path.open("a", encoding="utf-8", buffering=1)
            self._log_file.write(f"\n--- pet runtime launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self._log_file.flush()
            log_offset = log_path.stat().st_size
            try:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                self._proc = subprocess.Popen(
                    [sys.executable, "-m", "agent.pet_runtime", "--stdio"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=self._log_file,
                    text=True,
                    encoding="utf-8",
                    cwd=str(self.project_root),
                    creationflags=creationflags,
                )
            except OSError as exc:
                self._proc = None
                self._close_log_locked()
                return {"ok": False, "status": "error", "message": str(exc)}
            except Exception as exc:
                self._proc = None
                self._close_log_locked()
                return {"ok": False, "status": "error", "message": str(exc)}

            startup_error = self._wait_for_startup_locked(log_path, log_offset)
            if startup_error:
                proc = self._proc
                self._close_log_locked()
                self._proc = None
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return {
                    "ok": False,
                    "status": "startup-timeout",
                    "message": startup_error,
                }
            return {"ok": True, "status": "running", "pid": self._proc.pid, "path": str(runtime)}

    def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock:
            if not self.is_running() or self._proc is None or self._proc.stdin is None:
                return False
            try:
                self._proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": f"p{_now_ms()}",
                    "method": method,
                    "params": params or {},
                }, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                return True
            except Exception:
                return False

    def shutdown(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            log_file = self._log_file
            self._log_file = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if proc.stdin:
                    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "shutdown", "method": "pet.shutdown"}) + "\n")
                    proc.stdin.flush()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
        except Exception:
            pass
        finally:
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass


class PetManager:
    """Persistent desktop pet catalog and command surface."""

    def __init__(
        self,
        *,
        storage_path: Optional[Path] = None,
        library_dir: Optional[Path] = None,
        dropin_dirs: Optional[List[Path]] = None,
        runtime: Optional[PetRuntimeController] = None,
    ) -> None:
        base = Path.home() / ".cbagent"
        project_root = Path(__file__).resolve().parent.parent
        self.storage_path = Path(storage_path or base / "pet" / "state.json")
        self.library_dir = Path(library_dir or base / "pets")
        self.dropin_dirs = [Path(item) for item in (dropin_dirs or [project_root / ".cbagent" / "pet", base / "pet"])]
        self.runtime = runtime or PetRuntimeController()

    def _default_config(self) -> Dict[str, Any]:
        return {
            "current_pet_id": None,
            "visible": True,
            "activity": "idle",
            "max_size": 420,
            "updated_at": None,
        }

    def _load_config(self) -> Dict[str, Any]:
        cfg = self._default_config()
        data = _read_json(self.storage_path)
        if data:
            cfg.update(data)
        if cfg.get("activity") not in PET_ACTIVITIES:
            cfg["activity"] = "idle"
        try:
            cfg["max_size"] = max(120, min(1600, int(float(cfg.get("max_size") or 420))))
        except (TypeError, ValueError):
            cfg["max_size"] = 420
        return cfg

    def _save_config(self, cfg: Dict[str, Any]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(prefix=".pet.", suffix=".json", dir=str(self.storage_path.parent), text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            tmp_path.replace(self.storage_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _write_metadata(self, dest: Path, package: Dict[str, Any], source: Path) -> None:
        metadata = {
            "id": package.get("id"),
            "displayName": package.get("displayName") or source.name,
            "renderer": package.get("renderer"),
            "mode": package.get("mode"),
            "sourcePath": str(source),
            "installedAt": _now_ms(),
        }
        (dest / PET_METADATA_FILE).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _copy_package_to_library(self, source: Path, package: Dict[str, Any], *, replace: bool) -> Dict[str, Any]:
        pet_id = _safe_id(str(package.get("id") or source.name))
        dest = self.library_dir / pet_id
        self.library_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if not replace:
                installed = detect_pet_package(dest)
                return installed or {**package, "id": pet_id, "path": str(dest)}
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        self._write_metadata(dest, {**package, "id": pet_id}, source)
        installed = detect_pet_package(dest)
        return installed or {**package, "id": pet_id, "path": str(dest)}

    def _sync_dropin_pets(self, *, select_new: bool = False) -> List[Dict[str, Any]]:
        synced: List[Dict[str, Any]] = []
        library = self.library_dir.expanduser().resolve()
        seen_dirs = set()

        for dropin_dir in self.dropin_dirs:
            root = dropin_dir.expanduser().resolve()
            if root in seen_dirs or not root.is_dir():
                continue
            seen_dirs.add(root)
            if root == library:
                continue
            for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not item.is_dir() or item.name.startswith("."):
                    continue
                package = detect_pet_package(item)
                if package is None:
                    continue
                pet_id = _safe_id(str(package.get("id") or item.name))
                if (self.library_dir / pet_id).exists():
                    continue
                synced.append(self._copy_package_to_library(item, package, replace=False))

        if select_new and synced:
            cfg = self._load_config()
            selected = synced[-1]
            cfg["current_pet_id"] = selected.get("id")
            cfg["visible"] = True
            cfg["updated_at"] = _now_ms()
            self._save_config(cfg)

        return synced

    def list_pets(self) -> List[Dict[str, Any]]:
        self._sync_dropin_pets()
        self.library_dir.mkdir(parents=True, exist_ok=True)
        pets: List[Dict[str, Any]] = []
        for item in sorted(self.library_dir.iterdir(), key=lambda p: p.name.lower()):
            if item.is_dir():
                package = detect_pet_package(item)
                if package:
                    pets.append(package)
        return pets

    def current_pet(self) -> Optional[Dict[str, Any]]:
        pet_id = self._load_config().get("current_pet_id")
        if not pet_id:
            return None
        for pet in self.list_pets():
            if pet.get("id") == pet_id:
                return pet
        return None

    def state(self) -> Dict[str, Any]:
        cfg = self._load_config()
        current = self.current_pet()
        runtime_running = self.runtime.is_running()
        status = "running" if runtime_running else "stopped"
        return {
            "enabled": True,
            "status": status,
            "visible": bool(cfg.get("visible", True)),
            "activity": cfg.get("activity") or "idle",
            "max_size": cfg.get("max_size", 420),
            "current_pet_id": cfg.get("current_pet_id"),
            "current_pet": current,
            "pets": self.list_pets(),
            "runtime": {
                "kind": "cbagent-python",
                "running": runtime_running,
                "path": str(self.runtime.find_runtime()) if self.runtime.find_runtime() else None,
            },
            "message": None if current else "No pet selected. Use /pet install <folder>, drop a package into .cbagent/pet, or use /pet select <id>.",
        }

    def set_activity(self, activity: str) -> Optional[Dict[str, Any]]:
        if activity not in PET_ACTIVITIES:
            return None
        cfg = self._load_config()
        if cfg.get("activity") == activity:
            return None
        cfg["activity"] = activity
        cfg["updated_at"] = _now_ms()
        self._save_config(cfg)
        self.runtime.send("pet.set_state", {"state": activity})
        return self.state()

    def handle_command(self, args: str = "") -> Dict[str, Any]:
        parts = (args or "").strip().split()
        sub = (parts[0].lower() if parts else "status")
        rest = " ".join(parts[1:]).strip()

        if sub in {"", "status"}:
            return {"text": self._format_status(), "state": self.state(), "changed": False}
        if sub == "list":
            return {"text": self._format_list(), "state": self.state(), "changed": False}
        if sub == "install":
            return self._install(rest)
        if sub == "select":
            return self._select(rest)
        if sub in {"uninstall", "remove", "delete"}:
            return self._uninstall(rest)
        if sub == "launch":
            return self._launch()
        if sub in {"show", "hide"}:
            return self._set_visible(sub == "show")
        if sub == "quit":
            self.runtime.shutdown()
            return {"text": "Pet runtime stopped.", "state": self.state(), "changed": True}
        if sub in PET_ACTIVITIES:
            state = self.set_activity(sub) or self.state()
            return {"text": f"Pet activity set to {sub}.", "state": state, "changed": True}
        return {
            "text": "Unknown /pet command. Available: /pet status|show|hide|launch|quit|list|select <id>|install <folder>|uninstall <id>",
            "state": self.state(),
            "changed": False,
        }

    def _install(self, raw_path: str) -> Dict[str, Any]:
        if not raw_path:
            return {
                "text": (
                    "Usage: /pet install <folder>\n"
                    "<folder> must be the pet package root: a Live2D folder containing *.model3.json, "
                    "or a spritesheet folder containing pet.json.\n"
                    'Example: /pet install "C:\\path\\to\\守岸人 · 标准模式"'
                ),
                "state": self.state(),
                "changed": False,
            }
        source = Path(raw_path.strip().strip('"')).expanduser().resolve()
        package = detect_pet_package(source)
        if package is None:
            return {"text": f"Not a BongoCat-compatible pet folder: {source}", "state": self.state(), "changed": False}
        installed = self._copy_package_to_library(source, package, replace=True)
        pet_id = str(installed.get("id"))
        dest = Path(str(installed.get("path")))
        cfg = self._load_config()
        cfg["current_pet_id"] = pet_id
        cfg["updated_at"] = _now_ms()
        self._save_config(cfg)
        if self.runtime.is_running():
            self.runtime.send("pet.load", {
                "id": pet_id,
                "mode": installed.get("mode"),
                "path": str(dest),
                "renderer": installed.get("renderer"),
                "max_size": cfg.get("max_size", 420),
            })
            self.runtime.send("pet.set_visible", {"visible": cfg.get("visible", True)})
        issues = installed.get("issues") or []
        warning = f" issues={len(issues)}" if issues else ""
        return {
            "text": (
                f"Installed and selected pet {pet_id} ({installed.get('displayName')}, {installed.get('renderer')}){warning}.\n"
                f"Source: {source}\n"
                f"Library: {dest}"
            ),
            "state": self.state(),
            "changed": True,
        }

    def _select(self, pet_id: str) -> Dict[str, Any]:
        if not pet_id:
            return {"text": "Usage: /pet select <id>", "state": self.state(), "changed": False}
        pets = {str(item.get("id")): item for item in self.list_pets()}
        if pet_id not in pets:
            return {"text": f"Pet not found: {pet_id}", "state": self.state(), "changed": False}
        cfg = self._load_config()
        cfg["current_pet_id"] = pet_id
        cfg["updated_at"] = _now_ms()
        self._save_config(cfg)
        loaded = self.runtime.send("pet.load", {
            "id": pet_id,
            "mode": pets[pet_id].get("mode"),
            "path": pets[pet_id].get("path"),
            "renderer": pets[pet_id].get("renderer"),
            "max_size": cfg.get("max_size", 420),
        })
        suffix = " Loaded into running runtime." if loaded else " Use /pet launch to display it."
        return {"text": f"Selected pet {pet_id} ({pets[pet_id].get('displayName')}).{suffix}", "state": self.state(), "changed": True}

    def _uninstall(self, pet_id: str) -> Dict[str, Any]:
        if not pet_id:
            return {"text": "Usage: /pet uninstall <id>\nUse /pet list to see installed ids.", "state": self.state(), "changed": False}
        pets = {str(item.get("id")): item for item in self.list_pets()}
        pet = pets.get(pet_id)
        if not pet:
            return {"text": f"Pet not found: {pet_id}", "state": self.state(), "changed": False}

        target = Path(str(pet.get("path") or "")).resolve()
        library = self.library_dir.resolve()
        if target.parent != library:
            return {"text": f"Refusing to uninstall pet outside library: {target}", "state": self.state(), "changed": False}

        current = self._load_config().get("current_pet_id")
        shutil.rmtree(target)
        cfg = self._load_config()
        if current == pet_id:
            cfg["current_pet_id"] = None
            cfg["visible"] = False
            self.runtime.send("pet.set_visible", {"visible": False})
        cfg["updated_at"] = _now_ms()
        self._save_config(cfg)
        return {"text": f"Uninstalled pet {pet_id} ({pet.get('displayName')}).", "state": self.state(), "changed": True}

    def _launch(self) -> Dict[str, Any]:
        synced = self._sync_dropin_pets(select_new=True)
        result = self.runtime.launch()
        current = self.current_pet()
        loaded = False
        if result.get("ok") and current:
            cfg = self._load_config()
            loaded = self.runtime.send("pet.load", {
                "id": current.get("id"),
                "mode": current.get("mode"),
                "path": current.get("path"),
                "renderer": current.get("renderer"),
                "max_size": cfg.get("max_size", 420),
            })
            self.runtime.send("pet.set_visible", {"visible": cfg.get("visible", True)})
        text = "Pet runtime launched." if result.get("ok") else str(result.get("message") or "Pet runtime launch failed.")
        if result.get("ok") and current and not loaded:
            text += f"\nSelected pet could not be sent to the runtime. Check {self.runtime.log_path}."
        if synced:
            names = ", ".join(f"{item.get('id')} ({item.get('displayName')})" for item in synced)
            text += f"\nAuto-imported from drop-in folder: {names}."
        return {"text": text, "state": self.state(), "changed": bool(result.get("ok"))}

    def _set_visible(self, visible: bool) -> Dict[str, Any]:
        cfg = self._load_config()
        cfg["visible"] = visible
        cfg["updated_at"] = _now_ms()
        self._save_config(cfg)
        self.runtime.send("pet.set_visible", {"visible": visible})
        return {"text": "Pet shown." if visible else "Pet hidden.", "state": self.state(), "changed": True}

    def _format_status(self) -> str:
        state = self.state()
        current = state.get("current_pet") or {}
        name = current.get("displayName") or state.get("current_pet_id") or "none"
        return f"Pet status: {state['status']}; visible={state['visible']}; activity={state['activity']}; current={name}"

    def _format_list(self) -> str:
        pets = self.list_pets()
        if not pets:
            return "No installed pets. Use /pet install <folder>."
        current = self._load_config().get("current_pet_id")
        lines = ["Installed pets (* current):"]
        for item in pets:
            mark = "*" if item.get("id") == current else " "
            issue = "" if item.get("ok") else f" issues={len(item.get('issues') or [])}"
            lines.append(
                f" {mark} {item.get('id')} - {item.get('displayName')} "
                f"({item.get('renderer')}, mode={item.get('mode') or 'n/a'}){issue}\n"
                f"    path={item.get('path')}"
            )
        return "\n".join(lines)


class PetEventBridge:
    """Map agent lifecycle events to desktop pet activities.

    The lightweight runtime owns the floating window. This bridge only sends
    high-level agent activity hints.
    """

    _EVENT_TO_ACTIVITY = {
        "round_start": "running",
        "tool_start": "running",
        "tool_call_planned": "running",
        "ask_user_question": "waiting",
        "done": "review",
        "error": "failed",
        "cancelled": "failed",
    }

    def __init__(self, manager: PetManager) -> None:
        self.manager = manager
        self._event_bus: Any = None

    def attach(self, event_bus: Any) -> None:
        if self._event_bus is event_bus:
            return
        self.detach()
        self._event_bus = event_bus
        event_bus.subscribe(self._on_event)

    def detach(self) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.unsubscribe(self._on_event)
        finally:
            self._event_bus = None

    def _on_event(self, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        activity = self._EVENT_TO_ACTIVITY.get(event_type)
        if not activity:
            return
        if event_type == "done" and bool(getattr(event, "cancelled", False)):
            activity = "failed"
        try:
            state = self.manager.set_activity(activity)
            if state is None:
                return
            from agent.events import PetUpdated

            self._event_bus.emit(PetUpdated(state=state, reason="agent"))
        except Exception:
            return


__all__ = [
    "PET_ACTIVITIES",
    "PetEventBridge",
    "PetManager",
    "PetRuntimeController",
    "detect_pet_package",
]
