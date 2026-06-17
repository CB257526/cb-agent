from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from agent.event_bus import EventBus, collect_all
from agent.events import AskUserQuestion, PetUpdated, RoundStart
from agent.pet import PetEventBridge, PetManager, PetRuntimeController, detect_pet_package
from agent import pet_runtime


class FakeRuntime:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.running = False
        self.shutdowns = 0

    def is_running(self) -> bool:
        return self.running

    def find_runtime(self):
        return None

    def launch(self) -> dict:
        self.running = True
        return {"ok": True, "status": "running"}

    def send(self, method: str, params=None) -> bool:
        self.sent.append((method, params or {}))
        return True

    def shutdown(self) -> None:
        self.running = False
        self.shutdowns += 1


def make_live2d_package(root: Path) -> Path:
    pkg = root / "live2d-cat"
    (pkg / "textures").mkdir(parents=True)
    (pkg / "resources").mkdir()
    (pkg / "resources" / "left-keys").mkdir()
    (pkg / "cat.moc3").write_bytes(b"moc")
    (pkg / "cat.physics3.json").write_text("{}", encoding="utf-8")
    (pkg / "cat.cdi3.json").write_text("{}", encoding="utf-8")
    (pkg / "textures" / "texture_00.png").write_bytes(b"png")
    (pkg / "resources" / "cover.png").write_bytes(b"png")
    (pkg / "resources" / "background.png").write_bytes(b"bg")
    (pkg / "resources" / "left-keys" / "KeyA.png").write_bytes(b"key")
    (pkg / "cat.model3.json").write_text(
        json.dumps({
            "Version": 3,
            "FileReferences": {
                "Moc": "cat.moc3",
                "Textures": ["textures/texture_00.png"],
                "Physics": "cat.physics3.json",
                "DisplayInfo": "cat.cdi3.json",
            },
        }),
        encoding="utf-8",
    )
    return pkg


def make_spritesheet_package(root: Path) -> Path:
    pkg = root / "sprite-cat"
    pkg.mkdir()
    (pkg / "atlas.png").write_bytes(b"png")
    (pkg / "pet.json").write_text(
        json.dumps({
            "id": "sprite-cat",
            "displayName": "Sprite Cat",
            "renderer": "spritesheet",
            "spritesheetPath": "atlas.png",
            "atlas": {
                "width": 1536,
                "height": 1872,
                "columns": 8,
                "rows": 9,
                "cellWidth": 192,
                "cellHeight": 208,
            },
            "states": {"idle": {"row": 0, "frames": 8, "durationMs": 120}},
        }),
        encoding="utf-8",
    )
    return pkg


class TestPetPackageDetection(unittest.TestCase):
    def test_detect_live2d_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            package = detect_pet_package(make_live2d_package(Path(td)))
        self.assertIsNotNone(package)
        self.assertEqual(package["renderer"], "live2d")
        self.assertTrue(package["ok"])

    def test_detect_spritesheet_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            package = detect_pet_package(make_spritesheet_package(Path(td)))
        self.assertIsNotNone(package)
        self.assertEqual(package["renderer"], "spritesheet")
        self.assertEqual(package["id"], "sprite-cat")
        self.assertTrue(package["ok"])

    def test_live2d_missing_texture_reports_issue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = make_live2d_package(Path(td))
            (pkg / "textures" / "texture_00.png").unlink()
            package = detect_pet_package(pkg)
        self.assertIsNotNone(package)
        self.assertFalse(package["ok"])
        self.assertIn("missing texture", package["issues"][0])

    def test_external_bongocat_model_sample_if_available(self) -> None:
        repo_parent = Path(__file__).resolve().parents[2]
        sample = repo_parent / "外部代码" / "守岸人 · 标准模式" / "守岸人 · 标准模式"
        if not sample.is_dir():
            self.skipTest("external BongoCat Live2D sample is not available")
        package = detect_pet_package(sample)
        self.assertIsNotNone(package)
        self.assertEqual(package["renderer"], "live2d")
        self.assertTrue(package["ok"], package.get("issues"))


class TestPetManager(unittest.TestCase):
    def test_install_select_and_list_pet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = make_spritesheet_package(root)
            runtime = FakeRuntime()
            manager = PetManager(
                storage_path=root / "state.json",
                library_dir=root / "library",
                runtime=runtime,  # type: ignore[arg-type]
            )

            result = manager.handle_command(f'install "{source}"')
            self.assertTrue(result["changed"])
            self.assertIn("sprite-cat", result["text"])
            self.assertEqual(manager.state()["current_pet_id"], "sprite-cat")
            self.assertEqual(manager.list_pets()[0]["renderer"], "spritesheet")

            result = manager.handle_command("select sprite-cat")
            self.assertTrue(result["changed"])
            self.assertEqual(runtime.sent[-1][0], "pet.load")

    def test_launch_show_hide_and_quit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = FakeRuntime()
            manager = PetManager(
                storage_path=root / "state.json",
                library_dir=root / "library",
                runtime=runtime,  # type: ignore[arg-type]
            )

            self.assertTrue(manager.handle_command("launch")["changed"])
            self.assertTrue(manager.state()["runtime"]["running"])
            self.assertTrue(manager.handle_command("hide")["changed"])
            self.assertFalse(manager.state()["visible"])
            self.assertTrue(manager.handle_command("show")["changed"])
            self.assertTrue(manager.state()["visible"])
            self.assertTrue(manager.handle_command("quit")["changed"])
            self.assertEqual(runtime.shutdowns, 1)

    def test_launch_auto_imports_dropin_pet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dropin = root / "dropin"
            dropin.mkdir()
            make_live2d_package(dropin)
            runtime = FakeRuntime()
            manager = PetManager(
                storage_path=root / "state.json",
                library_dir=root / "library",
                dropin_dirs=[dropin],
                runtime=runtime,  # type: ignore[arg-type]
            )

            result = manager.handle_command("launch")

            self.assertTrue(result["changed"])
            self.assertIn("Auto-imported", result["text"])
            state = manager.state()
            self.assertEqual(state["current_pet_id"], "live2d-cat")
            self.assertEqual(state["current_pet"]["displayName"], "live2d-cat")
            self.assertEqual(runtime.sent[-2][0], "pet.load")

    def test_uninstall_pet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = make_spritesheet_package(root)
            runtime = FakeRuntime()
            manager = PetManager(
                storage_path=root / "state.json",
                library_dir=root / "library",
                runtime=runtime,  # type: ignore[arg-type]
            )

            manager.handle_command(f'install "{source}"')
            result = manager.handle_command("uninstall sprite-cat")

            self.assertTrue(result["changed"])
            self.assertEqual(manager.list_pets(), [])
            self.assertIsNone(manager.state()["current_pet_id"])


class TestPetRuntimeController(unittest.TestCase):
    def test_finds_builtin_python_runtime(self) -> None:
        runtime = PetRuntimeController().find_runtime()

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.name, "pet_runtime.py")

    def test_runtime_log_path_is_project_scoped(self) -> None:
        controller = PetRuntimeController()

        self.assertEqual(
            controller.log_path,
            Path(__file__).resolve().parents[1] / ".cbagent" / "logs" / "system" / "pet-runtime.log",
        )

    def test_launch_uses_python_module_runtime(self) -> None:
        class FakeProc:
            pid = 123
            stdin = None
            _poll = None

            def poll(self):
                return self._poll

            def wait(self, timeout=None):
                self._poll = 0
                return 0

            def terminate(self):
                self._poll = 1

        controller = PetRuntimeController()
        with patch.object(controller, "_dependency_error", return_value=None), \
                patch.object(controller, "_wait_for_startup_locked", return_value=None), \
                patch("agent.pet.subprocess.Popen", return_value=FakeProc()) as popen:
            result = controller.launch()

        self.assertTrue(result["ok"])
        command = popen.call_args.args[0]
        self.assertIn("-m", command)
        self.assertIn("agent.pet_runtime", command)
        controller.shutdown()

    def test_is_running_reaps_closed_runtime_process(self) -> None:
        class FakeProc:
            def poll(self):
                return 0

        class FakeLog:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        controller = PetRuntimeController()
        log = FakeLog()
        controller._proc = FakeProc()  # type: ignore[assignment]
        controller._log_file = log

        self.assertFalse(controller.is_running())
        self.assertIsNone(controller._proc)
        self.assertTrue(log.closed)


class TestPetRuntimePayload(unittest.TestCase):
    def test_live2d_payload_uses_local_asset_server(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = make_live2d_package(Path(td))
            runtime = pet_runtime.PetRuntime()
            try:
                payload = runtime._build_live2d_payload(source)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["renderer"], "live2d")
                self.assertTrue(str(payload["assetBaseUrl"]).startswith("http://127.0.0.1:"))
                self.assertIn("KeyA", payload["keys"])
                self.assertIsNone(payload["background"])

                payload_with_background = runtime._build_live2d_payload(source, {"showBackground": True})
                self.assertIsNotNone(payload_with_background)
                assert payload_with_background is not None
                self.assertIsNotNone(payload_with_background["background"])

                with urllib.request.urlopen(f"{payload['assetBaseUrl']}/cat.moc3", timeout=2) as response:
                    self.assertEqual(response.read(), b"moc")
            finally:
                runtime.shutdown()

    def test_live2d_payload_does_not_auto_transparent_model_parts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = make_live2d_package(Path(td))
            (source / "cat.cdi3.json").write_text(
                json.dumps({
                    "Parameters": [{"Id": "Param", "Name": "透明桌子"}],
                    "Parts": [{"Id": "Part28", "Name": "不透明"}],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            runtime = pet_runtime.PetRuntime()
            try:
                payload = runtime._build_live2d_payload(source)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["transparentParameterIds"], [])
                self.assertEqual(payload["hiddenPartIds"], [])

                auto_payload = runtime._build_live2d_payload(source, {"autoTransparentParameters": True, "autoHideOpaqueParts": True})
                self.assertIsNotNone(auto_payload)
                assert auto_payload is not None
                self.assertEqual(auto_payload["transparentParameterIds"], ["Param"])
                self.assertEqual(auto_payload["hiddenPartIds"], ["Part28"])
            finally:
                runtime.shutdown()

    def test_spritesheet_payload_uses_manifest_and_asset_server(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = make_spritesheet_package(Path(td))
            runtime = pet_runtime.PetRuntime()
            try:
                manifest = json.loads((source / "pet.json").read_text(encoding="utf-8"))
                payload = runtime._build_spritesheet_payload(source, manifest)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["renderer"], "spritesheet")
                self.assertEqual(payload["atlas"]["cellWidth"], 192)
                self.assertEqual(payload["atlas"]["cellHeight"], 208)
                self.assertTrue(str(payload["image"]).startswith("http://127.0.0.1:"))

                with urllib.request.urlopen(payload["image"], timeout=2) as response:
                    self.assertEqual(response.read(), b"png")
            finally:
                runtime.shutdown()

    def test_spritesheet_status_resizes_window_to_cell_aspect(self) -> None:
        class FakeWindow:
            x = 80
            y = 80
            width = 420
            height = 420

            def resize(self, width: int, height: int) -> None:
                self.width = width
                self.height = height

            def move(self, x: int, y: int) -> None:
                self.x = x
                self.y = y

        runtime = pet_runtime.PetRuntime()
        runtime.window = FakeWindow()
        runtime.max_size = 420

        runtime._resize_window_to_status({
            "renderer": "spritesheet",
            "spriteReady": True,
            "spriteCellWidth": 192,
            "spriteCellHeight": 208,
        })

        self.assertEqual(runtime.window.width, 388)
        self.assertEqual(runtime.window.height, 420)

    def test_key_names_match_bongocat_assets(self) -> None:
        self.assertEqual(pet_runtime._key_name(pet_runtime.keyboard.Key.space), "Space")
        self.assertEqual(pet_runtime._key_name(pet_runtime.keyboard.Key.enter), "Return")
        self.assertEqual(pet_runtime._key_name(pet_runtime.keyboard.KeyCode.from_char("a")), "KeyA")
        self.assertEqual(pet_runtime._key_name(pet_runtime.keyboard.KeyCode.from_char("1")), "Num1")

    def test_window_close_cleans_runtime_resources(self) -> None:
        class FakeListener:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        class FakeServer:
            def __init__(self) -> None:
                self.shutdown_called = False
                self.closed = False

            def shutdown(self) -> None:
                self.shutdown_called = True

            def server_close(self) -> None:
                self.closed = True

        runtime = pet_runtime.PetRuntime()
        keyboard_listener = FakeListener()
        mouse_listener = FakeListener()
        server = FakeServer()
        runtime.keyboard_listener = keyboard_listener  # type: ignore[assignment]
        runtime.mouse_listener = mouse_listener  # type: ignore[assignment]
        runtime.asset_server = server  # type: ignore[assignment]

        runtime._on_window_closed()

        self.assertTrue(runtime._closed)
        self.assertTrue(keyboard_listener.stopped)
        self.assertTrue(mouse_listener.stopped)
        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.closed)
        self.assertIsNone(runtime.asset_server)

    def test_mouse_wheel_resizes_when_cursor_is_over_pet_window(self) -> None:
        class FakeWindow:
            x = 80
            y = 80
            width = 420
            height = 420

            def resize(self, width: int, height: int) -> None:
                self.width = width
                self.height = height

            def move(self, x: int, y: int) -> None:
                self.x = x
                self.y = y

        runtime = pet_runtime.PetRuntime()
        runtime.window = FakeWindow()
        runtime.model_size = (1400, 1400)
        runtime.max_size = 420

        with patch.object(runtime, "_persist_max_size"):
            runtime._on_mouse_scroll(100, 100, 0, 1)

        self.assertEqual(runtime.max_size, 460)
        self.assertEqual(runtime.window.width, 460)
        self.assertEqual(runtime.window.height, 460)


class TestPetEventBridge(unittest.TestCase):
    def test_agent_events_update_pet_activity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = FakeRuntime()
            manager = PetManager(
                storage_path=root / "state.json",
                library_dir=root / "library",
                runtime=runtime,  # type: ignore[arg-type]
            )
            bus = EventBus()
            events = collect_all(bus)
            bridge = PetEventBridge(manager)
            bridge.attach(bus)

            bus.emit(RoundStart(round_idx=1, max_rounds=50))
            bus.emit(AskUserQuestion(
                question_id="q1",
                question="Continue?",
                options=[{"label": "Yes", "description": "Continue"}],
            ))

            pet_events = [ev for ev in events if isinstance(ev, PetUpdated)]
            self.assertEqual([ev.state["activity"] for ev in pet_events], ["running", "waiting"])
            self.assertEqual(runtime.sent[-1], ("pet.set_state", {"state": "waiting"}))


if __name__ == "__main__":
    unittest.main()
