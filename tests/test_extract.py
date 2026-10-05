"""离线单元测试：媒体提取、命名模板、Cookie 解析。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patreon_dl.config import AppConfig
from patreon_dl.cookies import parse_cookie_header, merge_cookies, has_session
from patreon_dl.extract import (
    ExtractOptions,
    build_included_index,
    extract_post,
    filter_by_kind,
)
from patreon_dl.models import Campaign, PostItem
from patreon_dl.naming import creator_directory, media_filename, post_directory
from patreon_dl.patreon import parse_reference
from patreon_dl.state import StateStore
from patreon_dl.util import sanitize_filename, stable_url_key


def make_jwt(payload: dict) -> str:
    """构造一个只有 payload 有意义的假 JWT（程序只解 payload，不验签名）。"""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesignature"


VIDEO_TOKEN = make_jwt({"sub": "PLAYBACK1", "exp": 9999999999, "aud": "v"})
IMAGE_TOKEN = make_jwt({"sub": "PLAYBACK1", "exp": 9999999999, "aud": "t"})
STORYBOARD_TOKEN = make_jwt({"sub": "PLAYBACK1", "exp": 9999999999, "aud": "s"})
# Patreon 对受保护视频签发的令牌：带 playback_restriction_id，
# mux 不为它提供任何静态 MP4（highest.mp4 会返回 404 MP4 does not exist）
RESTRICTED_TOKEN = make_jwt({
    "sub": "PLAYBACK1", "exp": 9999999999, "aud": "v",
    "playback_restriction_id": "Ir02FmqsqUnMIpEqH89MdlvWa15QwOo6dCnelCtSI9YI",
})


def mux_mp4(token: str) -> str:
    return f"https://stream.mux.com/PLAYBACK1/highest.mp4?token={token}"


def mux_m3u8(token: str) -> str:
    return f"https://stream.mux.com/PLAYBACK1.m3u8?token={token}"


def media_object(media_id: str, **attrs) -> dict:
    return {"type": "media", "id": media_id, "attributes": attrs}


def post_object(post_id: str, relationships: dict, included: list, **attrs) -> dict:
    base = {
        "title": "Test Post",
        "published_at": "2025-05-29T00:07:59.000+00:00",
        "post_type": "image_file",
        "url": f"https://www.patreon.com/posts/test-{post_id}",
        "current_user_can_view": True,
        "is_paid": False,
    }
    base.update(attrs)
    return {
        "type": "post",
        "id": post_id,
        "attributes": base,
        "relationships": relationships,
    }


def link(media_id: str) -> dict:
    return {"data": [{"type": "media", "id": media_id}]}


class ExtractImageTests(unittest.TestCase):
    def test_prefers_original_image_url(self):
        media = media_object(
            "1",
            media_type="image",
            mimetype="image/jpeg",
            file_name="naga sfw.jpg",
            size_bytes=1234,
            image_urls={
                "url": "https://c10.patreonusercontent.com/4/a/default.jpg?token-hash=x",
                "original": "https://c10.patreonusercontent.com/4/a/original.jpg?token-hash=y",
                "default_large": "https://c10.patreonusercontent.com/4/a/large.jpg?token-hash=z",
            },
        )
        payload_post = post_object("100", {"images": link("1"), "media": link("1")}, [media])
        post = extract_post(
            payload_post, build_included_index({"included": [media]}),
            ExtractOptions(image_quality="original"),
        )
        self.assertEqual(len(post.media), 1)
        item = post.media[0]
        self.assertEqual(item.kind, "image")
        self.assertIn("original.jpg", item.url)
        self.assertEqual(item.filename, "naga sfw.jpg")
        self.assertEqual(item.size, 1234)
        self.assertEqual(item.media_id, "1")

    def test_medium_quality_switches_url(self):
        media = media_object(
            "1", media_type="image", mimetype="image/jpeg",
            image_urls={"original": "https://c10.patreonusercontent.com/o/orig.jpg",
                        "default": "https://c10.patreonusercontent.com/o/mid.jpg"},
        )
        post = extract_post(
            post_object("100", {"images": link("1")}, [media]),
            build_included_index({"included": [media]}),
            ExtractOptions(image_quality="medium"),
        )
        self.assertIn("mid.jpg", post.media[0].url)

    def test_duplicate_thumbnail_is_deduplicated(self):
        media = media_object(
            "1", media_type="image", mimetype="image/jpeg",
            image_urls={"url": "https://c10.patreonusercontent.com/p/1.jpg"},
        )
        post = extract_post(
            post_object(
                "100", {"images": link("1"), "media": link("1")}, [media],
                image={"url": "https://c10.patreonusercontent.com/p/1.jpg"},
            ),
            build_included_index({"included": [media]}),
        )
        self.assertEqual(len(post.media), 1)


class ExtractVideoTests(unittest.TestCase):
    def test_hls_download_url_becomes_full_mux_video(self):
        media = media_object(
            "77", media_type="video", mimetype="application/x-mpegURL",
            download_url=f"https://stream.mux.com/PLAYBACK1/highest.mp4?token={VIDEO_TOKEN}",
        )
        post = extract_post(
            post_object("200", {"media": link("77")}, [],
                        post_type="video_external_file"),
            build_included_index({"included": [media]}),
        )
        self.assertEqual(len(post.media), 1)
        item = post.media[0]
        self.assertEqual(item.kind, "video")
        self.assertIn("highest.mp4", item.url)
        self.assertFalse(item.is_preview)
        self.assertTrue(item.extra.get("fallbacks"))

    def test_video_preview_m3u8_upgrades_to_full_video(self):
        """只有 video_preview（aud=v）时也应推导出完整版视频。"""
        attrs_extra = {
            "video_preview": {
                "duration": 30.0,
                "full_content_duration": 733.7,
                "url": f"https://stream.mux.com/PLAYBACK1.m3u8?token={VIDEO_TOKEN}",
            }
        }
        post = extract_post(
            post_object("201", {}, [], post_type="video_external_file", **attrs_extra),
            {},
            ExtractOptions(prefer_mux_full=True),
        )
        videos = [m for m in post.media if m.kind == "video"]
        self.assertEqual(len(videos), 1)
        self.assertIn("highest.mp4", videos[0].url)
        self.assertFalse(videos[0].is_preview)

    def test_image_thumbnail_token_is_not_used_for_video(self):
        """image.mux.com 的封面令牌 aud='t'，不能拿去请求视频（会 403）。"""
        attrs_extra = {
            "image": {"url": f"https://image.mux.com/PLAYBACK1/thumbnail.jpg?token={IMAGE_TOKEN}"}
        }
        post = extract_post(
            post_object("202", {}, [], post_type="video_external_file", **attrs_extra),
            {},
            ExtractOptions(prefer_mux_full=True),
        )
        self.assertEqual([m for m in post.media if m.kind == "video"], [])
        self.assertEqual([m for m in post.media if m.kind == "image"][0].kind, "image")

    def test_30s_preview_is_skipped_unless_enabled(self):
        attrs_extra = {
            "video_preview": {
                "duration": 30.0,
                "url": f"https://stream.mux.com/PLAYBACK1.m3u8?token={VIDEO_TOKEN}",
            }
        }
        post = extract_post(
            post_object("203", {}, [], **attrs_extra),
            {},
            ExtractOptions(prefer_mux_full=False, include_previews=False),
        )
        self.assertEqual(post.media, [])

        post2 = extract_post(
            post_object("203", {}, [], **attrs_extra),
            {},
            ExtractOptions(prefer_mux_full=False, include_previews=True),
        )
        self.assertEqual(len(post2.media), 1)
        self.assertTrue(post2.media[0].is_preview)

    def test_direct_mp4_preview_flagged(self):
        media = media_object(
            "88", media_type="video", mimetype="video/mp4",
            file_name="Grasp - video_preview.mp4", size_bytes=881166,
            download_url="https://c10.patreonusercontent.com/p/1.mp4?token-hash=a",
        )
        post = extract_post(
            post_object("204", {"media": link("88")}, []),
            build_included_index({"included": [media]}),
            ExtractOptions(include_previews=False),
        )
        self.assertEqual(post.media, [])
        post2 = extract_post(
            post_object("204", {"media": link("88")}, []),
            build_included_index({"included": [media]}),
            ExtractOptions(include_previews=True),
        )
        self.assertEqual(len(post2.media), 1)
        self.assertTrue(post2.media[0].is_preview)

    # ------------------------------------------------ 受保护的 mux 播放（DRM）
    def test_drm_restricted_playback_uses_hls_not_static_mp4(self):
        """带 playback_restriction_id 的播放没有静态 MP4，必须走 HLS。"""
        media = media_object(
            "77", media_type="video", mimetype="application/x-mpegURL",
            download_url=None,
        )
        attrs_extra = {
            "post_file": {"url": mux_m3u8(RESTRICTED_TOKEN),
                          "duration": 735.6, "full_content_duration": 735.6}
        }
        post = extract_post(
            post_object("205", {"media": link("77")}, [media],
                        post_type="video_external_file", **attrs_extra),
            build_included_index({"included": [media]}),
        )
        videos = [m for m in post.media if m.kind == "video"]
        self.assertEqual(len(videos), 1)
        item = videos[0]
        self.assertIn(".m3u8", item.url)
        self.assertNotIn("highest.mp4", item.url)
        self.assertEqual(item.mimetype, "video/mp4")   # 拼接/封装后仍是 mp4
        self.assertEqual(item.duration, 735.6)
        self.assertNotIn("highest.mp4", " ".join(item.extra.get("fallbacks") or []))

    def test_normal_mux_prefers_mp4_and_keeps_hls_as_last_resort(self):
        media = media_object(
            "78", media_type="video", mimetype="application/x-mpegURL",
            download_url=mux_mp4(VIDEO_TOKEN),
        )
        post = extract_post(
            post_object("206", {"media": link("78")}, []),
            build_included_index({"included": [media]}),
        )
        item = [m for m in post.media if m.kind == "video"][0]
        self.assertIn("highest.mp4", item.url)
        fallbacks = item.extra.get("fallbacks") or []
        self.assertTrue(fallbacks)
        self.assertIn(".m3u8", fallbacks[-1])          # 最后兜底才是 HLS

    def test_post_file_m3u8_becomes_full_video(self):
        """新版 Patreon 把完整视频放在 post_file.url（无 name）。"""
        attrs_extra = {
            "post_file": {"url": mux_m3u8(VIDEO_TOKEN), "duration": 735.6,
                          "full_content_duration": 735.6},
        }
        post = extract_post(
            post_object("207", {}, [], post_type="video_external_file", **attrs_extra),
            {},
        )
        videos = [m for m in post.media if m.kind == "video"]
        self.assertEqual(len(videos), 1)
        self.assertIn("highest.mp4", videos[0].url)
        self.assertEqual(videos[0].duration, 735.6)
        # 静态 MP4 取不到时要能退回 HLS
        self.assertIn(".m3u8", (videos[0].extra.get("fallbacks") or [])[-1])

    def test_mux_non_video_urls_are_ignored(self):
        """字幕、故事板、封面图的 mux 地址不能被当成视频。"""
        attrs_extra = {
            "post_file": {
                "url": f"https://image.mux.com/PLAYBACK1/thumbnail.jpg?token={IMAGE_TOKEN}",
                "storyboard": {
                    "vtt_url": f"https://image.mux.com/PLAYBACK1/storyboard.vtt?token={STORYBOARD_TOKEN}"
                },
                "transcript_url": f"https://stream.mux.com/PLAYBACK1/text/abc.vtt?token={VIDEO_TOKEN}",
            }
        }
        post = extract_post(
            post_object("208", {}, [], post_type="video_external_file", **attrs_extra),
            {},
        )
        self.assertEqual([m for m in post.media if m.kind == "video"], [])

    def test_hls_media_without_download_url_yields_nothing_on_its_own(self):
        """只有 m3u8 媒体对象、没有 download_url 时，不应凭它猜地址。"""
        media = media_object("79", media_type="video", mimetype="application/x-mpegURL",
                             download_url=None, image_urls=None)
        post = extract_post(
            post_object("209", {"media": link("79")}, [media]),
            build_included_index({"included": [media]}),
        )
        self.assertEqual(post.media, [])


class ExtractOtherTests(unittest.TestCase):
    def test_attachment_from_post_file(self):
        post = extract_post(
            post_object(
                "300", {}, [], post_type="attachment_file",
                post_file={"url": "https://cdn.patreon.com/f/art.psd", "name": "art.psd"},
            ),
            {},
        )
        self.assertEqual(len(post.media), 1)
        self.assertEqual(post.media[0].kind, "attachment")
        self.assertEqual(post.media[0].filename, "art.psd")

    def test_post_file_without_name_is_not_treated_as_attachment(self):
        post = extract_post(
            post_object(
                "301", {}, [], post_type="image_file",
                post_file={"url": "https://www.patreon.com/media-u/v3/123", "width": 320},
            ),
            {},
        )
        self.assertFalse([m for m in post.media if m.kind == "attachment"])

    def test_inline_content_images_are_collected(self):
        content = (
            '<p>hello</p><img src="https://c10.patreonusercontent.com/4/inline/1.png?a=b">'
            '<a href="https://cdn.patreon.com/files/pack.zip">pack</a>'
        )
        post = extract_post(
            post_object("302", {}, [], content=content),
            {},
        )
        urls = [m.url for m in post.media]
        self.assertTrue(any("inline/1.png" in u for u in urls))
        self.assertTrue(any("pack.zip" in u for u in urls))

    def test_audio_media(self):
        media = media_object(
            "9", media_type="audio", mimetype="audio/mpeg",
            file_name="episode.mp3", download_url="https://cdn.patreon.com/a/ep.mp3",
        )
        post = extract_post(
            post_object("303", {"audio": link("9")}, [media]),
            build_included_index({"included": [media]}),
        )
        self.assertEqual(post.media[0].kind, "audio")
        self.assertEqual(post.media[0].filename, "episode.mp3")

    def test_unknown_media_object_does_not_crash(self):
        post = extract_post(
            post_object("304", {"media": link("404")}, []),
            {},
        )
        self.assertEqual(post.media, [])

    def test_filter_by_kind(self):
        from patreon_dl.models import MediaItem

        items = [
            MediaItem(url="https://x/1.jpg", kind="image"),
            MediaItem(url="https://x/2.mp4", kind="video"),
            MediaItem(url="https://x/3.mp4", kind="video", is_preview=True),
            MediaItem(url="https://x/4.mp3", kind="audio"),
            MediaItem(url="https://x/5.zip", kind="attachment"),
        ]
        cfg = AppConfig()
        cfg.download_images = True
        cfg.download_videos = True
        cfg.download_audio = False
        cfg.download_attachments = False
        cfg.download_previews = False
        kinds = [i.kind for i in filter_by_kind(items, cfg)]
        self.assertEqual(kinds, ["image", "video"])

        cfg.download_previews = True
        kinds = [i.kind for i in filter_by_kind(items, cfg)]
        self.assertEqual(kinds, ["image", "video", "video"])


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.output_dir = r"C:\dl"
        self.cfg.folder_template = "{creator}"
        self.cfg.name_template = "{date}_{title}"
        self.cfg.file_template = "{index:02d}_{name}"
        self.campaign = Campaign(id="1", name="My Creator", vanity="mycreator")
        self.post = PostItem(
            id="555", title='Nikkie: "Naga" <pinup> set',
            published_at="2025-05-29T00:07:59.000+00:00", campaign_id="1",
        )

    def test_post_directory_sanitizes(self):
        path = post_directory(self.cfg, self.campaign, self.post)
        self.assertTrue(path.startswith(r"C:\dl"))
        self.assertIn("My Creator", path)
        self.assertNotIn("<", path)
        self.assertNotIn('"', path)
        self.assertIn("2025-05-29", path)

    def test_directory_collision_gets_post_id(self):
        path = post_directory(self.cfg, self.campaign, self.post, existing_owner="999")
        self.assertIn("[555]", path)

    def test_media_filename_template(self):
        from patreon_dl.models import MediaItem

        item = MediaItem(url="https://x/a.jpg", kind="image", filename="photo.jpg")
        name = media_filename(self.cfg, self.campaign, self.post, item, 3)
        self.assertEqual(name, "03_photo.jpg")

    def test_template_with_unknown_field_falls_back(self):
        self.cfg.file_template = "{index:02d}_{nope}_{name}"
        from patreon_dl.models import MediaItem

        item = MediaItem(url="https://x/a.jpg", kind="image", filename="photo.jpg")
        name = media_filename(self.cfg, self.campaign, self.post, item, 1)
        self.assertTrue(name.endswith(".jpg"))

    # ------------------------------------------------- 多层目录模板
    def test_nested_directory_template_creates_real_subfolders(self):
        """{year}/{month}/… 必须真的分出多层，而不是被安全化成一个名字。"""
        self.cfg.name_template = "{year}/{month}/{date}_{title}"
        path = post_directory(self.cfg, self.campaign, self.post)
        rel = os.path.relpath(path, self.cfg.output_dir)
        parts = rel.split(os.sep)
        self.assertEqual(parts[0], "My Creator")
        self.assertEqual(parts[1], "2025")
        self.assertEqual(parts[2], "05")
        self.assertIn("2025-05-29", parts[3])
        self.assertNotIn("_", parts[1])          # 没有被拼成 "2025_05_..."

    def test_backslash_and_slash_are_equivalent(self):
        self.cfg.name_template = "{year}/{month}"
        with_slash = post_directory(self.cfg, self.campaign, self.post)
        self.cfg.name_template = "{year}\\{month}"
        with_backslash = post_directory(self.cfg, self.campaign, self.post)
        self.assertEqual(with_slash, with_backslash)

    def test_multi_level_creator_directory(self):
        self.cfg.folder_template = "{vanity}/{campaign_id}"
        path = creator_directory(self.cfg, self.campaign)
        rel = os.path.relpath(path, self.cfg.output_dir)
        self.assertEqual(rel.split(os.sep), ["mycreator", "1"])

    def test_section_directory_can_be_nested(self):
        from patreon_dl.naming import section_directory

        self.cfg.section_template = "{index:02d}/{title}"
        rel = section_directory(self.cfg, self.campaign, self.post, "Part A", 2)
        self.assertEqual(rel.split(os.sep), ["02", "Part A"])

    def test_templates_cannot_escape_output_dir(self):
        """模板里的 .. 不能把文件写到下载目录外面去。"""
        root = os.path.abspath(self.cfg.output_dir)
        for template in ("../../evil", "..\\..\\evil", "{year}/../../x", ".."):
            with self.subTest(template=template):
                self.cfg.name_template = template
                path = os.path.abspath(post_directory(self.cfg, self.campaign, self.post))
                self.assertTrue(path.startswith(root), path)
                self.cfg.folder_template = template
                path = os.path.abspath(creator_directory(self.cfg, self.campaign))
                self.assertTrue(path.startswith(root), path)
                self.cfg.folder_template = "{creator}"

    def test_filename_template_never_creates_folders(self):
        from patreon_dl.models import MediaItem

        item = MediaItem(url="https://x/a.jpg", kind="image", filename="sub/dir/photo.jpg")
        name = media_filename(self.cfg, self.campaign, self.post, item, 1)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)


class UtilTests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(sanitize_filename("  trailing.  "), "trailing")
        self.assertEqual(sanitize_filename(""), "untitled")
        self.assertTrue(sanitize_filename("x" * 500).startswith("x"))

    def test_stable_url_key_ignores_token(self):
        a = "https://c10.patreonusercontent.com/4/p/1.jpg?token-hash=AAA&token-time=1"
        b = "https://c10.patreonusercontent.com/4/p/1.jpg?token-hash=BBB&token-time=2"
        self.assertEqual(stable_url_key(a), stable_url_key(b))
        c = "https://c10.patreonusercontent.com/4/p/2.jpg?token-hash=AAA"
        self.assertNotEqual(stable_url_key(a), stable_url_key(c))


class ReferenceTests(unittest.TestCase):
    def test_parse_reference(self):
        cases = {
            "sakimichan": ("vanity", "sakimichan"),
            "@sakimichan": ("vanity", "sakimichan"),
            "122089": ("campaign_id", "122089"),
            "https://www.patreon.com/sakimichan": ("vanity", "sakimichan"),
            "https://www.patreon.com/c/rossdraws": ("vanity", "rossdraws"),
            "https://www.patreon.com/posts/nikkie-goddess-130137332": ("post", "130137332"),
            "https://www.patreon.com/user?u=371321": ("user_id", "371321"),
            # Patreon 新版创作者主页前缀 /cw/
            "https://www.patreon.com/cw/BBebe": ("vanity", "BBebe"),
            "https://www.patreon.com/cw/BBebe/": ("vanity", "BBebe"),
            "patreon.com/cw/BBebe": ("vanity", "BBebe"),
            "https://www.patreon.com/cw/BBebe/posts/xxx-170630196": ("post", "170630196"),
            # 组合前缀
            "https://www.patreon.com/c/cw/Someone": ("vanity", "Someone"),
            # Patreon「分享」按钮生成的链接（带一堆 utm 参数）
            "https://www.patreon.com/BBebe/posts/exclusive-videos-141949666"
            "?utm_medium=clipboard_copy&utm_source=copyLink"
            "&utm_campaign=postshare_creator&utm_content=join_link": ("post", "141949666"),
            # slug 里带数字，只能取最后一段数字
            "https://www.patreon.com/x/posts/day-2-of-30-141949666": ("post", "141949666"),
            # 合集（注意接口路径是单数 collection）
            "https://www.patreon.com/collection/2084280?view=expanded": ("collection", "2084280"),
            "https://www.patreon.com/collection/2084280": ("collection", "2084280"),
            "patreon.com/collection/2084280": ("collection", "2084280"),
            # 合集列表页
            "https://www.patreon.com/cw/BBebe/collections": ("collections", "BBebe"),
            "https://www.patreon.com/BBebe/collections": ("collections", "BBebe"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_reference(raw), expected)

    def test_vanity_candidates_include_fallbacks(self):
        from patreon_dl.patreon import parse_reference_all

        kind, values = parse_reference_all("https://www.patreon.com/cw/BBebe")
        self.assertEqual(kind, "vanity")
        self.assertEqual(values[0], "BBebe")
        self.assertIn("cw", values)          # 兜底候选
        self.assertNotIn("BBebe", values[1:])  # 不重复

        kind, values = parse_reference_all("https://www.patreon.com/c/rossdraws")
        self.assertEqual(values[0], "rossdraws")
        self.assertIn("c", values)

    def test_non_vanity_prefixes_are_known(self):
        from patreon_dl.patreon import NON_VANITY_SEGMENTS

        for token in ("c", "cw", "user", "posts", "login"):
            self.assertIn(token, NON_VANITY_SEGMENTS)
        # 真正的创作者名不能被误判成前缀
        for token in ("BBebe", "sakimichan", "rossdraws"):
            self.assertNotIn(token.lower(), NON_VANITY_SEGMENTS)


class CookieTests(unittest.TestCase):
    def test_parse_cookie_header(self):
        records = parse_cookie_header("session_id=abc123; patreon_device_id=dev")
        names = {r.name: r.value for r in records}
        self.assertEqual(names["session_id"], "abc123")
        self.assertEqual(names["patreon_device_id"], "dev")
        self.assertTrue(has_session(records))

    def test_parse_full_header_and_newlines(self):
        text = "Cookie: a=1; b=2\r\nc=3"
        records = parse_cookie_header(text)
        self.assertEqual(len(records), 3)

    def test_merge_prefers_new(self):
        old = parse_cookie_header("session_id=old; x=1")
        new = parse_cookie_header("session_id=new")
        merged = {r.name: r.value for r in merge_cookies(old, new)}
        self.assertEqual(merged["session_id"], "new")
        self.assertEqual(merged["x"], "1")


class SinglePostModeTests(unittest.TestCase):
    """「只抓这一篇」用到的辅助逻辑（不联网）。"""

    def campaign_object(self, **attrs):
        base = {
            "creation_name": "NSFW VTuber Roleplay Videos",
            "vanity": "BBebe",
            "url": "https://www.patreon.com/cw/BBebe",
            "patron_count": 2554,
            "image_url": "https://c10.patreonusercontent.com/x.jpg",
        }
        base.update(attrs)
        return {("campaign", "15006767"): {"type": "campaign", "id": "15006767",
                                           "attributes": base}}

    def test_campaign_built_from_included_without_extra_request(self):
        from patreon_dl.patreon import PatreonClient

        campaign = PatreonClient._campaign_from_included(
            self.campaign_object(), "15006767", "BubbleBebe"
        )
        self.assertIsNotNone(campaign)
        self.assertEqual(campaign.id, "15006767")
        self.assertEqual(campaign.name, "NSFW VTuber Roleplay Videos")
        self.assertEqual(campaign.vanity, "BBebe")
        self.assertEqual(campaign.patron_count, 2554)
        self.assertEqual(campaign.url, "https://www.patreon.com/cw/BBebe")

    def test_campaign_falls_back_to_author_name(self):
        from patreon_dl.patreon import PatreonClient

        campaign = PatreonClient._campaign_from_included({}, "15006767", "BubbleBebe")
        self.assertEqual(campaign.id, "15006767")
        self.assertEqual(campaign.name, "BubbleBebe")
        self.assertEqual(campaign.vanity, "BubbleBebe")

    def test_campaign_is_none_without_id(self):
        from patreon_dl.patreon import PatreonClient

        self.assertIsNone(PatreonClient._campaign_from_included({}, "", "X"))

    def test_single_post_still_uses_normal_naming(self):
        """单篇模式下载出来的目录结构要和全量模式一致。"""
        from patreon_dl.naming import post_directory

        cfg = AppConfig()
        cfg.output_dir = r"E:\bb"
        cfg.folder_template = "{creator}"
        cfg.name_template = "{date}_{title}"
        campaign = Campaign(id="15006767", name="NSFW VTuber Roleplay Videos", vanity="BBebe")
        post = PostItem(
            id="141949666",
            title="[Exclusive Videos ] List of all old videos",
            published_at="2025-10-24T11:06:00.000+00:00",
            url="https://www.patreon.com/BBebe/posts/exclusive-videos-141949666",
        )
        path = post_directory(cfg, campaign, post)
        self.assertTrue(path.startswith(r"E:\bb"))
        self.assertIn("NSFW VTuber Roleplay Videos", path)
        self.assertIn("2025-10-24", path)


class RichContentTests(unittest.TestCase):
    """新版富文本正文（content_json_string）里的内嵌媒体。

    真实案例：一篇「索引贴」在正文里嵌了 21 个视频，它们**不在** relationships 里，
    只能靠解析正文拿到 media_id，再逐个取回。
    """

    def rich_doc(self, *nodes):
        return json.dumps({"type": "doc", "content": list(nodes)}, ensure_ascii=False)

    def test_extracts_inline_video_ids(self):
        from patreon_dl.extract import parse_rich_content

        raw = self.rich_doc(
            {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]},
            {"type": "video", "attrs": {"fallback_strategy": "fallback", "media_id": "739888749"}},
            {"type": "video", "attrs": {"fallback_strategy": "fallback", "media_id": "739893732"}},
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "T"}]},
        )
        rich = parse_rich_content(raw)
        self.assertEqual(rich.media_ids, ["739888749", "739893732"])
        self.assertEqual(rich.node_counts.get("video"), 2)
        self.assertIn("hello", rich.text)

    def test_deduplicates_repeated_media_ids(self):
        from patreon_dl.extract import parse_rich_content

        raw = self.rich_doc(
            {"type": "video", "attrs": {"media_id": "1"}},
            {"type": "video", "attrs": {"media_id": "1"}},
        )
        self.assertEqual(parse_rich_content(raw).media_ids, ["1"])

    def test_collects_links_and_broken_json_is_safe(self):
        from patreon_dl.extract import parse_rich_content

        raw = self.rich_doc({
            "type": "paragraph",
            "content": [{
                "type": "text", "text": "see",
                "marks": [{"type": "link", "attrs": {"href": "https://www.patreon.com/posts/x-123"}}],
            }],
        })
        self.assertEqual(parse_rich_content(raw).links, ["https://www.patreon.com/posts/x-123"])
        self.assertEqual(parse_rich_content("这不是 JSON").media_ids, [])
        self.assertEqual(parse_rich_content(None).media_ids, [])
        self.assertEqual(parse_rich_content("").media_ids, [])

    def test_inline_media_resolved_into_post(self):
        """传入 resolver 后，正文内嵌媒体应被补进媒体清单。"""
        raw = self.rich_doc(
            {"type": "video", "attrs": {"media_id": "9001"}},
            {"type": "video", "attrs": {"media_id": "9002"}},
        )
        media_objects = {
            "9001": media_object("9001", media_type="video", mimetype="application/x-mpegURL",
                                 file_name="A EX.mp4", size_bytes=850293884,
                                 download_url=mux_mp4(RESTRICTED_TOKEN)),
            "9002": media_object("9002", media_type="video", mimetype="application/x-mpegURL",
                                 file_name="B EX.mp4", size_bytes=1055410965,
                                 download_url=mux_mp4(RESTRICTED_TOKEN)),
        }

        def resolver(media_id: str):
            return media_objects.get(media_id)

        post = extract_post(
            post_object("141949666", {}, [], post_type="image_file",
                        content=None, content_json_string=raw),
            {},
            media_resolver=resolver,
        )
        videos = [m for m in post.media if m.kind == "video"]
        self.assertEqual(len(videos), 2)
        self.assertEqual([v.filename for v in videos], ["A EX.mp4", "B EX.mp4"])
        self.assertEqual(post.inline_media_total, 2)
        # 受保护播放 -> 走 HLS
        self.assertTrue(all(".m3u8" in v.url for v in videos))
        # 接口报告的大小只用于显示，不参与「已下载」校验
        self.assertIsNone(videos[0].size)
        self.assertEqual(videos[0].display_size, 850293884)

    def test_missing_inline_media_is_tolerated(self):
        from patreon_dl.extract import parse_rich_content

        raw = self.rich_doc({"type": "video", "attrs": {"media_id": "404"}})
        post = extract_post(
            post_object("2", {}, [], content_json_string=raw),
            {},
            media_resolver=lambda _mid: None,          # 取不到
        )
        self.assertEqual(post.media, [])
        self.assertEqual(post.inline_media_total, 1)
        # resolver 抛异常也不能影响整篇
        post2 = extract_post(
            post_object("3", {}, [], content_json_string=raw),
            {},
            media_resolver=lambda _mid: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self.assertEqual(post2.media, [])
        self.assertEqual(parse_rich_content(raw).media_ids, ["404"])

    def test_rich_text_used_as_post_summary_when_content_empty(self):
        raw = self.rich_doc({"type": "paragraph",
                             "content": [{"type": "text", "text": "正文内容在这里"}]})
        post = extract_post(
            post_object("4", {}, [], content=None, content_json_string=raw), {},
        )
        self.assertIn("正文内容在这里", post.summary)

    def test_rich_text_media_affects_post_total_size(self):
        raw = self.rich_doc({"type": "video", "attrs": {"media_id": "9001"}})
        obj = media_object("9001", media_type="video", mimetype="application/x-mpegURL",
                           file_name="A.mp4", size_bytes=100 * 1024 * 1024,
                           download_url=mux_mp4(RESTRICTED_TOKEN))
        post = extract_post(
            post_object("5", {}, [], content_json_string=raw),
            {},
            media_resolver=lambda _mid: obj,
        )
        self.assertEqual(post.total_size(), 100 * 1024 * 1024)


class SectionSplitTests(unittest.TestCase):
    """帖子内含多个部分时，按正文标题分组（每部分一个子目录）。

    真实案例：一篇「索引贴」里 21 个视频，每个视频前面用 heading 或 blockquote
    写着标题；还有一层分类标题（h1/h2）需要用层级路径区分同名的「Part 1」。
    """

    def rich(self, *children):
        return json.dumps({"type": "doc", "content": list(children)},
                          ensure_ascii=False)

    def video(self, media_id):
        return {"type": "video", "attrs": {"media_id": str(media_id)}}

    def heading(self, text, level=2):
        node = {"type": "heading", "attrs": {"level": level}}
        if text:
            node["content"] = [{"type": "text", "text": text}]
        return node

    def quote(self, text):
        return {"type": "blockquote",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    def parse(self, raw):
        from patreon_dl.extract import parse_rich_content

        return parse_rich_content(raw)

    def test_blockquote_before_video_becomes_section(self):
        rich = self.parse(self.rich(
            self.quote("Video A title"),
            self.video("1"),
            self.quote("Video B title"),
            self.video("2"),
        ))
        self.assertEqual(rich.section_titles, ["Video A title", "Video B title"])
        self.assertEqual(rich.media_sections["1"], "Video A title")
        self.assertEqual(rich.media_sections["2"], "Video B title")

    def test_heading_hierarchy_disambiguates_duplicate_titles(self):
        rich = self.parse(self.rich(
            self.heading("Bully Series Arcs", 2),
            self.heading("Part 1 [HJ]", 3),
            self.video("1"),
            self.heading("Part 2 [FJ]", 3),
            self.video("2"),
            self.heading("School Festival Arc", 1),
            self.heading("Part 1 [HJ]", 3),
            self.video("3"),
        ))
        self.assertEqual(rich.media_sections["1"], "Bully Series Arcs - Part 1 [HJ]")
        self.assertEqual(rich.media_sections["2"], "Bully Series Arcs - Part 2 [FJ]")
        # 同样是 "Part 1"，靠上级标题区分开
        self.assertEqual(rich.media_sections["3"], "School Festival Arc - Part 1 [HJ]")
        self.assertNotEqual(rich.media_sections["1"], rich.media_sections["3"])

    def test_level_1_heading_resets_deeper_levels(self):
        rich = self.parse(self.rich(
            self.heading("Series A", 2),
            self.heading("Part 1", 3),
            self.video("1"),
            self.heading("Series B", 1),
            self.heading("Part 1", 3),
            self.video("2"),
        ))
        self.assertNotIn("Series A", rich.media_sections["2"])
        self.assertTrue(rich.media_sections["2"].startswith("Series B"))

    def test_missing_title_falls_back_to_empty_section(self):
        rich = self.parse(self.rich(self.video("1"), self.video("2")))
        self.assertEqual(rich.media_sections, {})
        self.assertEqual(rich.section_titles, [])
        self.assertEqual(rich.media_ids, ["1", "2"])

    def test_sections_reach_media_items(self):
        raw = self.rich(
            self.quote("Part A"),
            self.video("9001"),
            self.quote("Part B"),
            self.video("9002"),
        )
        objects = {
            "9001": media_object("9001", media_type="video",
                                 mimetype="application/x-mpegURL", file_name="A.mp4",
                                 download_url=mux_mp4(RESTRICTED_TOKEN)),
            "9002": media_object("9002", media_type="video",
                                 mimetype="application/x-mpegURL", file_name="B.mp4",
                                 download_url=mux_mp4(RESTRICTED_TOKEN)),
        }
        post = extract_post(
            post_object("900", {}, [], content_json_string=raw), {},
            media_resolver=lambda mid: objects.get(mid),
        )
        self.assertEqual([m.section for m in post.media], ["Part A", "Part B"])
        self.assertEqual(post.distinct_sections, ["Part A", "Part B"])

    def test_build_tasks_splits_only_when_enabled_and_multiple_sections(self):
        from patreon_dl.downloader import build_tasks
        from patreon_dl.models import Campaign, MediaItem

        campaign = Campaign(id="1", name="Creator", vanity="creator")
        post = PostItem(id="55", title="Index post",
                        published_at="2025-10-24T11:06:00.000+00:00",
                        campaign_id="1")
        post.media = [
            MediaItem(url="https://x/1.mp4", kind="video", filename="a.mp4",
                      post_id="55", media_id="1", section="Part A"),
            MediaItem(url="https://x/2.mp4", kind="video", filename="b.mp4",
                      post_id="55", media_id="2", section="Part B"),
            MediaItem(url="https://x/cover.png", kind="image", filename="cover.png",
                      post_id="55", media_id="3"),
        ]

        cfg = AppConfig()
        cfg.output_dir = r"C:\dl"
        state = StateStore(Path(tempfile.mkdtemp()) / "s.json", output_dir=cfg.output_dir)

        cfg.split_sections = False
        flat = build_tasks(cfg, campaign, [post], state)
        self.assertEqual(len({t.dest_dir for t in flat}), 1)

        cfg.split_sections = True
        split = build_tasks(cfg, campaign, [post], state)
        dirs = {t.dest_dir for t in split}
        self.assertEqual(len(dirs), 3)          # 两个部分 + 无部分的封面留在作品根目录
        names = sorted(os.path.basename(d) for d in dirs)
        self.assertIn("01_Part A", names)
        self.assertIn("02_Part B", names)
        # 每个部分目录里的文件从 01 重新编号
        for task in split:
            if task.item.section:
                self.assertTrue(task.filename.startswith("01_"), task.filename)

    def test_single_section_post_is_not_split(self):
        from patreon_dl.downloader import build_tasks
        from patreon_dl.models import Campaign, MediaItem

        campaign = Campaign(id="1", name="Creator", vanity="creator")
        post = PostItem(id="56", title="Only one part",
                        published_at="2025-10-24T11:06:00.000+00:00", campaign_id="1")
        post.media = [
            MediaItem(url="https://x/1.mp4", kind="video", filename="a.mp4",
                      post_id="56", media_id="1", section="Part A"),
            MediaItem(url="https://x/2.mp4", kind="video", filename="b.mp4",
                      post_id="56", media_id="2", section="Part A"),
        ]
        cfg = AppConfig()
        cfg.output_dir = r"C:\dl"
        cfg.split_sections = True
        state = StateStore(Path(tempfile.mkdtemp()) / "s.json", output_dir=cfg.output_dir)
        tasks = build_tasks(cfg, campaign, [post], state)
        self.assertEqual(len({t.dest_dir for t in tasks}), 1)

    def test_section_directory_sanitizes_and_truncates_on_word_boundary(self):
        from patreon_dl.naming import section_directory
        from patreon_dl.models import Campaign

        cfg = AppConfig()
        campaign = Campaign(id="1", name="C", vanity="c")
        post = PostItem(id="1", title="t")
        title = ("Your Bratty Girlfriend Loves Bondage ( Bondage | Brat | Creampie ) "
                 "Voiced Vtuber Roleplay - Extra Long Suffix Here")
        name = section_directory(cfg, campaign, post, title, 3)
        self.assertTrue(name.startswith("03_"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertLessEqual(len(name), 90)
        # 不应该出现半截单词（截断前最后一个字符不是字母中间）
        self.assertFalse(name.rstrip("_").endswith("Vt"))


class CollectionTests(unittest.TestCase):
    """合集（Collections）。

    接口是 ``GET /api/collection/{id}``（**单数**），复数形式 404；
    列出一个创作者的全部合集用 ``GET /api/collection?filter[campaign_id]=…``。
    """

    def collection_object(self, **attrs):
        base = {
            "title": "| Multiple Character Videos |",
            "description": "",
            "num_posts": 3,
            "num_draft_posts": 0,
            "num_scheduled_posts": 0,
            "post_sort_type": "custom",
            "post_ids": [165658323, 164828421, 162979279],
            "created_at": "2026-03-31T18:54:02.000+00:00",
            "thumbnail": {"original": "https://c10.patreonusercontent.com/a.jpg",
                          "default": "https://c10.patreonusercontent.com/b.jpg"},
        }
        base.update(attrs)
        return {"type": "collection", "id": "2084280", "attributes": base}

    def test_collection_model_from_api_object(self):
        from patreon_dl.patreon import PatreonClient

        coll = PatreonClient._collection_from(self.collection_object(), "15006767")
        self.assertEqual(coll.id, "2084280")
        self.assertEqual(coll.title, "| Multiple Character Videos |")
        self.assertEqual(coll.post_count, 3)
        self.assertEqual(coll.campaign_id, "15006767")
        self.assertEqual(coll.sort_type, "custom")
        self.assertEqual(coll.post_ids, ["165658323", "164828421", "162979279"])
        self.assertTrue(coll.thumbnail.endswith("b.jpg"))   # 优先 default

    def test_collection_menu_label_includes_count(self):
        from patreon_dl.patreon import PatreonClient

        coll = PatreonClient._collection_from(self.collection_object(), "1")
        self.assertIn("Multiple Character Videos", coll.menu_label)
        self.assertIn("3 篇", coll.menu_label)

    def test_collection_without_title_gets_fallback_name(self):
        from patreon_dl.patreon import PatreonClient
        from patreon_dl.models import Collection

        coll = PatreonClient._collection_from(self.collection_object(title=""), "1")
        self.assertEqual(coll.display_name, "合集 2084280")
        self.assertEqual(Collection(id="9").display_name, "合集 9")

    def test_collection_tolerates_missing_attributes(self):
        from patreon_dl.patreon import PatreonClient

        coll = PatreonClient._collection_from({"type": "collection", "id": "7"}, "1")
        self.assertEqual(coll.id, "7")
        self.assertEqual(coll.post_count, 0)
        self.assertEqual(coll.post_ids, [])
        self.assertEqual(coll.thumbnail, "")

    def test_collection_to_dict_roundtrip(self):
        from patreon_dl.patreon import PatreonClient

        coll = PatreonClient._collection_from(self.collection_object(), "15006767")
        payload = coll.to_dict()
        self.assertEqual(payload["id"], "2084280")
        self.assertEqual(payload["post_count"], 3)
        self.assertEqual(len(payload["post_ids"]), 3)

    def test_collection_posts_reuse_post_extraction(self):
        """合集接口返回的作品，用同一套提取逻辑处理。"""
        post_obj = post_object(
            "165658323", {"images": link("553324771")}, [],
            title="A video post", post_type="video_external_file",
            content_json_string=self_rich_video_json(),
        )
        media_obj = media_object(
            "553324771", media_type="image", mimetype="image/jpeg",
            file_name="cover.jpg",
            image_urls={"original": "https://c10.patreonusercontent.com/c.jpg"},
        )
        post = extract_post(
            post_obj,
            build_included_index({"included": [media_obj]}),
        )
        self.assertEqual(post.id, "165658323")
        self.assertEqual(len(post.media), 1)
        self.assertEqual(post.media[0].filename, "cover.jpg")


def self_rich_video_json():
    return json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
