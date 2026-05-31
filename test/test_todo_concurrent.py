"""TodoStore 并发安全单测。

验证加锁修复：N 个线程并发 write/merge 不会丢条目。
"""

from __future__ import annotations

import sys
import threading
import unittest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tools.tools.todo_tool import TodoStore


class TestTodoStoreSequential(unittest.TestCase):
    def test_write_replace(self):
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "task 1"},
            {"id": "2", "content": "task 2"},
        ])
        self.assertEqual(len(result), 2)
        self.assertEqual(store.read()[0]["id"], "1")

    def test_write_merge_appends_new(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "a"}])
        store.write([{"id": "2", "content": "b"}], merge=True)
        items = store.read()
        self.assertEqual(len(items), 2)
        self.assertEqual({i["id"] for i in items}, {"1", "2"})

    def test_write_merge_updates_existing(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "a", "status": "pending"}])
        store.write([{"id": "1", "status": "completed"}], merge=True)
        items = store.read()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "completed")

    def test_read_returns_copy(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "a"}])
        snapshot = store.read()
        snapshot[0]["content"] = "mutated"
        # 不影响内部
        self.assertEqual(store.read()[0]["content"], "a")

    def test_format_for_injection_empty(self):
        store = TodoStore()
        self.assertIsNone(store.format_for_injection())

    def test_format_for_injection_with_active(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "task 1", "status": "pending"},
            {"id": "2", "content": "done", "status": "completed"},
            {"id": "3", "content": "now", "status": "in_progress"},
        ])
        text = store.format_for_injection()
        self.assertIsNotNone(text)
        # completed 不出现在注入文本里
        self.assertIn("task 1", text)
        self.assertIn("now", text)
        self.assertNotIn("done", text)


class TestTodoStoreConcurrent(unittest.TestCase):
    def test_concurrent_merge_appends_all(self):
        """N 个线程并发 merge 写入，最终条目数应等于 N（无丢失）。

        无锁版本下，这个测试在多次运行中至少有一次能稳定丢条目。
        加锁后必须 100% 稳定通过。
        """
        store = TodoStore()
        N_THREADS = 16
        ITEMS_PER_THREAD = 50

        def worker(tid):
            for i in range(ITEMS_PER_THREAD):
                store.write(
                    [{"id": f"t{tid}-{i}", "content": f"thread {tid} item {i}"}],
                    merge=True,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        items = store.read()
        # 全部 N*ITEMS 个条目都应在
        self.assertEqual(len(items), N_THREADS * ITEMS_PER_THREAD)
        # 每个 id 都唯一存在
        ids = {item["id"] for item in items}
        self.assertEqual(len(ids), N_THREADS * ITEMS_PER_THREAD)

    def test_concurrent_read_during_write(self):
        """边写边读：读到的快照应始终是合法状态（不会读到半截 dict）。"""
        store = TodoStore()
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    store.write(
                        [{"id": f"k{i}", "content": f"c{i}"}],
                        merge=True,
                    )
                except Exception as e:  # noqa: BLE001
                    errors.append(("write", e))
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    snap = store.read()
                    # 每个条目都必须有完整三字段
                    for item in snap:
                        assert "id" in item and "content" in item and "status" in item
                except Exception as e:  # noqa: BLE001
                    errors.append(("read", e))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()

        # 跑 200ms 让竞态出现
        threading.Event().wait(0.2)
        stop.set()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"出现错误: {errors}")

    def test_concurrent_merge_status_update(self):
        """两个线程对同一 id 并发 merge 改 status，最终状态应是最后写入的之一，无损坏。"""
        store = TodoStore()
        store.write([{"id": "x", "content": "initial", "status": "pending"}])

        def to_completed():
            for _ in range(100):
                store.write([{"id": "x", "status": "completed"}], merge=True)

        def to_inprogress():
            for _ in range(100):
                store.write([{"id": "x", "status": "in_progress"}], merge=True)

        t1 = threading.Thread(target=to_completed)
        t2 = threading.Thread(target=to_inprogress)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        items = store.read()
        self.assertEqual(len(items), 1)
        # 最终状态必须是合法值之一
        self.assertIn(items[0]["status"], {"completed", "in_progress"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
