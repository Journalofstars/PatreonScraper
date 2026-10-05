"""the-joi-database.com 适配器的离线单元测试。

站点结构见 ``patreon_dl/joi.py`` 开头的说明。这里的 HTML 片段是照着真实
页面抄的（自定义元素 ``<asis-video-thumbnail>`` + 后面的 views/相对时间），
所以解析逻辑的改动能被这些测试挡住。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from patreon_dl.config import AppConfig
from patreon_dl.joi import (
    JOI_BASE,
    SITE,
    entry_to_post,
    is_joi_reference,
    parse_creator,
    parse_detail_date,
    parse_duration,
    parse_joi_reference,
    parse_listing,
    parse_relative_age,
    parse_views,
    stream_url,
)
from patreon_dl.models import Campaign, PostItem
from patreon_dl.naming import media_filename, post_directory
from patreon_dl.patreon import PatreonError

# --------------------------------------------------------------- 测试用片段

BLOCK_A = """
<div class="col-lg-2 justify-content-center video-thumbnail-block" id="video-block-17796">
    <asis-video-thumbnail video-title="[VOICED] Bratty Bunny Edges and Mocks You! | Mean Brat | Handjob"
                          duration="11:43"
                          video-id="fa211fa917ee1e495850e09d"
                          explicit=""
                          is-patreon-exclusive=""
                          thumbnail="https://cdn-s.the-joi-database.com/videos/fa211fa917ee1e495850e09d/thumbnail_fa211fa917ee1e495850e09d.webp?ud=4698fb">
    </asis-video-thumbnail>
    <div class="d-flex justify-content-between" style="min-height: 31px">
        <a href="/watch/fa211fa917ee1e495850e09d" class="text-decoration-none video-title">
            <h6 class="text-white no-margin" title="[VOICED] Bratty Bunny Edges and Mocks You! | Mean Brat | Handjob"><span class="d-none score">0</span>[VOICED] Bratty Bunny Edges and Mocks You! | Mean Brat | Handjob</h6>
        </a>
    </div>
    <p class="text-white text-muted small">2.6K views
&bull; 3 days ago</p>
</div>
"""

BLOCK_B = """
<div class="col-lg-2 justify-content-center video-thumbnail-block" id="video-block-5520">
    <asis-video-thumbnail video-title="Your School Bully Gives You Private Lessons After Hours"
                          duration="55:07"
                          video-id="00aa410b52da86afcf6cfd6a"
                          thumbnail="https://cdn-s.the-joi-database.com/videos/00aa410b52da86afcf6cfd6a/thumbnail_00aa410b52da86afcf6cfd6a.webp?ud=b0ae">
    </asis-video-thumbnail>
    <p class="text-white text-muted small">32,369 views &bull; Aug 08, 2024</p>
</div>
"""

BLOCK_NO_TITLE_ATTR = """
<div class="col-lg-2 justify-content-center video-thumbnail-block" id="video-block-99">
    <asis-video-thumbnail duration="1:00" video-id="aaaaaaaaaaaaaaaaaaaaaaaa"
                          thumbnail="https://cdn-s.the-joi-database.com/x.webp">
    </asis-video-thumbnail>
    <h6 title="从 h6 里取到的标题">从 h6 里取到的标题</h6>
</div>
"""

PROFILE_HTML = f"""
<html><head>
<meta name="title" content="BubbleBebe"/>
<meta name="description" content="Watch BubbleBebe's videos"/>
</head><body>
<img src='https://cdn-s.the-joi-database.com/profile_images/pimage_6f0574ee171ebd1297b801f8.png?ud=982b' alt=""/>
<div class="d-block">
  <h2 class="mb-0" id="user-title">BubbleBebe <button class="btn btn-link">x</button></h2>
  <p class="no-margin text-muted"> <span id="sub_counter">2.5K</span> subscribers </p>
</div>
<div class="row">{BLOCK_A}{BLOCK_B}</div>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<div class="col-md-9">
  <h4 class="font-size-12 font-size-md-15 no-margin">Your School Bully Gives You Private Lessons</h4>
  <h4 class="font-size-12 font-size-md-15"><small class='text-muted'>32,369 Views &bull; Aug 08, 2024</small></h4>
</div>
<video id="player" data-poster="https://cdn-s.the-joi-database.com/t.webp" preload="none"
       data-hls-source="/api/stream/00aa410b52da86afcf6cfd6a"
       data-vtt-source="/api/vtt/00aa410b52da86afcf6cfd6a"></video>
<script type="module">
  initPlayerHelper('Your School Bully Gives You Private Lessons', '3307.85');
</script>
<a href="/profile/6f0574ee171ebd1297b801f8">BubbleBebe</a>
</body></html>
"""


class ReferenceTests(unittest.TestCase):
    def test_profile_url(self):
        for url in ("https://www.the-joi-database.com/profile/6f0574ee171ebd1297b801f8",
                    "https://www.the-joi-database.com/profile/6f0574ee171ebd1297b801f8?sort=new",
                    "http://the-joi-database.com/profile/6f0574ee171ebd1297b801f8"):
            with self.subTest(url=url):
                self.assertEqual(parse_joi_reference(url),
                                 ("profile", "6f0574ee171ebd1297b801f8"))

    def test_watch_url(self):
        self.assertEqual(
            parse_joi_reference("https://www.the-joi-database.com/watch/00aa410b52da86afcf6cfd6a"),
            ("video", "00aa410b52da86afcf6cfd6a"))

    def test_playlist_url(self):
        self.assertEqual(
            parse_joi_reference("https://www.the-joi-database.com/playlist/25501ca46c14f0eca52fbae3"),
            ("playlist", "25501ca46c14f0eca52fbae3"))

    def test_shorthand_and_bare_id(self):
        for text in ("joi:6f0574ee171ebd1297b801f8", "6f0574ee171ebd1297b801f8"):
            with self.subTest(text=text):
                self.assertEqual(parse_joi_reference(text),
                                 ("profile", "6f0574ee171ebd1297b801f8"))

    def test_rejects_patreon_and_garbage(self):
        for text in ("https://www.patreon.com/cw/BBebe", "BBebe", "", "   ", "12345"):
            with self.subTest(text=text):
                with self.assertRaises(PatreonError):
                    parse_joi_reference(text)

    def test_is_joi_reference(self):
        self.assertTrue(is_joi_reference("https://www.the-joi-database.com/profile/x"))
        self.assertTrue(is_joi_reference("joi:abc"))
        self.assertFalse(is_joi_reference("https://www.patreon.com/cw/BBebe"))
        self.assertFalse(is_joi_reference("BBebe"))
        self.assertFalse(is_joi_reference(""))


class ScalarParserTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("11:43"), 703.0)
        self.assertEqual(parse_duration("55:07"), 3307.0)
        self.assertEqual(parse_duration("1:02:03"), 3723.0)
        self.assertEqual(parse_duration("3307.85"), 3307.85)

    def test_duration_invalid(self):
        for value in (None, "", "abc", "1:2:3:4"):
            with self.subTest(value=value):
                self.assertIsNone(parse_duration(value))

    def test_views(self):
        self.assertEqual(parse_views("2.6K views"), 2600)
        self.assertEqual(parse_views("32,369 Views"), 32369)
        self.assertEqual(parse_views("1.5M views"), 1_500_000)
        self.assertEqual(parse_views("812 views"), 812)
        self.assertEqual(parse_views("没有数字"), 0)
        self.assertEqual(parse_views(None), 0)

    def test_relative_age(self):
        self.assertEqual(parse_relative_age("2.6K views • 3 days ago"), "3 days ago")
        self.assertEqual(parse_relative_age("• 2 years ago"), "2 years ago")
        self.assertEqual(parse_relative_age("没有时间"), "")

    def test_detail_date(self):
        self.assertEqual(parse_detail_date("32,369 Views • Aug 08, 2024"),
                         "2024-08-08T00:00:00")
        self.assertEqual(parse_detail_date("1,000 Views • Dec 31, 2025"),
                         "2025-12-31T00:00:00")
        self.assertEqual(parse_detail_date("没有日期"), "")

    def test_stream_url(self):
        self.assertEqual(stream_url("abc"), f"{JOI_BASE}/api/stream/abc")


class ListingParserTests(unittest.TestCase):
    def setUp(self):
        self.entries = parse_listing(PROFILE_HTML)

    def test_reads_all_entries(self):
        self.assertEqual(len(self.entries), 2)

    def test_reads_thumbnail_attributes(self):
        entry = self.entries[0]
        self.assertEqual(entry["video_id"], "fa211fa917ee1e495850e09d")
        self.assertEqual(entry["duration"], 703.0)
        self.assertEqual(entry["duration_text"], "11:43")
        self.assertIn("Bratty Bunny", entry["title"])
        self.assertTrue(entry["thumbnail"].startswith("https://cdn-s."))
        self.assertEqual(entry["page_url"],
                         f"{JOI_BASE}/watch/fa211fa917ee1e495850e09d")

    def test_reads_views_and_age(self):
        self.assertEqual(self.entries[0]["views"], 2600)
        self.assertEqual(self.entries[0]["age"], "3 days ago")
        self.assertEqual(self.entries[1]["views"], 32369)

    def test_boolean_flags(self):
        self.assertTrue(self.entries[0]["explicit"])
        self.assertTrue(self.entries[0]["patreon_exclusive"])
        self.assertFalse(self.entries[1]["explicit"])

    def test_falls_back_to_h6_title(self):
        entries = parse_listing(BLOCK_NO_TITLE_ATTR)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "从 h6 里取到的标题")

    def test_deduplicates_by_video_id(self):
        self.assertEqual(len(parse_listing(BLOCK_A + BLOCK_A)), 1)

    def test_empty_input(self):
        self.assertEqual(parse_listing(""), [])
        self.assertEqual(parse_listing("<html>没有视频</html>"), [])

    def test_unescapes_html_entities_in_title(self):
        html = BLOCK_A.replace("Edges and Mocks You!", "Tom &amp; Jerry &lt;3")
        entries = parse_listing(html)
        self.assertIn("Tom & Jerry <3", entries[0]["title"])

    def test_works_on_playlist_pages_too(self):
        """播放列表页用的是同一套标记，一个解析器通吃。"""
        self.assertEqual(len(parse_listing(f"<div>{BLOCK_A}{BLOCK_B}</div>")), 2)


class CreatorParserTests(unittest.TestCase):
    def test_reads_name_image_and_subscribers(self):
        campaign = parse_creator(PROFILE_HTML, "6f0574ee171ebd1297b801f8")
        self.assertEqual(campaign.display_name, "BubbleBebe")
        self.assertEqual(campaign.id, "6f0574ee171ebd1297b801f8")
        self.assertEqual(campaign.patron_count, 2500)
        self.assertIn("profile_images", campaign.image_url)

    def test_falls_back_to_meta_title(self):
        html = '<html><head><meta name="title" content="Someone"/></head></html>'
        self.assertEqual(parse_creator(html, "x").display_name, "Someone")

    def test_falls_back_to_id(self):
        campaign = parse_creator("<html></html>", "abcdef0123456789")
        self.assertEqual(campaign.display_name, "profile-abcdef0123456789")


class EntryToPostTests(unittest.TestCase):
    def setUp(self):
        self.campaign = Campaign(id="6f0574ee171ebd1297b801f8", name="BubbleBebe",
                                 vanity="BubbleBebe")
        self.entry = parse_listing(PROFILE_HTML)[0]
        self.post = entry_to_post(self.entry, self.campaign)

    def test_post_identity(self):
        self.assertEqual(self.post.id, "fa211fa917ee1e495850e09d")
        self.assertEqual(self.post.site, SITE)
        self.assertEqual(self.post.campaign_id, self.campaign.id)
        self.assertEqual(self.post.media_count, 1)

    def test_media_points_at_stream_endpoint(self):
        media = self.post.media[0]
        self.assertEqual(media.url, stream_url(self.post.id))
        self.assertEqual(media.extra["hls"], media.url)
        self.assertEqual(media.kind, "video")
        self.assertFalse(media.is_preview)

    def test_media_is_marked_as_hls(self):
        """没有 .m3u8 后缀，下载器靠 mimetype / extra 认出来。"""
        media = self.post.media[0]
        self.assertIn("mpegurl", str(media.mimetype).lower())
        self.assertTrue(media.extra.get("hls"))

    def test_filename_uses_title_with_mp4(self):
        self.assertTrue(self.post.media[0].filename.endswith(".mp4"))
        self.assertIn("Bratty Bunny", self.post.media[0].filename)

    def test_media_key_is_stable_across_runs(self):
        """key 只由站点内的 ID 决定，不含签名，所以增量同步有效。"""
        again = entry_to_post(parse_listing(PROFILE_HTML)[0], self.campaign)
        self.assertEqual(self.post.media[0].key, again.media[0].key)
        self.assertEqual(self.post.media[0].key, f"{self.post.id}:m{self.post.id}")

    def test_no_publish_date(self):
        """列表页没有可靠日期 -> 留空，避免文件夹名随运行时间漂移。"""
        self.assertEqual(self.post.published_at, "")
        self.assertEqual(self.post.date_key, "")

    def test_keeps_display_metadata(self):
        extra = self.post.media[0].extra
        self.assertEqual(extra["views"], 2600)
        self.assertEqual(extra["age"], "3 days ago")
        self.assertEqual(extra["site"], SITE)

    def test_referer_is_the_site(self):
        self.assertTrue(self.post.media[0].referer.startswith(JOI_BASE))


class NamingWithMissingDateTests(unittest.TestCase):
    """没有日期时，``{date}_{title}`` 不能留下前导下划线。"""

    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.output_dir = r"C:\dl"
        self.cfg.folder_template = "{creator}"
        self.cfg.name_template = "{date}_{title}"
        self.cfg.file_template = "{index:02d}_{name}"
        self.campaign = Campaign(id="p", name="BubbleBebe", vanity="BubbleBebe")

    def test_folder_name_has_no_leading_separator(self):
        post = PostItem(id="abc", site=SITE, title="A Video Title")
        name = os.path.basename(post_directory(self.cfg, self.campaign, post))
        self.assertEqual(name, "A Video Title")

    def test_patreon_post_with_date_unchanged(self):
        post = PostItem(id="1", title="T", published_at="2026-08-02T10:00:00")
        name = os.path.basename(post_directory(self.cfg, self.campaign, post))
        self.assertEqual(name, "2026-08-02_T")

    def test_inner_underscores_are_preserved(self):
        """只清理首尾分隔符，标题里自带的 _ 不能动。"""
        post = PostItem(id="abc", site=SITE, title="_Leading and_trailing_")
        name = os.path.basename(post_directory(self.cfg, self.campaign, post))
        self.assertIn("and_trailing", name)
        self.assertFalse(name.startswith("_"))

    def test_filename_template(self):
        post = PostItem(id="abc", site=SITE, title="A Video Title")
        post.media = [entry_to_post(parse_listing(PROFILE_HTML)[0], self.campaign).media[0]]
        name = media_filename(self.cfg, self.campaign, post, post.media[0], 1)
        self.assertTrue(name.startswith("01_"))
        self.assertTrue(name.endswith(".mp4"))

    def test_date_key_is_empty_without_date(self):
        self.assertEqual(PostItem(id="1").date_key, "")
        self.assertEqual(PostItem(id="1", published_at="2026-01-02T00:00:00").date_key,
                         "2026-01-02")


if __name__ == "__main__":
    unittest.main(verbosity=2)
