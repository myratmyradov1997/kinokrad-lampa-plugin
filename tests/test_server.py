import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import server


def test_catalog_parser():
    html = '''<a class="kino-poster" href="https://kinokrad.my/1-film.html">
      <img data-src="/poster.jpg">Тестовый фильм (2026)</a>'''
    item = server.parse_catalog(html)[0]
    assert item["title"] == "Тестовый фильм"
    assert item["year"] == "2026"
    assert item["poster"] == "https://kinokrad.my/poster.jpg"


def test_movie_versions_are_flattened():
    files = {"all": {"theatrical": {"t66": {"WEB-DL": {
        "id": 42, "id_translation": 66, "translation": "Дубляж", "uhd": 1
    }}}}}
    assert server.flatten_movie(files) == [{
        "file_id": 42, "translation_id": 66, "label": "Дубляж",
        "quality": "WEB-DL", "uhd": True, "group": "theatrical",
    }]


def test_series_tree_is_normalized():
    files = {"all": {"2": {"1": {"t93": {
        "id": 99, "id_translation": 93, "translation": "Оригинал", "quality": "WEB-DL"
    }}}}}
    seasons = server.flatten_series(files)
    assert seasons[0]["season"] == 2
    assert seasons[0]["episodes"][0]["episode"] == 1
    assert seasons[0]["episodes"][0]["translations"][0]["file_id"] == 99


def test_player_json_parser():
    payload = {"type": "movie", "active": {"id": 7}}
    html = "<script>const fileList = JSON.parse('" + json.dumps(payload) + "');</script>"
    assert server.parse_player_json(html, "fileList") == payload


def test_media_allowlist_rejects_arbitrary_hosts():
    assert server.allowed_media_url("https://a.vkvideo.cloud/video.m3u8")
    assert server.allowed_media_url("https://assortedia-as.stravers.live/sub.vtt")
    assert not server.allowed_media_url("https://evil.example/video.m3u8")
    assert not server.allowed_media_url("file:///etc/passwd")


def test_manifest_rewrite_uses_proxy():
    with server.app.test_request_context("/api/master/x/0.m3u8", base_url="http://localhost:5200"):
        text = "#EXTM3U\nsegment.m4s\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
        out = server.rewrite_manifest(text, "https://a.vkvideo.cloud/path/index.m3u8")
    assert out.count("http://localhost:5200/api/proxy?url=") == 2


def test_health_and_plugin_routes():
    client = server.app.test_client()
    assert client.get("/api/health").json["version"] == server.APP_VERSION
    plugin = client.get("/plugin.js")
    assert plugin.status_code == 200
    assert b"__BASE_URL__" not in plugin.data


def test_fetch_html_uses_browser_fallback_on_403(monkeypatch=None):
    class Forbidden:
        status_code = 403
        apparent_encoding = "utf-8"
        text = ""

        def raise_for_status(self):
            error = server.requests.HTTPError("forbidden")
            error.response = self
            raise error

    original_get = server.SESSION.get
    original_browser = server.fetch_html_browser
    try:
        server.HTML_CACHE.clear()
        server.SESSION.get = lambda *args, **kwargs: Forbidden()
        server.fetch_html_browser = lambda *args, **kwargs: "<html>browser</html>"
        assert server.fetch_html("https://kinokrad.my/test.html") == "<html>browser</html>"
    finally:
        server.SESSION.get = original_get
        server.fetch_html_browser = original_browser
        server.HTML_CACHE.clear()
