"""Buddy 宠物系统单测。

这些测试只使用临时 buddy.json，不读写用户真实 ``~/.cbagent/buddy.json``。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.buddy import BuddyManager, RARITIES, SPECIES, STAT_NAMES, roll_with_seed


class TestBuddyGeneration(unittest.TestCase):
    def test_roll_with_seed_is_deterministic(self):
        """同一 seed 必须生成完全相同的宠物骨架。"""
        a = roll_with_seed("seed-for-test")
        b = roll_with_seed("seed-for-test")
        self.assertEqual(a, b)
        self.assertIn(a["species"], SPECIES)
        self.assertIn(a["rarity"], RARITIES)
        self.assertIsInstance(a["shiny"], bool)

    def test_stats_are_in_range(self):
        """五项属性都在 1-100 区间内。"""
        bones = roll_with_seed("stats-seed")
        stats = bones["stats"]
        self.assertEqual(set(stats.keys()), set(STAT_NAMES))
        for value in stats.values():
            self.assertGreaterEqual(value, 1)
            self.assertLessEqual(value, 100)


class TestBuddyManager(unittest.TestCase):
    def test_disabled_without_feature_flag(self):
        """未启用 FEATURE_BUDDY 时返回 disabled，不展示已存储宠物。"""
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            manager = BuddyManager(Path(td) / "buddy.json")
            result = manager.handle_command("hatch")
            self.assertFalse(result["state"]["enabled"])
            self.assertEqual(result["state"]["status"], "disabled")
            self.assertIn("FEATURE_BUDDY=1", result["text"])

    def test_hatch_persists_and_reloads(self):
        """孵化后写入 buddy.json，新 manager 能读回同一只 Buddy。"""
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"FEATURE_BUDDY": "1"}, clear=True):
            path = Path(td) / "buddy.json"
            manager = BuddyManager(path)
            result = manager.handle_command("hatch")

            self.assertTrue(path.exists())
            self.assertTrue(result["changed"])
            companion = result["state"]["companion"]
            self.assertIsNotNone(companion)
            self.assertIn("Buddy 已孵化", result["text"])

            reloaded = BuddyManager(path).state()
            self.assertEqual(reloaded["companion"]["seed"], companion["seed"])
            self.assertEqual(reloaded["companion"]["name"], companion["name"])

    def test_hatch_does_not_replace_without_rehatch(self):
        """已有 Buddy 时 /buddy hatch 不覆盖，/buddy rehatch 才替换。"""
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"FEATURE_BUDDY": "1"}, clear=True):
            manager = BuddyManager(Path(td) / "buddy.json")
            first = manager.handle_command("hatch")["state"]["companion"]["seed"]
            second_result = manager.handle_command("hatch")
            second = second_result["state"]["companion"]["seed"]
            third = manager.handle_command("rehatch")["state"]["companion"]["seed"]

            self.assertEqual(first, second)
            self.assertFalse(second_result["changed"])
            self.assertNotEqual(first, third)

    def test_pet_mute_unmute_update_state(self):
        """pet 会取消静音并设置 pet_at/reaction；mute/unmute 会持久化隐藏状态。"""
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"FEATURE_BUDDY": "1"}, clear=True):
            manager = BuddyManager(Path(td) / "buddy.json")
            manager.handle_command("hatch")

            muted = manager.handle_command("mute")["state"]
            self.assertTrue(muted["muted"])

            petted = manager.handle_command("pet")["state"]
            self.assertFalse(petted["muted"])
            self.assertIsInstance(petted["pet_at"], int)
            self.assertIsInstance(petted["last_reaction"], str)

            unmuted = manager.handle_command("unmute")["state"]
            self.assertFalse(unmuted["muted"])

    def test_maybe_react_respects_basic_state(self):
        """已启用且已有 Buddy 时，本地模板反应会写入最新状态。"""
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"FEATURE_BUDDY": "1"}, clear=True):
            manager = BuddyManager(Path(td) / "buddy.json")
            manager.handle_command("hatch")
            state = manager.maybe_react(user_query="帮我看看代码", assistant_answer="好的")

            self.assertIsNotNone(state)
            self.assertIsInstance(state["last_reaction"], str)
            self.assertIsInstance(state["reaction_at"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
