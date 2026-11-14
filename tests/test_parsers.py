import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import parsers as P


def test_catalog_parser():
    html = '''<a class="kino-poster" href="https://kinokrad.my/1-film.html">
      <img data-src="/poster.jpg">Тестовый фильм (2026)</a>'''
    item = P.parse_catalog(html)[0]
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
    item = P.parse_search(html)[0]
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
    item = P.parse_search_ajax(payload)[0]
    assert item["title"] == "Аватар"
    assert item["year"] == "2024"
    assert item["poster"] == "https://kinokrad.my/avatar.jpg"
    assert item["kinopoisk"] == "7.15"
    assert item["media_type"] == "series"


def test_movie_versions_are_flattened():
    files = {"all": {"theatrical": {"t66": {"WEB-DL": {
        "id": 42, "id_translation": 66, "translation": "Дубляж", "uhd": 1
    }}}}}
    assert P.flatten_movie(files) == [{
        "file_id": 42, "translation_id": 66, "label": "Дубляж",
        "quality": "WEB-DL", "uhd": True, "group": "theatrical",
    }]


def test_series_tree_is_normalized():
    files = {"all": {"2": {"1": {"t93": {
        "id": 99, "id_translation": 93, "translation": "Оригинал", "quality": "WEB-DL"
    }}}}}
    seasons = P.flatten_series(files)
    assert seasons[0]["season"] == 2
    assert seasons[0]["episodes"][0]["episode"] == 1
    assert seasons[0]["episodes"][0]["translations"][0]["file_id"] == 99
    assert seasons[0]["episodes"][0]["translations"][0]["season"] == 2
    assert seasons[0]["episodes"][0]["translations"][0]["episode"] == 1


def test_player_json_parser():
    payload = {"type": "movie", "active": {"id": 7}}
    html = "<script>const fileList = JSON.parse('" + json.dumps(payload) + "');</script>"
    assert P.parse_player_json(html, "fileList") == payload


def test_media_allowlist_rejects_arbitrary_hosts():
    assert P.allowed_media_url("https://a.vkvideo.cloud/video.m3u8")
    assert P.allowed_media_url("https://assortedia-as.stravers.live/sub.vtt")
    assert not P.allowed_media_url("https://evil.example/video.m3u8")
    assert not P.allowed_media_url("file:///etc/passwd")



def test_player_host_check_is_suffix_safe():
    assert P.is_player_url("https://Assortedia-as.stravers.live/?token=1")
    assert P.is_player_url("https://other-host.stravers.live/x")
    assert not P.is_player_url("https://stravers.live.evil.example/x")
    assert not P.is_player_url("http://assortedia-as.stravers.live/x")
    assert not P.allowed_media_url("https://vkvideo.cloud.evil.example/seg.m4s")


def test_dle_hash_is_extracted_from_home_page():
    assert P.extract_dle_hash("var dle_login_hash = 'de4c95119c4d23c5f3bcb1e11367ec2df5707b27';") == \
        "de4c95119c4d23c5f3bcb1e11367ec2df5707b27"
    assert P.extract_dle_hash("<html></html>") == ""


def test_empty_search_does_not_return_hot_releases():
    html = '''<div id="dle-content" class="error-container"><h1>Горячие новинки</h1>
    <div class="kino-card"><a class="kino-poster" href="/123-film.html"></a>
    <h2 class="kino-card-title">Посторонний фильм</h2></div></div>'''
    assert P.parse_search(html) == []
