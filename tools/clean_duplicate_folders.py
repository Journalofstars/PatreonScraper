"""找出并清理重复的作品文件夹。

**问题从哪来**

命名规则一旦调整（例如目录名的截断方式变了），同一篇作品按新规则算出的目录名
会和旧的不同。已下载的媒体被判为「已下载」而跳过，**但元数据仍然写进新目录**，
于是磁盘上就多出一个只装着 ``post.json`` / ``post.txt`` 的空壳文件夹，
和原来装着几百 MB 视频的那个并排。

程序侧的修复见 ``downloader.reuse_existing_directory()``：以后会沿用旧目录，
不再分裂。这个脚本负责把**已经产生**的空壳清理掉。

**靠什么识别**

不是靠名字像不像，而是读每个文件夹里 ``post.json`` 记录的作品 ID —— 精确。
同一个作品 ID 下如果有多个文件夹：

* 按媒体文件总大小选出「主目录」（装着真正内容的那个）；
* 其余**不含任何媒体文件**的，视为空壳，可以删；
* 其余若也含媒体，只报告不删（需要人工判断）。

**用法**

    python tools/clean_duplicate_folders.py                 # 只看，不动手
    python tools/clean_duplicate_folders.py --apply         # 真正删除
    python tools/clean_duplicate_folders.py --root "G:/bb"  # 指定下载目录
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from patreon_dl.config import AppConfig  # noqa: E402
from patreon_dl.metadata import read_post_owner  # noqa: E402
from patreon_dl.util import human_size  # noqa: E402

METADATA_NAMES = {"post.json", "post.txt"}


def media_files(folder: str) -> list[str]:
    """列出文件夹里除元数据以外的所有文件（递归）。"""
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            if name in METADATA_NAMES:
                continue
            found.append(os.path.join(dirpath, name))
    return found


def folder_size(paths: list[str]) -> int:
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def collect_groups(root: str) -> dict[str, list[str]]:
    """扫描下载目录，按 post.json 里的作品 ID 分组。"""
    groups: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "post.json" not in filenames:
            continue
        owner = read_post_owner(dirpath)
        if owner:
            groups.setdefault(owner, []).append(dirpath)
    return groups


def looks_like_truncation_change(names: list[str]) -> bool:
    """这组名字看起来像「同一篇作品、截断方式变过」吗？

    截断变化留下的签名很明确：**一个名字是另一个的前缀**
    （``…Blowj`` 与 ``…Blowj_``，``…DUO`` 与 ``…DUO Bunny``）。

    这一条能把 ``split_sections`` 的分段子目录排除掉——它们的名字是
    ``13_Other Episodes …`` / ``10_School Festival …`` 这种，
    彼此不构成前缀关系。
    """
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i != j and b.startswith(a):
                return True
    return False


def group_duplicates(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    """挑出真正的重复文件夹。

    三个条件同时满足才算：

    1. **同一个作品 ID**（读 ``post.json`` 得出，精确）；
    2. **同一个父目录** —— 真正的重复是并排的兄弟目录；``split_sections``
       的分段子目录虽然同 ID，但位于作品根目录**之内**，不算重复；
    3. **名字互为前缀** —— 截断方式变化留下的签名，用它把分段子目录
       （``13_…`` / ``10_…``）进一步排除掉。

    局限：如果模板从 ``{creator}`` 改成 ``{creator}\\{year}``，新旧目录会落在
    不同层级，这里认不出来。宁可漏报也不误删。
    """
    result: dict[str, list[str]] = {}
    for post_id, dirs in groups.items():
        by_parent: dict[str, list[str]] = {}
        for folder in dirs:
            by_parent.setdefault(os.path.dirname(os.path.abspath(folder)), []).append(folder)
        for parent, siblings in by_parent.items():
            if len(siblings) < 2:
                continue
            # 只留下参与「前缀关系」的那些
            names = {d: os.path.basename(d.rstrip("\\/")) for d in siblings}
            keep = [d for d in siblings
                    if any(names[o].startswith(names[d]) or names[d].startswith(names[o])
                           for o in siblings if o != d)]
            if len(keep) > 1:
                result[f"{post_id} @ {parent}"] = sorted(keep)
    return result


def is_content_subset(victim: str, keeper: str) -> bool:
    """``victim`` 里的每个媒体文件，在 ``keeper`` 里都有同名同大小的副本吗？

    有这个保证，删掉 ``victim`` 不会丢任何内容。比「victim 必须完全为空」
    更宽松，能收拾掉「新目录里只补下了一两个文件」这种残留。
    """
    for path in media_files(victim):
        rel = os.path.relpath(path, victim)
        twin = os.path.join(keeper, rel)
        if not os.path.isfile(twin):
            return False
        try:
            if os.path.getsize(twin) != os.path.getsize(path):
                return False
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理重复的作品文件夹（默认只报告，不删除）")
    parser.add_argument("--root", help="下载目录，默认读设置里的 output_dir")
    parser.add_argument("--apply", action="store_true", help="真正执行删除")
    parser.add_argument("--list-all", action="store_true",
                        help="连没有重复的作品也列出来")
    args = parser.parse_args()

    root = args.root
    if not root:
        root = AppConfig.load().output_dir
    root = os.path.abspath(root or "")
    if not root or not os.path.isdir(root):
        print(f"❌ 下载目录不存在：{root!r}")
        return 2

    print(f"扫描：{root}")
    groups = collect_groups(root)
    print(f"共发现 {len(groups)} 篇作品带 post.json")

    duplicates = group_duplicates(groups)
    if args.list_all:
        for pid in sorted(groups):
            print(f"  {pid}: {len(groups[pid])} 个文件夹")
    print(f"其中文件夹重复的：{len(duplicates)} 组")
    if not duplicates:
        print("\n✅ 没有重复文件夹")
        return 0

    removable: list[tuple[str, str, str, int]] = []   # (post_id, keeper, victim, victim_size)
    manual: list[tuple[str, str, list[str]]] = []

    for label, dirs in sorted(duplicates.items()):
        stats = {}
        for d in dirs:
            files = media_files(d)
            stats[d] = (len(files), folder_size(files))
        ranked = sorted(dirs, key=lambda d: (-stats[d][1], -stats[d][0], d))
        keeper = ranked[0]
        for victim in ranked[1:]:
            count, size = stats[victim]
            # 空壳，或者内容在保留目录里都有副本 -> 安全删除
            if count == 0 or is_content_subset(victim, keeper):
                removable.append((label, keeper, victim, size))
            else:
                manual.append((label, keeper, [victim]))

    print()
    print("=" * 78)
    print(f"可以安全删除的空壳文件夹：{len(removable)} 个")
    freed = 0
    for _label, keeper, victim, _size in removable[:200]:
        print(f"  保留  {os.path.relpath(keeper, root)}")
        print(f"  待删  {os.path.relpath(victim, root)}")
        print()

    if manual:
        print("=" * 78)
        print(f"⚠️  {len(manual)} 组里每个文件夹都含媒体，需人工判断，未处理：")
        for label, keeper, victims in manual[:40]:
            print(f"  {label}")
            print(f"     最大  {os.path.relpath(keeper, root)}")
            for v in victims:
                print(f"     其它  {os.path.relpath(v, root)}")

    if not args.apply:
        print()
        print(f"（这是预览。加 --apply 才会真正删除这 {len(removable)} 个空壳文件夹）")
        return 0

    print()
    print(f"开始删除 {len(removable)} 个空壳文件夹 …")
    deleted = failed = 0
    for _pid, _keeper, victim, size in removable:
        try:
            shutil.rmtree(victim)
            deleted += 1
            freed += size
        except OSError as exc:
            failed += 1
            print(f"  ❌ {victim}: {exc}")
    print(f"已删除 {deleted} 个（失败 {failed} 个），释放约 {human_size(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
