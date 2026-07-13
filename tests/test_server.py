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


def test_search_parser_reads_cards_and_ratings():
    html = '''<div class="kino-card" itemscope itemtype="https://schema.org/TVSeries">
      <a class="kino-poster" href="https://kinokrad.my/22460-avatar.html" title="Аватар (2024)">
        <img data-src="/avatar.jpg"><span class="kino-card-title">Аватар <span class="card-title-year">(2024)</span></span>
        <span class="kino-poster-imdb-rting" data-title="IMDb: 7.2"></span>
        <span class="kino-poster-kp-rting" data-title="KP: 7.15"></span>
      </a><span itemprop="description">Описание сериала</span></div>'''
    item = server.parse_search(html)[0]
    assert item["title"] == "Аватар"
    assert item["year"] == "2024"
    assert item["poster"] == "https://kinokrad.my/avatar.jpg"
    assert item["kinopoisk"] == "7.15"
    assert item["media_type"] == "series"


def test_ajax_search_parser_reads_fast_results():
    payload = {"content": '''<a class="search-card" href="https://kinokrad.my/22460-avatar.html">
      <span class="searchheading">Аватар</span><img src="/avatar.jpg">
      <span class="search-card-categorys"><b>Сериал</b>, Фэнтези</span>
      <span class="search-card-country">2024 · США</span>
      <span class="search-card-kp-rting">KP: 7.15</span></a>'''}
    item = server.parse_search_ajax(payload)[0]
    assert item["title"] == "Аватар"
    assert item["year"] == "2024"
    assert item["poster"] == "https://kinokrad.my/avatar.jpg"
    assert item["kinopoisk"] == "7.15"
    assert item["media_type"] == "series"


def test_ajax_search_uses_curl_without_shell():
    original = server.subprocess.run
    called = {}
    try:
        class Result:
            stdout = '{"content":""}'

        def fake_run(args, **kwargs):
            called.update({"args": args, "kwargs": kwargs})
            return Result()

        server.subprocess.run = fake_run
        assert server.search_ajax("тест") == []
        assert called["args"][0] == "curl"
        assert "story=тест" in called["args"]
        assert "shell" not in called["kwargs"]
        assert called["kwargs"]["check"] is True
    finally:
        server.subprocess.run = original


def test_search_api_uses_encoded_kinokrad_url():
    original = server.search_ajax
    try:
        called = []
        server.search_ajax = lambda query: called.append(query) or [{"title": "Тест"}]
        response = server.app.test_client().get("/api/search?q=тест кино")
        assert response.status_code == 200
        assert response.json["count"] == 1
        assert called == ["тест кино"]
    finally:
        server.search_ajax = original


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


def test_player_fetch_waits_for_async_file_list():
    source = open(server.__file__, encoding="utf-8").read()
    assert 'r"\\bfileList\\s*=\\s*JSON\\.parse"' in source
    assert "player metadata timed out" in source


def test_media_allowlist_rejects_arbitrary_hosts():
    assert server.allowed_media_url("https://a.vkvideo.cloud/video.m3u8")
    assert server.allowed_media_url("https://assortedia-as.stravers.live/sub.vtt")
    assert not server.allowed_media_url("https://evil.example/video.m3u8")
    assert not server.allowed_media_url("file:///etc/passwd")


def test_manifest_rewrite_uses_proxy():
    with server.app.test_request_context("/api/master/x/0.m3u8", base_url="http://localhost:5200"):
        text = "#EXTM3U\nsegment.m4s\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
        out = server.rewrite_manifest(text, "https://a.vkvideo.cloud/path/index.m3u8", "stream123")
    assert out.count("http://localhost:5200/api/proxy?url=") == 2
    assert out.count("&key=stream123") == 2


def test_health_and_plugin_routes():
    client = server.app.test_client()
    assert client.get("/api/health").json["version"] == server.APP_VERSION
    plugin = client.get("/plugin.js")
    assert plugin.status_code == 200
    assert b"__BASE_URL__" not in plugin.data


def test_fetch_html_uses_browser_fallback_on_protected_http_error(monkeypatch=None):
    class Forbidden:
        status_code = 404
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
