import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import backup_player as B
import parsers as P


def fixture(episodes, blocked=False):
    return 'var lok = 1, sig="salt"; makePlayer({playlist:{seasons:' + json.dumps([
        {'season': 1, 'blocked': blocked, 'episodes': episodes}]) + '}})'


def test_selects_requested_episode_instead_of_latest():
    text = fixture([{'episode': '10', 'hls': 'https://cdnr.interkh.com/ten.m3u8?t=1'},
                    {'episode': '13', 'hls': 'https://cdnr.interkh.com/thirteen.m3u8?t=1'}])
    assert B.episode_source(text, 1, 10) == 'https://cdnr.interkh.com/ten.m3u8?t=1&salt'
    with pytest.raises(ValueError, match='отсутствует'):
        B.episode_source(text, 1, 11)
    with pytest.raises(ValueError, match='отсутствует'):
        B.episode_source(text, 2, 10)
    with pytest.raises(ValueError, match='отсутствует'):
        B.episode_source(fixture([{'episode': '10', 'hls': 'url'}], blocked=True), 1, 10)


def test_only_trusted_backup_embed_is_accepted():
    assert B.embed_url('<button class="tab-button" data-url="//api.nextembed.ws/embed/movie/93705?host=kinokrad.my">')
    assert B.embed_url('<button class="tab-button" data-url="https://evil.example/embed/movie/123">') is None


MASTER = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="rus0",DEFAULT=YES,AUTOSELECT=YES,LANGUAGE="ru",URI="ru.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="eng1",DEFAULT=NO,AUTOSELECT=NO,LANGUAGE="en",URI="en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,AUDIO="a",CODECS="avc1.64001f,mp4a.40.2"
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=350000,RESOLUTION=640x360,AUDIO="a"
360.m3u8
'''


def test_audio_and_quality_are_preserved_without_inventing_1080p():
    m = B.Master(MASTER, 'https://cdnr.interkh.com/v/master.m3u8', P.allowed_media_url)
    assert m.heights == [720, 360]
    assert m.label(0) == 'Русская дорожка'
    out = m.playlist(1, 720)
    assert 'eng1' in out and 'rus0' not in out
    assert 'DEFAULT=YES' in out and '720.m3u8' in out and '360.m3u8' not in out
    assert 'CODECS="avc1.64001f,mp4a.40.2"' in out


def test_rejects_untrusted_cdn_and_invalid_playlist():
    with pytest.raises(ValueError, match='сервер'):
        B.Master(MASTER.replace('720.m3u8', 'https://evil.example/720.m3u8'),
                 'https://cdnr.interkh.com/v/master.m3u8', P.allowed_media_url)
    with pytest.raises(ValueError, match='неверный'):
        B.Master('<html>blocked</html>', 'https://cdnr.interkh.com/a', P.allowed_media_url)


def test_provider_transport_uses_server_clock_and_keeps_canonical_playlist_base(monkeypatch):
    import time
    monkeypatch.setattr(time, 'monotonic', lambda: 50.0)
    url = 'https://cdnr.interkh.com/v/seg.ts?x=1'
    encoded = B.cdn_url(url, (360000, 50.0))
    # Reference vector from the site's JavaScript loader, with server hour 100.
    assert encoded == 'https://cdnr.interkh.com/x-en-x/RBDcYa92Y3keSa50qG94jBE='
    assert B.cdn_url(encoded, (360000, 50.0)) == encoded
    assert B.cdn_url('https://cdnr.interkh.com/init.mp4', (360000, 50.0)).endswith('init.mp4')
    restored = B.canonical_url('https://cdnr.interkh.com/v/index.m3u8?x=1', encoded.replace('cdnr.', 'x-bc.'))
    assert restored == 'https://x-bc.interkh.com/v/index.m3u8?x=1'
