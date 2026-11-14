"""The site's public Video 2 player; parse data, never execute provider scripts."""
import json
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def embed_url(page):
    for button in BeautifulSoup(page, 'html.parser').select('button.tab-button[data-url]'):
        url = urljoin('https://kinokrad.my/', button.get('data-url', ''))
        p = urlparse(url)
        if p.scheme == 'https' and p.hostname == 'api.nextembed.ws' and re.fullmatch(r'/embed/movie/\d+', p.path):
            return url
    return None


def episode_source(page, season=None, episode=None):
    salt = re.search(r'var\s+lok\s*=\s*\d+,\s*\w+\s*=\s*"([^"]+)"', page)
    if not salt:
        raise ValueError('Видео 2: изменился формат подписи ссылки')
    match = re.search(r'\bseasons\s*:\s*(\[)', page)
    if match:
        seasons, _ = json.JSONDecoder().raw_decode(page[match.start(1):])
        if season is None or episode is None:
            raise ValueError('Видео 2: укажи сезон и серию')
        source = next((ep for se in seasons if str(se.get('season')) == str(season) and not se.get('blocked')
                       for ep in se.get('episodes', []) if str(ep.get('episode')) == str(episode) and not ep.get('blocked')), None)
        if source is None:
            raise ValueError('Видео 2: выбранная серия отсутствует')
    else:
        match = re.search(r'\bsource\s*:\s*(\{)', page)
        if not match:
            raise ValueError('Видео 2: видеопоток не найден')
        source, _ = json.JSONDecoder().raw_decode(page[match.start(1):])
    if not source.get('hls'):
        raise ValueError('Видео 2: у выбранной серии нет HLS')
    return source['hls'] + '&' + salt.group(1)


def attrs(line):
    return {m[1]: m[2].strip('"') for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]+)', line)}


class Master:
    def __init__(self, text, url, allowed):
        if not text.lstrip().startswith('#EXTM3U'):
            raise ValueError('Видео 2: неверный плейлист')
        self.text, self.url = text, url
        self.audio, self.variants = [], []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-MEDIA:') and attrs(line).get('TYPE') == 'AUDIO':
                self.audio.append((line, attrs(line)))
            if line.startswith('#EXT-X-STREAM-INF:') and i + 1 < len(lines):
                a = attrs(line)
                size = re.fullmatch(r'(\d+)x(\d+)', a.get('RESOLUTION', ''))
                if size:
                    self.variants.append((int(size[2]), line, lines[i + 1]))
            for link in re.findall(r'URI="([^"]+)"', line) + ([line] if line and not line.startswith('#') else []):
                if not allowed(urljoin(url, link)):
                    raise ValueError('Видео 2: неподдерживаемый сервер видео')
        self.names = list(dict.fromkeys(a['NAME'] for _, a in self.audio)) or ['video']
        self.heights = sorted({h for h, _, _ in self.variants}, reverse=True)
        if not self.heights:
            raise ValueError('Видео 2: варианты качества не найдены')

    def label(self, index):
        name = self.names[index]
        lang = next((a.get('LANGUAGE', '') for _, a in self.audio if a['NAME'] == name), '')
        label = {'ru': 'Русская дорожка', 'uk': 'Украинская дорожка', 'en': 'Английская дорожка'}.get(lang, name)
        count = Counter(a.get('LANGUAGE') for _, a in self.audio if a.get('GROUP-ID') == self.audio[0][1].get('GROUP-ID')) if self.audio else {}
        return label + (' — ' + name if count.get(lang, 0) > 1 else '')

    def playlist(self, index, height):
        name = self.names[index]
        output = ['#EXTM3U', '#EXT-X-VERSION:3']
        for line, a in self.audio:
            if a['NAME'] == name:
                line = re.sub(r'DEFAULT=(?:YES|NO)', 'DEFAULT=YES', line)
                line = re.sub(r'AUTOSELECT=(?:YES|NO)', 'AUTOSELECT=YES', line)
                output.append(line)
        for h, line, url in self.variants:
            if h == int(height):
                # No subtitle groups are advertised in the generated master.
                output.extend([re.sub(r',SUBTITLES="[^"]*"', '', line), url])
        return '\n'.join(output) + '\n'


def clock(page):
    import time
    match = re.search(r'\bunixTime\s*=\s*(\d+)', page)
    if not match:
        raise ValueError('Видео 2: время сервера не найдено')
    return (int(match.group(1)), time.monotonic())


def cdn_url(url, server_clock):
    """Same public URL transport used by the provider's cdn.js loader."""
    import base64
    import math
    import time
    p = urlparse(url)
    if '/x-en-x/' in p.path or not re.search(r'\.(ts|m3u8|webm|mpd)$', p.path):
        return url
    server_time, observed = server_clock
    hour = math.floor((server_time + time.monotonic() - observed) / 3600 + 0.5)
    plain = str(hour) + '/' + p.path + ('?' + p.query if p.query else '')
    encoded = base64.b64encode(plain.encode()).decode().translate(str.maketrans(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
        'DlChEXitLONYRkFjAsnBbymWzSHMqKPgQZpvwerofJTVdIuUcxaG'))
    return p.scheme + '://' + p.netloc + '/x-en-x/' + encoded


def canonical_url(original, final):
    # The native loader restores the original pathname after encoded redirects.
    if '/x-en-x/' in urlparse(final).path:
        return urlparse(original)._replace(netloc=urlparse(final).netloc).geturl()
    return final
