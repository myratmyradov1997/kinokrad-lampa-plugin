import asyncio
import os
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import server  # noqa: E402

EMBED = "https://assortedia-as.stravers.live/?token_movie=abc&token=def"
PAGE = "https://kinokrad.my/1227-interstellar.html"


# ---- выбор карточки и аудиодорожки ---------------------------------------

def test_pick_best_prefers_exact_title_and_year():
    items = [
        {"title": "Менталистка", "year": "2026", "media_type": "series", "url": "a"},
        {"title": "Менталист", "year": "2010", "media_type": "series", "url": "b"},
    ]
    best, confident = server.pick_best(items, "Менталист", "The Mentalist", "2008", "series")
    assert best["url"] == "b" and confident


def test_pick_best_is_not_confident_for_ambiguous_titles():
    items = [{"title": "Дюна", "year": "2021", "url": "a"}, {"title": "Дюна", "year": "1984", "url": "b"}]
    best, confident = server.pick_best(items, "Дюна", "", "", "")
    assert not confident


def test_match_audio_by_name_and_alias():
    audios = [{"label": "(Russian) Dub ATMOSPHERE"}, {"label": "(Russian) MVO Омикрон"},
              {"label": "(Russian) AVO Ю Живов"}, {"label": "(English) Original"}]
    assert server.match_audio(audios, "Живов") == 2
    assert server.match_audio(audios, "Профессиональный многоголосый") == 1
    assert server.match_audio(audios, "Дублированный") == 0
    assert server.match_audio(audios, "Оригинальный") == 3
    assert server.match_audio(audios, "LostFilm") is None
    assert server.match_audio([{"label": "1"}], "что угодно") == 0


def test_stream_payload_builds_quality_map_and_defaults():
    entry = {"key": "k1", "file_id": 5, "embed": EMBED, "data": {"hlsSource": [
        {"label": "(eng) DD", "quality": {"720": "https://a.vkvideo.cloud/1.m3u8", "1080": "https://a.vkvideo.cloud/2.m3u8"}},
        {"label": "(eng) DD", "quality": {"360": "https://evil.example/3.m3u8"}},
    ], "tracks": [{"src": "https://a.vkvideo.cloud/s.vtt", "label": "ru"}, {"src": "https://evil.example/x.vtt"}]}}
    payload = server.stream_payload(entry, "http://lampa.local:5200", "Дублированный")
    audio = payload["audios"][0]
    assert list(audio["quality"]) == ["1080p", "720p"]
    assert audio["url"] == "http://lampa.local:5200/hls/k1/0/1080.m3u8"
    assert len(payload["audios"]) == 1          # дорожка без разрешённых URL отброшена
    assert len(payload["tracks"]) == 1          # субтитры с чужого хоста отброшены
    assert payload["default_audio"] == 0


# ---- кэши и прокси --------------------------------------------------------

def test_ttl_cache_expires_and_limits_size():
    cache = server.TTLCache(ttl=100, maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None and cache.get("c") == 3
    cache.set("d", 4, ttl=-1)
    assert cache.get("d") is None


def test_byte_lru_evicts_by_size():
    lru = server.ByteLRU(10)
    lru.put("a", b"12345", "video/mp4")
    lru.put("b", b"12345", "video/mp4")
    lru.put("c", b"1", "video/mp4")
    assert lru.get("a") is None and lru.get("c") is not None


def test_rewrite_routes_allowed_urls_and_records_segment_order():
    media = server.MediaProxy(server.StreamStore())
    text = ("#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.0,\nseg-1.m4s\n#EXTINF:6.0,\nseg-2.m4s\n"
            "#EXTINF:6.0,\nhttps://evil.example/x.m4s\n")
    out = media.rewrite(text, "https://a.vkvideo.cloud/v/index.m3u8", "http://lampa.local:5200", "k1")
    assert out.count("http://lampa.local:5200/p/k1?u=") == 3
    assert "https://evil.example/x.m4s" in out
    segments, index = media.position["https://a.vkvideo.cloud/v/seg-1.m4s"]
    assert index == 0 and segments[1] == "https://a.vkvideo.cloud/v/seg-2.m4s"
    assert server.b64d(server.b64e("https://a.vkvideo.cloud/v/seg-1.m4s?x=1")) == "https://a.vkvideo.cloud/v/seg-1.m4s?x=1"


def test_cached_segment_supports_range():
    response = server.MediaProxy._serve_bytes(b"0123456789", "video/mp4", "bytes=2-5")
    assert response.status == 206 and response.body == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert server.MediaProxy._serve_bytes(b"0123456789", "video/mp4", "bytes=-3").body == b"789"
    assert server.MediaProxy._serve_bytes(b"0123", "video/mp4", None).status == 200


def test_sibling_reuse_requires_same_version_and_a_matching_track():
    store = server.StreamStore()
    store.remember_target(EMBED, 1, {"tid": 66, "quality": "BDRip", "group": "theatrical"})
    store.put(EMBED, 1, {"hlsSource": [{"label": "(Russian) Dub"}, {"label": "(Russian) AVO Ю Живов"}]})
    same = {"tid": 68, "quality": "BDRip", "group": "theatrical"}
    assert store.sibling_with_audio(EMBED, same, "Живов")["file_id"] == 1
    assert store.sibling_with_audio(EMBED, dict(same, group="directors"), "Живов") is None
    assert store.sibling_with_audio(EMBED, same, "LostFilm") is None


def test_single_flight_runs_once():
    calls = []

    async def scenario():
        flight = server.SingleFlight()

        async def work():
            calls.append(1)
            await asyncio.sleep(0.01)
            return 42

        return await asyncio.gather(*(flight.run("k", work) for _ in range(5)))

    assert asyncio.run(scenario()) == [42] * 5 and len(calls) == 1


# ---- HTTP API без сети ----------------------------------------------------

@pytest.fixture
def client_factory(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(server.Site, "refresh_hash", noop)
    monkeypatch.setattr(server.Browser, "ensure", noop)

    async def make():
        app = server.make_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        return app, client

    return make


def test_health_plugin_and_validation(client_factory):
    async def scenario():
        app, client = await client_factory()
        try:
            health = await (await client.get("/api/health")).json()
            assert health["version"] == server.APP_VERSION
            plugin = await client.get("/plugin.js")
            body = await plugin.text()
            assert "__BASE_URL__" not in body and str(client.server.port) in body
            assert plugin.headers["Access-Control-Allow-Origin"] == "*"
            assert (await client.get("/api/search?q=")).status == 400
            assert (await client.get("/api/detail?url=https://evil.example/")).status == 400
            bad = await client.get("/api/resolve?embed_url=https://evil.example/&page_url=%s&file_id=1" % PAGE)
            assert bad.status == 400
            assert (await client.get("/p/unknown?u=x")).status == 410
            assert (await client.get("/hls/unknown/0/720.m3u8")).status == 410
        finally:
            await client.close()

    asyncio.run(scenario())


def test_resolve_returns_cached_stream_without_browser(client_factory):
    async def scenario():
        app, client = await client_factory()
        try:
            app["streams"].put(EMBED, 77, {"hlsSource": [
                {"label": "(Russian) AVO Ю Живов", "quality": {"1080": "https://a.vkvideo.cloud/m.m3u8"}}]})
            response = await client.get("/api/resolve", params={
                "embed_url": EMBED, "page_url": PAGE, "file_id": "77", "prefer": "Живов"})
            data = await response.json()
            assert response.status == 200 and data["default_audio"] == 0
            assert data["audios"][0]["url"].endswith("/hls/%s/0/1080.m3u8" % data["key"])
        finally:
            await client.close()

    asyncio.run(scenario())


def test_quality_alternatives_keep_cdn_mirrors_in_order():
    qualities = {"1080": "https://a.vkvideo.cloud/1.m3u8 or https://b.vkvideo.cloud/1.m3u8 or https://evil.example/1.m3u8"}
    assert server.quality_alternatives(qualities, "1080") == [
        "https://a.vkvideo.cloud/1.m3u8", "https://b.vkvideo.cloud/1.m3u8"]
    assert server.quality_alternatives(qualities, 720) == []


def test_av1_only_heights_are_hidden_by_default():
    entry = {"key": "k", "file_id": 1, "embed": EMBED, "data": {"hlsSource": [{"label": "a", "quality": {
        "2160": "https://a.vkvideo.cloud/4k.m3u8", "1080": "https://a.vkvideo.cloud/fhd.m3u8"}}]}}
    audio = server.stream_payload(entry, "http://lampa.local:5200")["audios"][0]
    assert list(audio["quality"]) == ["1080p"] and audio["url"].endswith("/1080.m3u8")


def test_cors_header_is_present_on_streamed_responses(client_factory):
    async def scenario():
        app, client = await client_factory()
        try:
            for path in ("/api/health", "/p/unknown?u=x", "/plugin.js"):
                response = await client.get(path)
                assert response.headers.get("Access-Control-Allow-Origin") == "*", path
        finally:
            await client.close()

    asyncio.run(scenario())


def test_menu_steps_follow_player_menu_order():
    steps = server.menu_steps({"season": 1, "episode": 4, "tid": 82, "quality": "WEB-DL"})
    assert [s["kind"] for s in steps] == ["season", "episode", "translation"]
    assert steps[2] == {"kind": "translation", "exact": "t82|WEB-DL", "prefix": "t82"}
    movie = server.menu_steps({"group": "directors", "tid": 66, "quality": "BDRip"})
    assert [s["kind"] for s in movie] == ["group", "translation"]


def test_mirror_order_skips_known_bad_cdn_nodes():
    media = server.MediaProxy(server.StreamStore())
    calls = []

    async def fake_fetch(url, entry, attempts=3):
        calls.append(url)
        if "bad" in url:
            raise RuntimeError("upstream HTTP 403")
        return "#EXTM3U", url

    media.fetch_text = fake_fetch
    entry = {"embed": EMBED}
    urls = ["https://bad.vkvideo.cloud/a.m3u8", "https://good.vkvideo.cloud/a.m3u8"]
    assert asyncio.run(media.fetch_first(urls, entry, (0, "1080")))[1] == urls[1]
    assert "bad.vkvideo.cloud" in media.bad_hosts
    calls.clear()
    asyncio.run(media.fetch_first(urls, {"embed": EMBED}, (0, "720")))
    assert calls == [urls[1]]  # сломанный узел больше не пробуется первым


def test_search_rejects_recommendations_and_preserves_short_title_words():
    items = [{"title": "Доктор Хаус"}, {"title": "Доктор Икс"}, {"title": "Грандиозная подделка"}]
    assert server.relevant_items(items, "Доктор Х") == [items[1]]
    assert server.relevant_items(items, "несуществующий фильм") == []


def test_pick_best_matches_subtitle_and_x_variants():
    item = {"title": "Доктор Икс", "year": "2026", "media_type": "series", "url": "x"}
    best, confident = server.pick_best([item], "Доктор Х: Эпоха белой мафии", "Doctor X", "2026", "series")
    assert best == item and confident


def test_4k_payload_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "MAX_HEIGHT", 2160)
    entry = {"key":"uhd", "file_id":1, "data":{"hlsSource":[{"label":"RU", "quality":{"2160":"https://a.vkvideo.cloud/uhd.m3u8", "1080":"https://a.vkvideo.cloud/hd.m3u8"}}]}}
    audio = server.stream_payload(entry, "http://lampa.local:5200")["audios"][0]
    assert audio["url"].endswith("/2160.m3u8")
    assert "1080p" in audio["quality"]


def test_media_headers_normalize_referer_and_use_actual_browser_origin():
    entry = {"embed":"https://Assortedia-as.stravers.live/?token=abc"}
    headers = server.MediaProxy.headers(entry)
    assert headers["Referer"] == "https://assortedia-as.stravers.live/?token=abc"
    assert headers["Origin"] == "https://assortedia-as.stravers.live"
    entry["media_headers"] = {"referer":"https://actual.stravers.live/", "origin":"https://actual.stravers.live"}
    headers = server.MediaProxy.headers(entry, {"Range":"bytes=0-10"})
    assert headers["Referer"] == "https://actual.stravers.live/"
    assert headers["Origin"] == "https://actual.stravers.live"
    assert headers["Range"] == "bytes=0-10"


def test_browser_captures_cdn_session_headers_without_borth():
    from types import SimpleNamespace
    browser = server.Browser(server.StreamStore())
    sess = server.PlayerSession(EMBED, PAGE)
    request = SimpleNamespace(url="https://a.vkvideo.cloud/file/master.m3u8", headers={
        "authorizations":"session", "accepts-controls":"controls", "origin":"https://frame.stravers.live",
        "referer":"https://frame.stravers.live/", "user-agent":"Chrome", "borth":"private-api-only"})
    browser._on_request(sess, request)
    captured = sess.cdn_headers[request.url]
    assert captured['Authorizations'] == 'session'
    assert captured['Accepts-Controls'] == 'controls'
    assert 'Borth' not in captured
    assert server.MediaProxy.headers({'embed':EMBED, 'media_headers':captured})['Authorizations'] == 'session'


@pytest.mark.asyncio
async def test_media_regenerates_session_headers_for_every_network_request():
    media = server.MediaProxy(server.StreamStore())
    calls = []
    async def signer(entry, url):
        calls.append(url)
        return {'authorizations': 'fresh-%d' % len(calls), 'accepts-controls':'live'}
    media.signer = signer
    entry = {'embed': EMBED, 'signer_id': 0, 'media_headers': {'Authorizations':'expired'}}
    first = await media.request_headers(entry, 'https://a.vkvideo.cloud/master.m3u8')
    second = await media.request_headers(entry, 'https://a.vkvideo.cloud/seg.ts', {'Range':'bytes=0-10'})
    assert first['Authorizations'] == 'fresh-1'
    assert second['Authorizations'] == 'fresh-2'
    assert len([k for k in second if k.lower() == 'authorizations']) == 1
    assert second['Range'] == 'bytes=0-10'


def test_stream_refresh_preserves_old_generation_credentials():
    store = server.StreamStore()
    old = store.put(EMBED, 77, {'hlsSource':[]})
    old['media_headers'] = {'Authorizations':'old-key'}
    fresh = store.put(EMBED, 77, {'hlsSource':[]})
    fresh['media_headers'] = {'Authorizations':'new-key'}
    assert old['key'] != fresh['key']
    assert store.by(old['key'])['media_headers']['Authorizations'] == 'old-key'
    assert store.by(fresh['key'])['media_headers']['Authorizations'] == 'new-key'
    assert store.get(EMBED, 77) is fresh


def test_prefetch_never_switches_the_active_provider_player(client_factory, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('hover must not sign another stream')
    monkeypatch.setattr(server.Browser, 'resolve', forbidden)
    async def scenario():
        app, client = await client_factory()
        try:
            response = await client.get('/api/prefetch', params={
                'embed_url':EMBED, 'page_url':PAGE, 'file_id':'88', 'season':'1', 'episode':'2'})
            assert response.status == 202
        finally:
            await client.close()
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_live_headers_renew_via_native_loader_after_expiry():
    from types import SimpleNamespace
    sess = server.PlayerSession(EMBED, PAGE)
    sess.page = SimpleNamespace(is_closed=lambda:False)
    calls=[]
    class Frame:
        async def evaluate(self, script):
            assert 'loadSource' in script
            calls.append(1)
            sess.live_headers = {'Authorizations':'renewed'}
            sess.header_time = server.time.monotonic()
            sess.header_event.set()
    sess.frame=lambda:Frame()
    sess.header_time=server.time.monotonic()-90
    sess.live_headers={'Authorizations':'expired'}
    browser=server.Browser(server.StreamStore())
    entry={'embed':EMBED, 'file_id':1, 'session':sess}
    assert (await browser.sign_headers(entry,'https://a.vkvideo.cloud/a.ts'))['Authorizations']=='renewed'
    assert (await browser.sign_headers(entry,'https://a.vkvideo.cloud/b.ts'))['Authorizations']=='renewed'
    assert len(calls)==1


def test_active_stream_ttl_extends_and_old_version_cannot_replace_new(monkeypatch):
    clock=[100.0]
    monkeypatch.setattr(server.time,'monotonic',lambda:clock[0])
    store=server.StreamStore()
    store.by_key.ttl=10
    old=store.put(EMBED,1,{'hlsSource':[]})
    fresh=store.put(EMBED,1,{'hlsSource':[]})
    for _ in range(4):
        clock[0]+=9
        assert store.by(old['key']) is old
        assert store.get(EMBED,1) is fresh
    clock[0]+=11
    assert store.by(old['key']) is None


@pytest.mark.asyncio
async def test_page_eviction_keeps_active_playback_session():
    from collections import OrderedDict
    browser=server.Browser(server.StreamStore())
    active=server.PlayerSession(EMBED,PAGE);active.file_id=1
    active.last_media_used=server.time.monotonic()
    browser.sessions=OrderedDict([(1,active)])
    for i in range(server.MAX_PLAYER_PAGES):
        s=server.PlayerSession(EMBED,PAGE);s.file_id=i+2
        browser.sessions[i+2]=s
    removed=[]
    async def close(s):removed.append(s)
    browser._close_page=close
    await browser._evict()
    assert active in browser.sessions.values() and active not in removed
    assert len(browser.sessions)==server.MAX_PLAYER_PAGES


@pytest.mark.asyncio
async def test_forced_refresh_keeps_previous_browser_session_immutable():
    browser=server.Browser(server.StreamStore())
    async def noop():pass
    browser.ensure=noop
    old=await browser._session(EMBED,PAGE,1)
    old.live_headers={'Authorizations':'old'}
    fresh=await browser._session(EMBED,PAGE,1,fresh=True)
    fresh.live_headers={'Authorizations':'new'}
    assert old is not fresh
    assert old.live_headers['Authorizations']=='old'
    assert old in browser.sessions.values()
    assert browser.sessions[(EMBED,1)] is fresh


@pytest.mark.asyncio
async def test_all_media_download_paths_use_the_configured_proxy(monkeypatch):
    # A nonexistent destination can only succeed through this local HTTP proxy.
    from aiohttp import web
    paths = []
    async def forward(request):
        paths.append(request.path)
        body = b"#EXTM3U\n" if request.path.endswith(".m3u8") else b"media-bytes"
        return web.Response(body=body, headers={"Content-Type": "application/octet-stream"})
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", forward)
    async with TestServer(app) as upstream:
        monkeypatch.setattr(server, "UPSTREAM_PROXY", str(upstream.make_url("/")).rstrip("/"))
        media = server.MediaProxy(server.StreamStore())
        await media.start()
        try:
            entry = {"embed": EMBED}
            text, _ = await media.fetch_text("http://unreachable.invalid/master.m3u8", entry)
            assert text == "#EXTM3U\n"
            assert (await media._download("http://unreachable.invalid/prefetch.m4s", entry))[0] == b"media-bytes"
            async def stream(request):
                return await media.segment(request, "http://unreachable.invalid/live.m4s", entry)
            downstream = web.Application()
            downstream.router.add_get("/video", stream)
            async with TestClient(TestServer(downstream)) as client:
                response = await client.get("/video")
                assert response.status == 200 and await response.read() == b"media-bytes"
            assert paths == ["/master.m3u8", "/prefetch.m4s", "/live.m4s"]
        finally:
            await media.close()


@pytest.mark.asyncio
async def test_resolve_does_not_issue_a_successful_url_for_a_blocked_cdn_session(client_factory, monkeypatch):
    app, client = await client_factory()
    try:
        entry = app['streams'].put(EMBED, 77, {'hlsSource': [
            {'label': 'RU', 'quality': {'1080': 'https://a.vkvideo.cloud/master.m3u8'}}]})
        entry['session'] = object()
        async def blocked(*args, **kwargs):
            raise RuntimeError('upstream HTTP 403 (session_blocked)')
        monkeypatch.setattr(app['media'], 'fetch_first', blocked)
        response = await client.get('/api/resolve', params={'embed_url': EMBED, 'page_url': PAGE, 'file_id': '77'})
        assert response.status == 502
        data = await response.json()
        assert 'session_blocked' in data['error'] and 'audios' not in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_backup_keeps_metadata_and_media_user_agent_and_episode(client_factory, monkeypatch):
    from types import SimpleNamespace
    import json
    app, client = await client_factory()
    calls = []
    alternate = 'https://api.nextembed.ws/embed/movie/93705?host=kinokrad.my'
    async def detail(_url):
        return {'backup_embed_url': alternate}
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        if url == alternate:
            text = 'var unixTime=360000; var lok=1, sig="salt"; makePlayer({playlist:{seasons:' + json.dumps([
                {'season': 1, 'episodes': [{'episode': n, 'hls': f'https://cdnr.interkh.com/{n}.m3u8?t=1'} for n in (10, 11, 13)]}
            ]) + '}})'
        else:
            text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n720.m3u8\n'
        return SimpleNamespace(status_code=200, text=text, url=url)
    monkeypatch.setattr(app['site'], 'detail', detail)
    monkeypatch.setattr(app['site'].session, 'get', get)
    try:
        keys = []
        for episode in (10, 11, 10):
            response = await client.get('/api/resolve', params={'embed_url': EMBED, 'page_url': PAGE,
                'file_id': str(episode), 'season': '1', 'episode': str(episode), 'provider': 'backup'})
            assert response.status == 200
            payload = await response.json()
            assert payload['default_audio'] is None and payload['provider'] == 'backup'
            keys.append(payload['key'])
            entry = app['streams'].by(payload['key'])
            assert entry['target']['episode'] == episode
            assert f'/{episode}.m3u8?' in entry['backup_master'].url
            media_headers = await app['media'].request_headers(entry, entry['backup_master'].url)
            assert media_headers['User-Agent'] == calls[0][1]['headers']['User-Agent'] == server.UA
            assert media_headers['Referer'] == 'https://api.nextembed.ws/'
            assert all(call[1]['proxy'] == server.UPSTREAM_PROXY for call in calls)
            master = await client.get('/' + payload['audios'][0]['url'].split('/', 3)[3])
            assert master.status == 200 and '/p/' + payload['key'] in await master.text()
        assert keys[0] == keys[2] and keys[0] != keys[1] and len(calls) == 4
    finally:
        await client.close()
