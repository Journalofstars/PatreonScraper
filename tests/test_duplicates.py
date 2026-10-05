"""重复文件夹：目录复用 + 清理工具的判据。

背景：命名规则变化后，同一篇作品按新规则算出的目录名和旧的不同，已下载的
媒体被跳过，**但元数据仍然写进新目录**，磁盘上就多出一个空壳文件夹。
``downloader.reuse_existing_directory()`` 负责不再产生新的，
``tools/clean_duplicate_folders.py`` 负责收拾已经产生的。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from patreon_dl.config import AppConfig
from patreon_dl.downloader import build_tasks, reuse_existing_directory
from patreon_dl.models import Campaign, MediaItem, PostItem
from patreon_dl.state import StateStore

import clean_duplicate_folders as cdf


def write(path: str, payload: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def write_post_json(directory: str, post_id: str) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "post.json"), "w", encoding="utf-8") as handle:
        json.dump({"post": {"id": post_id}}, handle)


class ResolvedDirsTests(unittest.TestCase):
    def test_groups_resolved_media_by_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = StateStore(output_dir=tmp)
            d1 = os.path.join(tmp, "Creator", "Post A")
            d2 = os.path.join(tmp, "Creator", "Post B")
            f1 = write(os.path.join(d1, "a.png"), b"x" * 10)
            f2 = write(os.path.join(d2, "b.png"), b"y" * 20)
            st.mark("1", "100:m1", f1, 10)
            st.mark("1", "100:m2", f2, 20)
            dirs = st.resolved_dirs_for_post("1", "100")
            self.assertEqual(len(dirs), 2)
            self.assertIn(d1, dirs)
            self.assertIn(d2, dirs)

    def test_missing_files_are_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = StateStore(output_dir=tmp)
            st.mark("1", "100:m1", os.path.join(tmp, "gone.png"), 10)
            self.assertEqual(st.resolved_dirs_for_post("1", "100"), [])

    def test_media_keys_of_post_filters_by_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = StateStore(output_dir=tmp)
            st.mark("1", "100:m1", os.path.join(tmp, "a"), 1)
            st.mark("1", "1000:m1", os.path.join(tmp, "b"), 1)
            self.assertEqual(st.media_keys_of_post("1", "100"), ["100:m1"])


class ReuseExistingDirectoryTests(unittest.TestCase):
    """核心回归：命名规则变过之后，不能给同一篇作品分裂出第二个文件夹。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cfg = AppConfig()
        self.cfg.output_dir = self.tmp
        self.cfg.group_by_post = True
        self.cfg.folder_template = "{creator}"
        self.cfg.name_template = "{date}_{title}"
        self.campaign = Campaign(id="1", name="Creator", vanity="creator")
        self.post = PostItem(id="100", title="A Title",
                             published_at="2025-01-01T00:00:00.000+00:00")

    def tearDown(self):
        self._tmp.cleanup()

    def make_old_folder(self, name="2025-01-01_Old Truncated Name") -> str:
        folder = os.path.join(self.tmp, "Creator", name)
        write_post_json(folder, self.post.id)
        return folder

    def test_returns_existing_folder(self):
        old = self.make_old_folder()
        media_path = write(os.path.join(old, "01_a.png"), b"x" * 32)
        st = StateStore(output_dir=self.tmp)
        st.mark("1", "100:m1", media_path, 32)

        reused = reuse_existing_directory(self.cfg, self.campaign, self.post, st)
        self.assertEqual(reused, old)

    def test_returns_none_when_nothing_downloaded(self):
        self.make_old_folder()
        st = StateStore(output_dir=self.tmp)
        self.assertIsNone(
            reuse_existing_directory(self.cfg, self.campaign, self.post, st))

    def test_ignores_folder_owned_by_another_post(self):
        """同名目录属于别的作品时不能复用。"""
        folder = self.make_old_folder()
        write_post_json(folder, "999")            # 换个归属
        media_path = write(os.path.join(folder, "01_a.png"), b"x" * 32)
        st = StateStore(output_dir=self.tmp)
        st.mark("1", "100:m1", media_path, 32)
        self.assertIsNone(
            reuse_existing_directory(self.cfg, self.campaign, self.post, st))

    def test_finds_post_folder_from_section_subdirectory(self):
        """媒体在分段子目录里，也要能往上找到作品根目录。"""
        root = self.make_old_folder()
        section = os.path.join(root, "01_Part One")
        media_path = write(os.path.join(section, "01_a.png"), b"x" * 32)
        st = StateStore(output_dir=self.tmp)
        st.mark("1", "100:m1", media_path, 32)
        self.assertEqual(
            reuse_existing_directory(self.cfg, self.campaign, self.post, st), root)

    def test_ignores_paths_outside_output_dir(self):
        outside = os.path.join(os.path.dirname(self.tmp), "elsewhere", "Post")
        write_post_json(outside, self.post.id)
        media_path = write(os.path.join(outside, "01_a.png"), b"x" * 32)
        st = StateStore(output_dir=self.tmp)
        st.mark("1", "100:m1", media_path, 32)
        self.assertIsNone(
            reuse_existing_directory(self.cfg, self.campaign, self.post, st))

    def test_disabled_when_not_grouping_by_post(self):
        self.cfg.group_by_post = False
        old = self.make_old_folder()
        media_path = write(os.path.join(old, "01_a.png"), b"x" * 32)
        st = StateStore(output_dir=self.tmp)
        st.mark("1", "100:m1", media_path, 32)
        self.assertIsNone(
            reuse_existing_directory(self.cfg, self.campaign, self.post, st))

    def test_none_state_is_safe(self):
        self.assertIsNone(
            reuse_existing_directory(self.cfg, self.campaign, self.post, None))


class BuildTasksReuseTests(unittest.TestCase):
    def test_tasks_land_in_the_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.output_dir = tmp
            cfg.group_by_post = True
            cfg.folder_template = "{creator}"
            cfg.name_template = "{date}_{title}"
            campaign = Campaign(id="1", name="Creator", vanity="creator")
            post = PostItem(
                id="100", title="A Title",
                published_at="2025-01-01T00:00:00.000+00:00",
                media=[MediaItem(url="https://x/01.png", kind="image",
                                 filename="a.png", media_id="m1", size=32,
                                 post_id="100")],
            )
            old = os.path.join(tmp, "Creator", "2025-01-01_Old Truncated Name")
            write_post_json(old, "100")
            media_path = write(os.path.join(old, "01_a.png"), b"x" * 32)
            st = StateStore(output_dir=tmp)
            st.mark("1", "100:m1", media_path, 32)

            tasks = build_tasks(cfg, campaign, [post], st)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(os.path.abspath(tasks[0].dest_dir), os.path.abspath(old))

    def test_tasks_use_fresh_directory_when_nothing_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.output_dir = tmp
            cfg.group_by_post = True
            cfg.folder_template = "{creator}"
            cfg.name_template = "{date}_{title}"
            campaign = Campaign(id="1", name="Creator", vanity="creator")
            post = PostItem(
                id="100", title="A Title",
                published_at="2025-01-01T00:00:00.000+00:00",
                media=[MediaItem(url="https://x/01.png", kind="image",
                                 filename="a.png", media_id="m1", size=32,
                                 post_id="100")],
            )
            st = StateStore(output_dir=tmp)
            tasks = build_tasks(cfg, campaign, [post], st)
            self.assertEqual(len(tasks), 1)
            self.assertIn("2025-01-01_A Title", tasks[0].dest_dir)


class CleanupToolTests(unittest.TestCase):
    def test_group_duplicates_finds_truncation_pair(self):
        groups = {"100": [r"C:\dl\Creator\2025-01-01_Title",
                          r"C:\dl\Creator\2025-01-01_Title Long"]}
        found = cdf.group_duplicates(groups)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(next(iter(found.values()))), 2)

    def test_group_duplicates_ignores_section_subdirectories(self):
        """分段子目录同 ID、同父目录，但名字不互为前缀，不能当成重复。"""
        groups = {"100": [r"C:\dl\C\Post\13_Other Episodes",
                          r"C:\dl\C\Post\10_School Festival"]}
        self.assertEqual(cdf.group_duplicates(groups), {})

    def test_group_duplicates_ignores_different_parents(self):
        groups = {"100": [r"C:\dl\C\Post", r"C:\dl\C\Post\01_Section"]}
        self.assertEqual(cdf.group_duplicates(groups), {})

    def test_group_duplicates_needs_same_post_id(self):
        found = cdf.group_duplicates({
            "100": [r"C:\dl\C\Title"],
            "200": [r"C:\dl\C\Title Long"],
        })
        self.assertEqual(found, {})

    def test_looks_like_truncation_change(self):
        self.assertTrue(cdf.looks_like_truncation_change(["abc", "abcdef"]))
        self.assertTrue(cdf.looks_like_truncation_change(["abcdef", "abc"]))
        self.assertFalse(cdf.looks_like_truncation_change(["abc", "xyz"]))
        self.assertFalse(cdf.looks_like_truncation_change(["13_Other", "10_School"]))

    def test_is_content_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            keeper = os.path.join(tmp, "keep")
            victim = os.path.join(tmp, "victim")
            write(os.path.join(keeper, "01_a.png"), b"x" * 10)
            write(os.path.join(keeper, "02_b.png"), b"y" * 20)
            write(os.path.join(victim, "02_b.png"), b"y" * 20)
            self.assertTrue(cdf.is_content_subset(victim, keeper))

    def test_is_content_subset_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            keeper = os.path.join(tmp, "keep")
            victim = os.path.join(tmp, "victim")
            write(os.path.join(keeper, "01_a.png"), b"x" * 10)
            write(os.path.join(victim, "99_only_here.png"), b"z" * 5)
            self.assertFalse(cdf.is_content_subset(victim, keeper))

    def test_is_content_subset_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            keeper = os.path.join(tmp, "keep")
            victim = os.path.join(tmp, "victim")
            write(os.path.join(keeper, "01_a.png"), b"x" * 10)
            write(os.path.join(victim, "01_a.png"), b"x" * 11)
            self.assertFalse(cdf.is_content_subset(victim, keeper))

    def test_metadata_files_are_not_counted_as_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "post.json"), b"{}")
            write(os.path.join(tmp, "post.txt"), b"hi")
            self.assertEqual(cdf.media_files(tmp), [])

    def test_collect_groups_reads_post_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_post_json(os.path.join(tmp, "C", "A"), "100")
            write_post_json(os.path.join(tmp, "C", "B"), "100")
            write_post_json(os.path.join(tmp, "C", "D"), "200")
            groups = cdf.collect_groups(tmp)
            self.assertEqual(sorted(groups), ["100", "200"])
            self.assertEqual(len(groups["100"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
