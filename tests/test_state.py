"""下载状态判断的单元测试。

核心要求：**判断基准是「下载目录内的相对路径」**。
只要把整个下载目录搬走（例如 G:/bb → E:/bb）并在设置里改成新目录，
目录内的结构没变，就依然算「已下载」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patreon_dl.state import StateStore

CREATOR = "NSFW VTuber Roleplay Videos"
POST = "2026-10-03_Bratty Bunny"
FILE = "01_cover.png"
KEY = "171238686:m756567987"


class StateTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="patreon_state_"))
        self.old_dir = self.root / "bb"
        self.new_dir = self.root / "moved" / "bb"
        self.state_file = self.root / "state.json"
        self.relative = os.path.join(CREATOR, POST, FILE)
        self.old_file = self.old_dir / self.relative
        self.old_file.parent.mkdir(parents=True, exist_ok=True)
        self.old_file.write_bytes(b"x" * 1234)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def move_tree(self):
        """把整个下载目录搬到新位置（内容结构不变）。"""
        self.new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.old_dir), str(self.new_dir))

    def new_file(self) -> Path:
        return self.new_dir / self.relative


class RelativePathTests(StateTestBase):
    def test_mark_records_path_relative_to_output_dir(self):
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        record = store.record_of("c1", KEY)
        self.assertEqual(record["rel"], self.relative)
        self.assertEqual(record["path"], str(self.old_file))
        self.assertTrue(store.is_downloaded("c1", KEY, 1234))

    def test_survives_moving_the_whole_output_dir(self):
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        store.save(force=True)

        self.move_tree()
        # 换到新下载目录，磁盘上的文件完全没动
        store.set_output_dir(str(self.new_dir))

        self.assertTrue(store.is_downloaded("c1", KEY, 1234))
        self.assertEqual(store.resolve_path("c1", KEY), str(self.new_file()))

    def test_reload_from_disk_after_move(self):
        """重启程序、换了目录，也应当认出已下载。"""
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        store.save(force=True)

        self.move_tree()
        reopened = StateStore(self.state_file, output_dir=str(self.new_dir))
        self.assertTrue(reopened.is_downloaded("c1", KEY, 1234))

    def test_missing_file_is_not_downloaded(self):
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        self.old_file.unlink()
        self.assertFalse(store.is_downloaded("c1", KEY, 1234))
        self.assertIsNone(store.resolve_path("c1", KEY))

    def test_size_mismatch_is_not_downloaded(self):
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 999999, "image")
        self.assertFalse(store.is_downloaded("c1", KEY, 999999, verify_size=True))
        # 关掉大小校验就只看文件在不在
        self.assertTrue(store.is_downloaded("c1", KEY, 999999, verify_size=False))

    def test_unknown_key_is_not_downloaded(self):
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        self.assertFalse(store.is_downloaded("c1", "nope", 1))

    def test_files_outside_download_dir_do_not_count(self):
        """只考虑下载目录中的内容：目录外的文件不算已下载。"""
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        outside = self.root / "elsewhere" / "a.png"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"y" * 10)
        store.mark("c1", "k-outside", str(outside), 10, "image")
        record = store.record_of("c1", "k-outside")
        self.assertNotIn("rel", record)                 # 目录外不写相对路径
        self.assertFalse(store.is_downloaded("c1", "k-outside", 10))
        self.assertIsNone(store.resolve_path("c1", "k-outside"))

    def test_old_absolute_path_is_ignored_after_switching_dir(self):
        """换了下载目录后，旧目录里的文件不再算数（即使它还躺在原地）。"""
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        self.assertTrue(store.is_downloaded("c1", KEY, 1234))

        # 只是把「下载目录」这个设置改掉，磁盘上 G:/bb 里的文件还在
        store.set_output_dir(str(self.new_dir))       # new_dir 里什么都没有
        self.assertFalse(store.is_downloaded("c1", KEY, 1234))
        self.assertIsNone(store.resolve_path("c1", KEY))

    def test_without_output_dir_falls_back_to_absolute_path(self):
        """没配置下载目录时（独立使用状态库）仍按绝对路径判断。"""
        store = StateStore(self.state_file, output_dir="")
        store.mark("c1", KEY, str(self.old_file), 1234, "image")
        self.assertTrue(store.is_downloaded("c1", KEY, 1234))


class LegacyRecordTests(StateTestBase):
    """老版本 state.json 只有绝对路径，需要能自动适应。"""

    def write_legacy_state(self):
        payload = {
            "version": 1,
            "campaigns": {
                "c1": {
                    "media": {
                        KEY: {"path": str(self.old_file), "size": 1234,
                              "kind": "image", "at": "2026-01-01T00:00:00"}
                    },
                    "posts": {},
                }
            },
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_legacy_record_still_works_in_place(self):
        self.write_legacy_state()
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        self.assertTrue(store.is_downloaded("c1", KEY, 1234))

    def test_reindex_fills_relative_path(self):
        self.write_legacy_state()
        store = StateStore(self.state_file, output_dir=str(self.old_dir))
        filled = store.reindex()
        self.assertEqual(filled, 1)
        self.assertEqual(store.record_of("c1", KEY)["rel"], self.relative)

    def test_legacy_record_recovers_after_directory_move(self):
        """没跑过 reindex 就换了目录，也要能靠路径尾部找回来。"""
        self.write_legacy_state()
        self.move_tree()
        store = StateStore(self.state_file, output_dir=str(self.new_dir))
        self.assertIsNone(store.record_of("c1", KEY).get("rel"))

        self.assertTrue(store.is_downloaded("c1", KEY, 1234))
        self.assertEqual(store.resolve_path("c1", KEY), str(self.new_file()))
        # 自愈：记录被更新成新位置
        record = store.record_of("c1", KEY)
        self.assertEqual(record["path"], str(self.new_file()))
        self.assertEqual(record["rel"], self.relative)

    def test_suffix_recovery_requires_matching_size(self):
        """后缀兜底是「猜」，大小对不上就不能认。"""
        self.write_legacy_state()
        self.move_tree()
        self.new_file().write_bytes(b"z" * 55)      # 大小变了

        store = StateStore(self.state_file, output_dir=str(self.new_dir))
        self.assertFalse(store.is_downloaded("c1", KEY, 1234))
        self.assertIsNone(store.resolve_path("c1", KEY))

    def test_suffix_recovery_prefers_longest_match(self):
        """同名文件出现在多个层级时，取最贴近原结构的那一个。"""
        self.write_legacy_state()
        self.move_tree()
        # 在新目录里伪造一个更浅的同名文件，大小和原文件一样，
        # 这样只有「优先尝试更长后缀」才能选对
        shallow = self.new_dir / POST / FILE
        shallow.parent.mkdir(parents=True, exist_ok=True)
        shallow.write_bytes(b"q" * 1234)

        store = StateStore(self.state_file, output_dir=str(self.new_dir))
        resolved = store.resolve_path("c1", KEY)
        self.assertEqual(resolved, str(self.new_file()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
