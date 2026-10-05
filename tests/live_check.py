"""在线自检脚本：验证到 Patreon 的抓取链路是否正常。

用法：
    .venv\\Scripts\\python.exe tests\\live_check.py [创作者] [--download] [--count N]
    .venv\\Scripts\\python.exe tests\\live_check.py <作品链接或作品ID> --post

不登录也能跑（只能看到公开内容）；如果 data/cookies.json 里已有凭证，会自动带上。

    --post       只抓单篇作品（第一个参数填作品链接或作品 ID）
    --download   真的下载少量文件到临时目录，验证下载链路
    --count N    检查前 N 篇作品（默认 5）
    --videos N   下载验证时最多下几个视频（默认 1）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patreon_dl.config import AppConfig
from patreon_dl.cookies import has_session, load_cookies
from patreon_dl.extract import ExtractOptions, filter_by_kind
from patreon_dl.models import Campaign
from patreon_dl.patreon import PatreonClient, parse_reference
from patreon_dl.util import human_size


def resolve_post_id(reference: str) -> str:
    """从作品链接或纯数字里取出作品 ID。"""
    reference = (reference or "").strip()
    if re.fullmatch(r"\d+", reference):
        return reference
    kind, value = parse_reference(reference)
    if kind == "post" and value:
        return value
    raise SystemExit(f"无法从 {reference!r} 里解析出作品 ID，请填作品链接或纯数字 ID")


def print_post(post, index: str = "      ·") -> int:
    """打印一篇作品及其媒体，返回可下载条目数。"""
    config = AppConfig()
    media = filter_by_kind(post.media, config)
    flag = "" if post.can_view else "  [需要订阅]"
    inline = f"  ★正文内嵌 {post.inline_media_total}" if post.inline_media_total else ""
    print(f"{index} {post.date_key} [{post.post_type}] {post.safe_title[:52]}{flag}{inline}")
    print(f"          {post.media_count} 个媒体：{post.count_text()}")
    for item in media[:6]:
        size = human_size(item.display_size) if item.display_size else "-"
        print(f"          - {item.kind_label:<10} {item.filename[:44]:<44} {size:>10}")
    if len(media) > 6:
        print(f"          … 其余 {len(media) - 6} 个")
    return len(media)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patreon 抓取链路自检")
    parser.add_argument("creator", nargs="?", default="rossdraws",
                        help="创作者主页地址 / 名字 / 数字 ID；配合 --post 时填作品链接或 ID")
    parser.add_argument("--post", action="store_true", help="只抓单篇作品")
    parser.add_argument("--download", action="store_true", help="同时验证下载")
    parser.add_argument("--count", type=int, default=5, help="检查多少篇作品")
    parser.add_argument("--videos", type=int, default=1, help="下载验证时最多下几个视频")
    args = parser.parse_args()

    config = AppConfig()
    config.request_delay = 0.5
    client = PatreonClient(config)
    client.log = lambda message: print("   [api]", message)

    cookies = load_cookies()
    if cookies:
        client.set_cookies(cookies)
        print(f"已载入 {len(cookies)} 条本地凭证（含 session_id: {has_session(cookies)}）")
    else:
        print("没有本地凭证，本次只检查公开内容")

    # 步骤编号随模式变化
    steps = (["只抓单篇作品", "检查媒体解析"] if args.post
             else ["解析创作者", "抓取作品列表", "检查媒体解析"])
    if args.download:
        steps.append("下载验证")

    def step(index: int) -> str:
        return f"[{index}/{len(steps)}] {steps[index - 1]}"

    # ---------------------------------------------------------- 1. 定位
    if args.post:
        post_id = resolve_post_id(args.creator)
        print(f"\n{step(1)} {post_id}（只需 1 个请求）…")
        post, campaign = client.fetch_post(post_id)
        if campaign is None:
            campaign = Campaign(id=post.campaign_id or "", name=post.creator or "?")
        print(f"      → {campaign.display_name} (campaign {campaign.id}) "
              f"| 作者 {post.creator or '?'}")
        posts = [post]
        client_note = "（未拉取完整作品列表）"
    else:
        print(f"\n{step(1)} {args.creator!r} …")
        campaign = client.resolve_campaign(args.creator)
        print(f"      → {campaign.display_name}  (campaign {campaign.id}, "
              f"订阅者 {campaign.patron_count})")

        print(f"\n{step(2)} …")
        options = ExtractOptions(image_quality="medium", include_embeds=False)
        page = client.fetch_page(campaign.id, options=options, creator=campaign.vanity)
        print(f"      → 本页 {len(page.posts)} 篇，共 {page.total} 篇，"
              f"下一页游标 {'有' if page.next_cursor else '无'}")
        posts = page.posts
        client_note = ""

    # ---------------------------------------------------------- 媒体解析
    detail_index = 2 if args.post else 3
    print(f"\n{step(detail_index)} 前 {args.count} 篇{client_note} …")
    total_media = 0
    for post in posts[: args.count]:
        total_media += print_post(post)
    print(f"      → 共解析出 {total_media} 个可下载条目")

    if not args.download:
        print("\n（加 --download 可以继续验证下载链路）")
        print("\n✅ 抓取链路正常")
        return 0

    # ---------------------------------------------------------- 下载
    print(f"\n{step(len(steps))} …")
    from patreon_dl.downloader import DownloadEngine, build_tasks, summarize
    from patreon_dl.state import StateStore

    target = tempfile.mkdtemp(prefix="patreon_dl_check_")
    config.output_dir = target
    config.max_workers = 3
    print(f"      临时目录：{target}")

    state = StateStore(os.path.join(target, "state.json"), output_dir=target)
    tasks = build_tasks(config, campaign, posts, state,
                        on_log=lambda m: print("      [tasks]", m))
    images = [t for t in tasks if t.item.kind == "image"][:3]
    videos = [t for t in tasks if t.item.kind == "video"][: max(0, args.videos)]
    picked = images + videos
    if not picked:
        print("      没有可下载的文件，跳过")
        return 0

    engine = DownloadEngine(
        config, state, cookies,
        on_task_done=lambda r: print(f"      {r.status:<8} {r.size:>11} B  "
                                     f"{r.task.filename[:46]} {r.error[:60]}"),
        on_log=lambda m: None,
    )
    results = engine.run(picked)
    summary = summarize(results)
    print(f"      → 新下载 {summary['done']}，跳过 {summary['skipped']}，"
          f"失败 {summary['failed']}，共 {summary['size_text']}")
    for error in summary["errors"][:5]:
        print(f"      ! {error}")
    print("\n✅ 抓取 + 下载链路正常")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
