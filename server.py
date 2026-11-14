"""KinoKrad для Lampa, v3 — асинхронный сервер для домашней сети или VPS.

Что делает быстрым:
* поиск и карточка — `curl_cffi` (TLS-отпечаток Chrome) с keep-alive, без
  Chromium; устаревший `dle_login_hash` обновляется с главной за ~0,5 с;
* подпись Borth — один постоянно запущенный Chromium; страница плеера для
  тайтла прогревается заранее и переключает серии/озвучки собственным меню
  плеера (~0,3 с) вместо новой загрузки страницы (8 с на старом сервере);
* одинаковые параллельные запросы схлопываются (single-flight);
* видео — потоковый прокси с упреждающей загрузкой следующих HLS-сегментов в
  память, чтобы ускоренное воспроизведение не упиралось в CDN.
"""
import asyncio
import base64
import hashlib
import html as html_lib
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from aiohttp import web
from curl_cffi.requests import AsyncSession

import parsers as P
import backup_player as Backup
from curl_cffi.requests import AsyncSession as CFAsyncSession

APP_VERSION = "3.4.5"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5200"))
# Keep the signing player, WebSocket and CDN on one stable egress address.
UPSTREAM_PROXY = os.getenv("KK_UPSTREAM_PROXY") or None
# Fallback: прокси через собственный VPN пользователя. Включается автоматически,
# когда CDN начинает отказывать DIRECT-маршруту (token_decrypt/session_blocked).
FALLBACK_PROXY = os.getenv("KK_FALLBACK_PROXY") or None
UA = os.getenv(
    "KK_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
)
SEARCH_AJAX = P.SITE + "/engine/lazydev/dle_search/ajax.php"
SEARCH_TTL = 600
DETAIL_TTL = 1800
STREAM_TTL = int(os.getenv("STREAM_TTL", "1200"))
PREFETCH_AHEAD = int(os.getenv("PREFETCH_AHEAD", "6"))
SEGMENT_CACHE_MB = int(os.getenv("SEGMENT_CACHE_MB", "768"))
MAX_PLAYER_PAGES = int(os.getenv("MAX_PLAYER_PAGES", "3"))
PAGE_IDLE = int(os.getenv("PAGE_IDLE", "900"))
CLICK_WAIT = float(os.getenv("CLICK_WAIT", "4"))
LOAD_WAIT = float(os.getenv("LOAD_WAIT", "30"))
# Всё выше 1080p у KinoKrad — только AV1 (av01): на ТВ без аппаратного AV1
# это программное декодирование 4K, долгий старт и рывки. По умолчанию
# отдаём лучшее H.264-качество; AV1 можно включить переменной окружения.
ALLOW_AV1 = os.getenv("KK_ALLOW_AV1", "0") == "1"
MAX_HEIGHT = int(os.getenv("KK_MAX_HEIGHT", "0") or 0) or (100000 if ALLOW_AV1 else 1080)
ROOT = Path(__file__).resolve().parent

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kinokrad")


# --------------------------------------------------------------------------
# Утилиты
# --------------------------------------------------------------------------

class TTLCache:
    def __init__(self, ttl, maxsize=500):
        self.ttl, self.maxsize, self.data = ttl, maxsize, OrderedDict()

    def get(self, key):
        item = self.data.get(key)
        if item is None:
            return None
        if item[0] < time.monotonic():
            self.data.pop(key, None)
            return None
        self.data.move_to_end(key)
        return item[1]

    def set(self, key, value, ttl=None):
        self.data[key] = (time.monotonic() + (ttl or self.ttl), value)
        self.data.move_to_end(key)
        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)

    def __len__(self):
        return len(self.data)


class SingleFlight:
    """Одинаковые одновременные запросы ждут одну и ту же задачу."""

    def __init__(self):
        self.tasks = {}

    async def run(self, key, factory):
        task = self.tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self.tasks[key] = task
            task.add_done_callback(lambda _t, k=key: self.tasks.pop(k, None))
        # shield: обрыв соединения одним клиентом не отменяет общую работу
        return await asyncio.shield(task)


class ByteLRU:
    def __init__(self, max_bytes):
        self.max_bytes, self.size, self.data = max_bytes, 0, OrderedDict()

    def get(self, key):
        item = self.data.get(key)
        if item is not None:
            self.data.move_to_end(key)
        return item

    def put(self, key, body, ctype):
        if key in self.data:
            return
        self.data[key] = (body, ctype)
        self.size += len(body)
        while self.size > self.max_bytes and self.data:
            _, (old, _) = self.data.popitem(last=False)
            self.size -= len(old)


def b64e(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def b64d(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def norm_title(value):
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"\b(?:x|х|икс)\b", "икс", value)
    return re.sub(r"[^0-9a-zа-я]+", "", value)


def title_tokens(value):
    value = re.sub(r"\b(?:x|х|икс)\b", "икс", (value or "").lower().replace("ё", "е"))
    return re.findall(r"[0-9a-zа-я]+", value)


def relevant_items(items, query):
    """Отсекает рекомендации и совпадения только по одному слову названия."""
    tokens = title_tokens(query)
    return [item for item in items if tokens and all(
        any(t == word or (len(t) >= 4 and word.startswith(t)) for word in title_tokens(item.get("title")))
        for t in tokens)]


def search_queries(title):
    # Подзаголовки у TMDB/DoramyClub часто отсутствуют у KinoKrad.
    base = re.split(r"[:：]", title, maxsplit=1)[0].strip()
    variants = [title, base, re.sub(r"\b[хx]\b", "Икс", base, flags=re.I)]
    return list(dict.fromkeys(q for q in variants if q))


def pick_best(items, title, original="", year="", media_type=""):
    """Выбирает результат поиска KinoKrad под карточку TMDB."""
    want = {norm_title(q) for value in (title, original) for q in search_queries(value)} - {""}
    scored = []
    for item in items:
        name = norm_title(item.get("title"))
        score = 0
        if name and name in want:
            score += 10
        elif name and (relevant_items([item], title) or (original and relevant_items([item], original))):
            score += 4
        if year and str(item.get("year", "")).isdigit() and str(year).isdigit():
            diff = abs(int(item["year"]) - int(year))
            # у сериалов KinoKrad и TMDB часто расходятся на год-два
            score += 5 if diff == 0 else (2 if diff <= 2 else -5)
        if media_type and item.get("media_type") == media_type:
            score += 2
        scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return None, False
    best_score = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else -99
    exact = [x for x in scored if norm_title(x[1].get("title")) in want]
    unique_exact = len(exact) == 1 and exact[0][0] >= 10
    confident = (best_score >= 12 and best_score - second >= 3) or (unique_exact and scored[0] is exact[0])
    return scored[0][1], confident


AUDIO_ALIASES = (
    (r"дублир", r"\bdub\b|дубляж"),
    (r"многоголос", r"\bmvo\b"),
    (r"двухголос", r"\bdvo\b"),
    (r"одноголос|авторск", r"\bavo\b|\bvo\b"),
    (r"оригинал", r"original|english|\(eng"),
    (r"украин", r"ukrainian|украин"),
)


def match_audio(audios, prefer):
    """Индекс аудиодорожки, соответствующей выбранной озвучке, или None."""
    if len(audios) == 1:
        return 0
    prefer_l = (prefer or "").lower().replace("ё", "е")
    if not prefer_l:
        return None
    words = [w for w in re.findall(r"[a-zа-я0-9]{4,}", prefer_l)
             if w not in {"профессиональный", "многоголосый", "одноголосый", "закадровый",
                          "дублированный", "двухголосый", "авторский", "любительский"}]
    best, best_score = None, 0
    for index, audio in enumerate(audios):
        label = (audio.get("label") or "").lower().replace("ё", "е")
        score = 0
        if any(w in label for w in words):
            score += 10
        for left, right in AUDIO_ALIASES:
            if re.search(left, prefer_l) and re.search(right, label):
                score += 5
        if score > best_score:
            best, best_score = index, score
    return best if best_score >= 5 else None


# --------------------------------------------------------------------------
# Сайт KinoKrad: поиск и карточка без браузера
# --------------------------------------------------------------------------

class Site:
    def __init__(self):
        self.session = None
        self.dle_hash = os.getenv("KINOKRAD_SEARCH_HASH", "")
        self.hash_lock = asyncio.Lock()
        self.search_cache = TTLCache(SEARCH_TTL)
        self.detail_cache = TTLCache(DETAIL_TTL, 300)
        self.flight = SingleFlight()

    async def start(self):
        self.session = AsyncSession(
            impersonate="chrome", timeout=20,
            headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"},
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def get_text(self, url, referer=None):
        headers = {"Referer": referer} if referer else None
        response = await self.session.get(url, headers=headers, allow_redirects=True)
        if response.status_code >= 400:
            raise RuntimeError("HTTP %d for %s" % (response.status_code, urlparse(url).hostname))
        return response.text

    async def refresh_hash(self, force=False):
        async with self.hash_lock:
            if self.dle_hash and not force:
                return self.dle_hash
            started = time.monotonic()
            value = P.extract_dle_hash(await self.get_text(P.SITE + "/"))
            if not value:
                raise RuntimeError("dle_login_hash not found on KinoKrad home page")
            self.dle_hash = value
            log.info("dle hash refreshed in %.2fs", time.monotonic() - started)
            return value

    async def _ajax_search(self, query):
        response = await self.session.post(
            SEARCH_AJAX,
            data={"story": query, "thisUrl": "/", "dle_hash": self.dle_hash},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": P.SITE + "/", "Origin": P.SITE},
        )
        if response.status_code >= 400:
            raise RuntimeError("search HTTP %d" % response.status_code)
        return P.parse_search_ajax(response.text)

    async def search(self, query):
        query = re.sub(r"\b[хx]\b", "Икс", query, flags=re.I)
        key = query.casefold()
        cached = self.search_cache.get(key)
        if cached is not None:
            return cached

        async def work():
            if not self.dle_hash:
                await self.refresh_hash()
            try:
                items = await self._ajax_search(query)
            except Exception as exc:
                log.info("ajax search failed (%s), refreshing dle hash", exc)
                await self.refresh_hash(force=True)
                try:
                    items = await self._ajax_search(query)
                except Exception as exc2:
                    log.warning("ajax search failed again (%s), using /search page", exc2)
                    items = await self.search_full(query)
            items = relevant_items(items, query)
            self.search_cache.set(key, items)
            return items

        return await self.flight.run(("search", key), work)

    async def search_full(self, query):
        """Полная страница поиска: медленнее автодополнения, но находит больше."""
        key = "full:" + query.casefold()
        cached = self.search_cache.get(key)
        if cached is not None:
            return cached

        async def work():
            response = await self.session.post(
                P.SITE + "/index.php?do=search",
                data={"do": "search", "subaction": "search", "story": query, "search_start": "1", "full_search": "0", "result_from": "1"},
                headers={"Referer": P.SITE + "/"},
            )
            if response.status_code >= 400:
                raise RuntimeError("full search HTTP %d" % response.status_code)
            html = response.text
            self.dle_hash = P.extract_dle_hash(html) or self.dle_hash
            items = P.parse_search(html)
            items = relevant_items(items, query)
            self.search_cache.set(key, items)
            return items

        return await self.flight.run(("search", key), work)

    async def detail(self, url):
        if not P.allowed_page_url(url):
            raise ValueError("invalid KinoKrad URL")
        cached = self.detail_cache.get(url)
        if cached is not None:
            return cached

        async def work():
            page = await self.get_text(url)
            embed = P.extract_embed_url(page)
            if not P.is_player_url(embed):
                raise RuntimeError("На KinoKrad для этого тайтла нет плеера")
            player = await self.get_text(embed, referer=url)
            if not re.search(r"\bfileList\s*=\s*JSON\.parse", player):
                raise RuntimeError("Плеер KinoKrad не отдал список серий и озвучек")
            data = P.parse_detail(page, url, player)
            data["embed_url"] = embed
            data["backup_embed_url"] = Backup.embed_url(page)
            self.detail_cache.set(url, data)
            return data

        return await self.flight.run(("detail", url), work)


# --------------------------------------------------------------------------
# Потоки: результаты подписанных запросов плеера
# --------------------------------------------------------------------------

class StreamStore:
    def __init__(self):
        self.by_file = TTLCache(STREAM_TTL, 1000)
        self.by_key = TTLCache(STREAM_TTL, 1000)
        self.by_embed = TTLCache(STREAM_TTL, 300)
        self.targets = {}

    @staticmethod
    def make_key(embed, file_id):
        return hashlib.sha256(("%s|%s" % (embed, file_id)).encode()).hexdigest()[:20]

    def put(self, embed, file_id, data, page_url=""):
        entry = {"key": self.make_key(embed, file_id) + secrets.token_hex(6), "embed": embed, "file_id": int(file_id),
                 "data": data, "target": self.targets.get((embed, int(file_id)), {}),
                 "page_url": page_url, "created": time.monotonic()}
        self.by_file.set((embed, int(file_id)), entry)
        self.by_key.set(entry["key"], entry)
        siblings = self.by_embed.get(embed) or []
        self.by_embed.set(embed, [e for e in siblings if e["file_id"] != entry["file_id"]][-20:] + [entry])
        return entry

    def remember_target(self, embed, file_id, target):
        if target:
            self.targets[(embed, int(file_id))] = dict(target)
            if len(self.targets) > 5000:
                self.targets.clear()

    def sibling_with_audio(self, embed, target, prefer):
        """Уже полученный поток той же версии/серии, где есть нужная дорожка.

        У многих фильмов KinoKrad каждая «озвучка» — это один и тот же релиз
        со всеми аудиодорожками, поэтому второй подписанный запрос не нужен.
        """
        same = ("group", "quality", "season", "episode")
        for entry in reversed(self.by_embed.get(embed) or []):
            sources = entry["data"].get("hlsSource") or []
            if len(sources) < 2 or not prefer:
                continue
            if any(entry["target"].get(k) != target.get(k) for k in same):
                continue
            if match_audio([{"label": a.get("label")} for a in sources], prefer) is not None:
                return entry
        return None

    def get(self, embed, file_id):
        return self.by_file.get((embed, int(file_id)))

    def by(self, key):
        entry = self.by_key.get(key)
        if entry is not None:
            # STREAM_TTL is an idle timeout, not a maximum movie duration.
            self.by_key.set(key, entry)
            current = self.by_file.get((entry['embed'], entry['file_id']))
            if current is entry:
                self.by_file.set((entry['embed'], entry['file_id']), entry)
        return entry


class StreamError(RuntimeError):
    pass


# Подменяет `active` в fileList, если iframe открыт с name="kkfile:<id>".
# Тогда guard плеера сам подписывает первый запрос именно для нужного файла.
INIT_JS = r"""(() => {
  const m = /^kkfile:(\d+)$/.exec(window.name || '');
  if (!m) return;
  const wanted = Number(m[1]);
  const original = JSON.parse;
  JSON.parse = function (text, reviver) {
    const value = original.call(JSON, text, reviver);
    try {
      if (value && value.active && value.all && Number(value.active.id) !== wanted) {
        let found = null;
        const walk = node => {
          if (!node || found || typeof node !== 'object') return;
          if (Number(node.id) === wanted) { found = node; return; }
          Object.values(node).forEach(walk);
        };
        walk(value.all);
        if (found) value.active = found;
      }
    } catch (e) {}
    return value;
  };
})();"""

# Один шаг собственного меню плеера (версия / сезон / серия / озвучка).
# Каждый клик порождает подписанный запрос, а KinoKrad отвергает (404)
# подписи, выданные в ту же секунду, поэтому шаги выполняются по одному.
STEP_JS = r"""(step) => {
  const items = s => [...s.querySelectorAll('.select__drop-item')];
  const kind = s => {
    const first = items(s)[0];
    if (!first) return '';
    const text = (first.textContent || '').trim();
    const id = first.dataset.id || '';
    if (/^Сезон/i.test(text)) return 'season';
    if (/^Серия/i.test(text)) return 'episode';
    if (/^t\d+/.test(id)) return 'translation';
    return 'group';
  };
  const select = [...document.querySelectorAll('.selects .select')].find(x => kind(x) === step.kind);
  if (!select) return 'missing';
  const list = items(select);
  let it = list.find(i => (i.dataset.id || '') === step.exact);
  if (!it && step.prefix) it = list.find(i => (i.dataset.id || '').split('|')[0] === step.prefix);
  if (!it) return 'noitem';
  if (it.classList.contains('active')) return 'same';
  it.click();
  return 'clicked';
}"""


def menu_steps(target):
    steps = []
    if target.get("group"):
        steps.append({"kind": "group", "exact": target["group"]})
    if target.get("season") is not None:
        steps.append({"kind": "season", "exact": str(target["season"])})
    if target.get("episode") is not None:
        steps.append({"kind": "episode", "exact": str(target["episode"])})
    if target.get("tid") is not None:
        tid = "t%s" % target["tid"]
        steps.append({"kind": "translation", "exact": tid + ("|" + target["quality"] if target.get("quality") else ""),
                      "prefix": tid})
    return steps


SIGN_GAP = float(os.getenv("SIGN_GAP", "1.1"))



class PlayerSession:
    def __init__(self, embed, page_url):
        self.embed = embed
        self.page_url = page_url
        self.page = None
        self.context = None
        self.file_id = None
        self.want = None
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.last_media_used = 0.0
        self.player_ready = asyncio.Event()
        self.inflight = set()
        self.last_signed = 0.0
        self.cdn_headers = {}
        self.live_headers = {}
        self.header_time = 0.0
        self.header_event = asyncio.Event()
        self.header_lock = asyncio.Lock()

    async def quiet(self, timeout):
        """Ждёт, пока нет запросов в пути и прошла секунда с прошлой подписи."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.inflight and time.monotonic() - self.last_signed >= SIGN_GAP:
                return True
            await asyncio.sleep(0.05)
        return False

    def frame(self):
        if not self.page or self.page.is_closed():
            return None
        for frame in self.page.frames:
            if P.is_player_url(frame.url):
                return frame
        return None


class MediaRoute:
    """Маршрут медиатрафика: DIRECT по умолчанию, автоматический фолбэк на VPN.

    Состояние общее для браузера подписи (Playwright) и медиазагрузчика,
    потому что сессии CDN привязаны к выходному адресу. Причина повторных
    вечерних manifestLoadError — CDN перестаёт принимать DIRECT-соединения
    (403 token_decrypt / session_blocked); тогда весь медиатрафик (браузер
    подписи, WS, плейлисты, сегменты) переезжает на FALLBACK_PROXY, а затем
    """
    route = "direct"           # direct | vpn | probe
    fail = 0
    last_fail = 0.0
    probe_at = 0.0
    browser_dirty = False
    on_flip = None             # колбэк: перезапуск Chromium при смене маршрута

    @classmethod
    def _announce(cls):
        if cls.on_flip and cls.browser_dirty:
            try:
                asyncio.ensure_future(cls.on_flip())
            except Exception as logexc:
                log.info("route flip callback failed: %s", logexc)

    @classmethod
    def proxy_media(cls):
        force = os.getenv("KK_FORCE_ROUTE")
        if FALLBACK_PROXY and force == "vpn":
            return FALLBACK_PROXY
        if force == "direct":
            return UPSTREAM_PROXY
        if cls.route == "vpn" and FALLBACK_PROXY:
            if time.monotonic() >= cls.probe_at:
                cls.route = "probe"
                cls.probe_at = time.monotonic() + 900
            else:
                return FALLBACK_PROXY
        return UPSTREAM_PROXY

    @classmethod
    def proxy_browser(cls):
        force = os.getenv("KK_FORCE_ROUTE")
        if FALLBACK_PROXY and force == "vpn":
            return FALLBACK_PROXY
        if force == "direct":
            return UPSTREAM_PROXY
        if cls.route in ("vpn", "probe") and FALLBACK_PROXY:
            return FALLBACK_PROXY
        return UPSTREAM_PROXY

    @classmethod
    def note(cls, exc):
        """exc: None — успех; иначе строка/исключение последнего ответа."""
        now = time.monotonic()
        if exc is None:
            # Успех возвращает на DIRECT только если это был контрольный
            # DIRECT-запрос (probe). Успех через VPN просто сбрасывает счётчик
            # отказов и держит VPN, пока DIRECT не станет рабочим.
            cls.fail = 0
            if cls.route == "probe":
                cls.route = "direct"
                cls.last_fail = 0.0
                cls.browser_dirty = True
                log.info("media route: -> direct (probe OK)")
                cls._announce()
            return
        text = str(exc)
        if not re.search(r"HTTP (401|403|429)\b", text):
            return
        if cls.route == "vpn":
            return
        if cls.last_fail and now - cls.last_fail <= 240:
            cls.fail += 1
        else:
            cls.fail = 1
        cls.last_fail = now
        if not FALLBACK_PROXY:
            return
        if cls.fail >= 2:
            cls.route = "vpn"
            cls.probe_at = now + 900
            cls.browser_dirty = True
            cls._announce()
            log.info("media route: -> vpn (repeated upstream %s) — fallback proxy %s",
                     text[-140:], FALLBACK_PROXY)
        elif cls.route == "probe":
            cls.route = "vpn"
            cls.probe_at = now + 1800
            log.info("media route: direct probe failed -> vpn")


class Browser:
    """Один постоянный Chromium и несколько прогретых страниц плеера."""

    def __init__(self, streams):
        self.streams = streams
        self.pw = None
        self.browser = None
        self.ctx = None
        self.sessions = OrderedDict()
        self.start_lock = asyncio.Lock()
        self.waiters = {}
        self.flight = SingleFlight()

    async def ensure(self):
        global UA
        async with self.start_lock:
            want_proxy = MediaRoute.proxy_browser()
            if self.browser and self.browser.is_connected():
                # Прокси Chromium фиксируется при старте; при смене маршрута
                # медиасессий его нужно перезапустить на новый выход.
                if getattr(self, "_launched_proxy", None) == want_proxy:
                    return
                try:
                    await self.browser.close()
                except Exception:
                    pass
                self.browser = None
                self.sessions.clear()
            from playwright.async_api import async_playwright
            started = time.monotonic()
            if self.pw is None:
                self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(channel=os.getenv("KK_BROWSER_CHANNEL", "chrome"), headless=True,
                proxy={"server": want_proxy} if want_proxy else None, args=[
                # QUIC (UDP) через VPN-туннель в десятки раз медленнее TCP:
                # 767-КБ бандл плеера грузился >25 с против 0,3 с по TCP.
                "--disable-quic", "--disable-blink-features=AutomationControlled",
                "--mute-audio", "--no-first-run", "--disable-gpu",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ])
            UA = re.sub(r"Chrome/[0-9.]+", "Chrome/" + self.browser.version.split(".")[0] + ".0.0.0", UA)
            self.ctx = await self.browser.new_context(
                user_agent=UA, locale="ru-RU", viewport={"width": 1280, "height": 720})
            await self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await self.ctx.add_init_script(INIT_JS)
            await self.ctx.route("**/*", self._filter)
            self.sessions.clear()
            self._launched_proxy = want_proxy
            log.info("chromium started in %.2fs (proxy=%s)", time.monotonic() - started, want_proxy or "direct")

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.pw:
                await self.pw.stop()

    @staticmethod
    async def _filter(route, _request=None):
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
        else:
            await route.fallback()

    # ---- ожидание ответов плеера -------------------------------------
    def _waiter(self, embed, file_id):
        key = (embed, int(file_id))
        fut = self.waiters.get(key)
        if fut is None or fut.done():
            fut = asyncio.get_running_loop().create_future()
            # исключение считается прочитанным, даже если никто не ждёт
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            self.waiters[key] = fut
        return fut

    def _settle(self, embed, file_id, data=None, error=None):
        fut = self.waiters.pop((embed, int(file_id)), None)
        if fut and not fut.done():
            if error:
                fut.set_exception(StreamError(error))
            else:
                fut.set_result(data)

    @staticmethod
    def _file_id(url):
        try:
            return int(urlparse(url).path.rstrip("/").rsplit("/", 1)[1])
        except (ValueError, IndexError):
            return None

    def _on_request(self, sess, request):
        if P.allowed_media_url(request.url) and 'authorizations' in request.headers:
            sess.cdn_headers[request.url] = {k.title(): v for k, v in request.headers.items()
                if k.lower() in ('authorizations', 'accepts-controls', 'origin', 'referer', 'user-agent')}
            sess.live_headers = sess.cdn_headers[request.url]
            sess.header_time = time.monotonic()
            sess.header_event.set()
            if len(sess.cdn_headers) > 200:
                sess.cdn_headers = {request.url: sess.cdn_headers[request.url]}
        if "/bnsi/movies/" in request.url:
            fid = self._file_id(request.url)
            if fid is not None:
                sess.inflight.add(fid)
                sess.last_signed = time.monotonic()
                sess.player_ready.set()

    def _on_response(self, sess, response):
        if "/bnsi/movies/" in response.url:
            asyncio.ensure_future(self._read(sess, response))

    async def _read(self, sess, response):
        fid = self._file_id(response.url)
        if fid is None:
            return
        try:
            data = await response.json() if response.ok else None
            error = None if response.ok else "KinoKrad player HTTP %d" % response.status
        except Exception as exc:
            data, error = None, "unreadable player response: %s" % exc
        sess.inflight.discard(fid)
        if data and data.get("hlsSource"):
            # CDN now requires session headers generated by its own player.
            # Capture the first actual playlist request for THIS returned source.
            urls = [url for source in data['hlsSource'] for raw in (source.get('quality') or {}).values()
                    for url in str(raw).split(' or ')]
            captured = {}
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                captured = next((sess.cdn_headers[u] for u in urls if u in sess.cdn_headers), {})
                if captured:
                    break
                await asyncio.sleep(0.05)
            entry = self.streams.put(sess.embed, fid, data, sess.page_url)
            # URL in HTML may contain upper-case hostname; Chromium normalizes
            # its Referer. CDN compares it strictly and rejects mismatched origins.
            entry['media_headers'] = {k: v for k, v in response.request.headers.items()
                                      if k.lower() in ('origin', 'referer', 'user-agent')}
            entry['media_headers'].update(captured)
            entry['session'] = sess
            log.info("stream ready file=%s audios=%d live_headers=%s", fid, len(data["hlsSource"]), bool(captured))
            self._settle(sess.embed, fid, data=entry)
        else:
            self._settle(sess.embed, fid, error=error or "KinoKrad returned no HLS sources")

    # ---- страницы ------------------------------------------------------
    async def sign_headers(self, entry, url):
        # Маршрут мог смениться (фолбэк/возврат на DIRECT): ensure() перезапускает
        # Chromium на нужном прокси; старые сессии при этом закрываются и
        # resolve сам поднимет новую генерацию.
        await self.ensure()
        sess = entry.get('session')
        if not sess or not sess.page or sess.page.is_closed():
            raise StreamError('player session unavailable; refresh stream')
        sess.last_used = time.monotonic()
        sess.last_media_used = sess.last_used
        async with sess.header_lock:
            # Renew through the original HLS loader. Calling the private token
            # generator out of its native request path can invalidate a session.
            if time.monotonic() - sess.header_time > 30:
                sess.header_event.clear()
                frame = sess.frame()
                if not frame:
                    raise StreamError('player frame unavailable')
                try:
                    await frame.evaluate("""() => {
                    const p = window.player;
                    if (!p || !p.hls || !p.hls.url) throw new Error('native loader unavailable');
                    p.hls.loadSource(p.hls.url);
                    p.hls.startLoad(-1);
                    }""")
                    await asyncio.wait_for(sess.header_event.wait(), 8)
                except Exception as exc:
                    raise StreamError('player session cannot renew; refresh stream') from exc
            return dict(sess.live_headers)

    async def _session(self, embed, page_url, file_id=None, fresh=False):
        await self.ensure()
        key = (embed, file_id)
        sess = self.sessions.get(key)
        if fresh and sess is not None:
            # In-flight requests retain their old context/credentials. Never
            # turn the old session object into a new generation in place.
            self.sessions.pop(key)
            self.sessions[(embed, file_id, secrets.token_hex(6))] = sess
            sess = None
        if sess is None:
            sess = PlayerSession(embed, page_url)
            sess.file_id = file_id
            self.sessions[key] = sess
        self.sessions.move_to_end(key)
        sess.last_used = time.monotonic()
        await self._evict()
        return sess

    async def _evict(self):
        while len(self.sessions) > MAX_PLAYER_PAGES:
            candidates = [(key, sess) for key, sess in self.sessions.items()
                          if not sess.lock.locked() and time.monotonic() - sess.last_media_used > 60]
            if not candidates:
                break
            old_key, old = min(candidates, key=lambda item: (item[1].file_id is not None, item[1].last_used))
            self.sessions.pop(old_key)
            await self._close_page(old)

    @staticmethod
    async def _close_page(sess):
        try:
            if sess.page and not sess.page.is_closed():
                await sess.page.close()
        except Exception:
            pass
        sess.page = None
        if sess.context:
            await sess.context.close()
            sess.context = None

    async def reap_idle(self):
        now = time.monotonic()
        for key, sess in list(self.sessions.items()):
            if now - sess.last_used > PAGE_IDLE and not sess.lock.locked():
                self.sessions.pop(key, None)
                await self._close_page(sess)

    async def _load(self, sess, file_id=None):
        if sess.page is None or sess.page.is_closed():
            # Each selected episode owns its cookies/storage/session. Hovering
            # another voice or preparing the next episode must not revoke it.
            sess.context = await self.browser.new_context(user_agent=UA, locale="ru-RU", viewport={"width": 1280, "height": 720})
            await sess.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await sess.context.add_init_script(INIT_JS)
            await sess.context.route("**/*", self._filter)
            sess.page = await sess.context.new_page()
            sess.page.on("request", lambda r, s=sess: self._on_request(s, r))
            sess.page.on("response", lambda r, s=sess: self._on_response(s, r))

            async def fulfill(route, _request=None, s=sess):
                name = "kkfile:%d" % s.want if s.want else "kk"
                body = ('<!doctype html><meta charset="utf-8"><iframe name="%s" src="%s" '
                        'width="1280" height="720" allow="autoplay; fullscreen"></iframe>'
                        % (name, html_lib.escape(s.embed, quote=True)))
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

            await sess.page.route(sess.page_url, fulfill)
        sess.want = int(file_id) if file_id else None
        sess.player_ready = asyncio.Event()
        sess.inflight = set()
        await sess.page.goto(sess.page_url, wait_until="domcontentloaded", timeout=20000)

    async def warm(self, embed, page_url):
        if not (P.is_player_url(embed) and P.allowed_page_url(page_url)):
            return
        try:
            sess = await self._session(embed, page_url)
            if sess.page and not sess.page.is_closed():
                return
            async with sess.lock:
                if not sess.page or sess.page.is_closed():
                    started = time.monotonic()
                    await self._load(sess)
                    log.info("player warmed in %.2fs", time.monotonic() - started)
        except Exception as exc:
            log.warning("player warm failed: %s", exc)

    async def resolve(self, embed, page_url, file_id, target=None, force=False):
        if not force:
            hit = self.streams.get(embed, file_id)
            if hit:
                return hit
        key = (embed, int(file_id), bool(force))
        return await self.flight.run(("resolve",) + key, lambda: self._resolve(embed, page_url, int(file_id), target or {}, force))

    async def _resolve(self, embed, page_url, file_id, target, force):
        for attempt in range(2):
            try:
                return await self._resolve_once(embed, page_url, file_id, target, force)
            except StreamError:
                raise
            except Exception as exc:
                # Обычно — упавший Chromium/страница: перезапуск и повтор.
                log.warning("resolve attempt %d failed: %s", attempt + 1, exc)
                if attempt:
                    raise
                sess = self.sessions.pop((embed, file_id), None)
                if sess:
                    await self._close_page(sess)
                if self.browser and not self.browser.is_connected():
                    self.browser = None

    async def _resolve_once(self, embed, page_url, file_id, target, force):
        started = time.monotonic()
        sess = await self._session(embed, page_url, file_id, fresh=force)
        async with sess.lock:
            sess.last_used = time.monotonic()
            hit = None if force else self.streams.get(embed, file_id)
            if hit:
                return hit
            page_alive = sess.page is not None and not sess.page.is_closed()

            # Страница уже грузится (прогрев): ждём, пока плеер оживёт.
            if page_alive and not sess.player_ready.is_set():
                try:
                    await asyncio.wait_for(sess.player_ready.wait(), 12)
                except asyncio.TimeoutError:
                    pass
                hit = None if force else self.streams.get(embed, file_id)
                if hit:
                    return hit
            # Нужный файл уже запрошен плеером — просто дождаться ответа.
            if page_alive and file_id in sess.inflight and not force:
                fut = self._waiter(embed, file_id)
                try:
                    entry = await asyncio.wait_for(asyncio.shield(fut), LOAD_WAIT)
                    log.info("resolve file=%s via in-flight request in %.2fs", file_id, time.monotonic() - started)
                    return entry
                except (asyncio.TimeoutError, StreamError):
                    pass

            # Быстрый путь: меню плеера на прогретой странице, по шагам.
            frame = sess.frame() if page_alive else None
            steps = menu_steps(target)
            if frame is not None and sess.player_ready.is_set() and steps:
                fut = self._waiter(embed, file_id)
                outcome = "no-click"
                for step in steps:
                    await sess.quiet(LOAD_WAIT)
                    if fut.done():
                        break
                    try:
                        result = await frame.evaluate(STEP_JS, step)
                    except Exception as exc:
                        result = "error: %s" % exc
                    if result == "clicked":
                        outcome = "clicked"
                        await asyncio.sleep(0.15)  # дать плееру отправить запрос
                    elif result != "same":
                        outcome = "%s:%s" % (step["kind"], result)
                        break
                if outcome == "clicked" or fut.done():
                    try:
                        entry = await asyncio.wait_for(asyncio.shield(fut), CLICK_WAIT)
                        log.info("resolve file=%s via player menu in %.2fs", file_id, time.monotonic() - started)
                        return entry
                    except (asyncio.TimeoutError, StreamError) as exc:
                        log.info("player menu did not give file=%s (%s), reloading", file_id, exc)
                else:
                    log.info("player menu not usable for file=%s: %s", file_id, outcome)

            # Надёжный путь: перезагрузка iframe с нужным active-файлом.
            for attempt in range(2):
                await sess.quiet(5)
                fut = self._waiter(embed, file_id)
                await self._load(sess, file_id)
                try:
                    entry = await asyncio.wait_for(asyncio.shield(fut), LOAD_WAIT)
                except StreamError as exc:
                    if attempt:
                        raise
                    log.info("page load for file=%s failed (%s), retrying", file_id, exc)
                    continue
                log.info("resolve file=%s via page load in %.2fs", file_id, time.monotonic() - started)
                return entry


# --------------------------------------------------------------------------
# Видео-прокси с упреждающей загрузкой сегментов
# --------------------------------------------------------------------------

# TLS-отпечаток Chrome для медиазапросов: CDN (VK) блокирует клиенты по
# fingerprint (client_blocked на сочистом aiohttp/Python), Chrome проходит.
IMPERSONATE = os.getenv("KK_IMPERSONATE", "chrome")


class MediaProxy:
    def __init__(self, streams):
        self.streams = streams
        self.signer = None
        self.http = None
        self.cf = None
        self.cache = ByteLRU(SEGMENT_CACHE_MB * 1024 * 1024)
        self.inflight = {}
        self.position = {}  # url сегмента -> (список url, индекс)
        # Сегменты качаются по очереди (FIFO), не делят канал на шестерых:
        # нужный плееру следующий сегмент приходит первым.
        self.sem = asyncio.Semaphore(int(os.getenv("SEGMENT_PARALLEL", "2")))
        self.stats = {"hit": 0, "wait": 0, "miss": 0, "prefetched": 0, "refreshed": 0}
        self.last_segments = []
        self.bad_hosts = {}  # узел CDN -> до какого времени считать его сломанным

    async def start(self):
        self.http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=48, ttl_dns_cache=300, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
            auto_decompress=False,
            proxy=UPSTREAM_PROXY,
        )
        if CFAsyncSession:
            # Медиа идёт через curl_cffi с отпечатком Chrome; aiohttp остаётся
            # лишь вспомогательным клиентом (у него другой TLS-fingerprint,
            # по которому VK Video отдаёт client_blocked).
            self.cf = CFAsyncSession(impersonate=IMPERSONATE, timeout=60)

    async def close(self):
        if self.cf:
            await self.cf.close()
        if self.http:
            await self.http.close()

    @staticmethod
    def headers(entry, extra=None):
        embed = urlparse(entry["embed"])
        headers = {
            "User-Agent": UA, "Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": embed._replace(netloc=embed.netloc.lower()).geturl(), "Origin": "%s://%s" % (embed.scheme, embed.hostname),
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "cross-site",
        }
        headers.update({k.title(): v for k, v in entry.get("media_headers", {}).items()})
        headers.update(extra or {})
        return headers

    @staticmethod
    def proxy_url(base, key, url):
        return "%s/p/%s?u=%s" % (base, key, b64e(url))

    def absolutize(self, text, source):
        """Превращает плейлист в набор абсолютных upstream-ссылок БЕЗ
        прокси: файлы собирает сам телевизор ( как в online_mod)."""
        def abs_uri(m):
            quote, uri = m.group(1), m.group(2)
            absolute = urljoin(source, uri.strip())
            if P.allowed_media_url(absolute):
                return "URI=" + quote + absolute + quote
            return m.group(0)
        output = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                output.append(raw)
                continue
            if line.startswith("#"):
                output.append(re.sub(r'URI=("|\')([^"\']+)(\1)', abs_uri, raw))
            else:
                output.append(urljoin(source, line))
        return "\n".join(output) + "\n"

    def rewrite(self, text, source, base, key):
        """Переписывает HLS-плейлист на прокси и запоминает порядок сегментов."""
        def wrap(value):
            absolute = urljoin(source, value.strip())
            return self.proxy_url(base, key, absolute) if P.allowed_media_url(absolute) else absolute

        output, segments = [], []
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                output.append(re.sub(r'URI=("|\')([^"\']+)(\1)',
                                     lambda m: "URI=" + m.group(1) + wrap(m.group(2)) + m.group(3), raw))
            elif line:
                absolute = urljoin(source, line)
                if not line.endswith(".m3u8"):
                    segments.append(absolute)
                output.append(wrap(line))
            else:
                output.append(raw)
        for index, url in enumerate(segments):
            self.position[url] = (segments, index)
        self.last_segments = segments
        if len(self.position) > 200000:
            self.position.clear()
        return "\n".join(output) + "\n"

    async def request_headers(self, entry, url, extra=None):
        headers = self.headers(entry)
        if self.signer and ('session' in entry or 'signer_id' in entry):
            headers.update({k.title(): v for k, v in (await self.signer(entry, url)).items()})
        headers.update(extra or {})
        return headers

    @staticmethod
    def upstream_url(entry, url):
        return Backup.cdn_url(url, entry['backup_clock']) if entry.get('provider') == 'backup' else url

    async def _page_fetch(self, entry, url, headers=None, binary=False, timeout=30):
        """Запрос изнутри живой страницы плеера — те же куки/ Origin/TLS и
        сессия, что у штатного плеера. Именно такие запросы CDN пропускает,
        когда Python-клиенту (даже с отпечатком Chrome) отвечает 403."""
        sess = entry.get('session') if isinstance(entry, dict) else None
        if sess is None:
            raise RuntimeError('entry uses HTTP transport without a browser session')
        page = getattr(sess, 'page', None)
        if page is None or page.is_closed():
            raise StreamError('player session unavailable')
        # Origin/Referer/UA выставляет сам браузер — вручную их не дублируем.
        js_headers = {k: v for k, v in (headers or {}).items()
                      if k.lower() not in ('origin', 'referer', 'user-agent', 'sec-fetch-dest',
                                           'sec-fetch-mode', 'sec-fetch-site', 'cookie')}
        # Запрос исполняем в iframe плеера: CDN сверяет Origin с тем, что
        # отсылает штатный XHR из плеера (куки и TLS те же, что и у плеера).
        target = None
        try:
            target = sess.frame()
        except Exception:
            target = None
        if target is None:
            target = page
        result = await asyncio.wait_for(target.evaluate(self.PAGE_FETCH_JS,
                                                      {"url": self.upstream_url(entry, url),
                                                       "headers": js_headers, "binary": binary}),
                                        timeout)
        if not isinstance(result, dict):
            raise RuntimeError("in-page fetch: bad result")
        if result.get("status") != 200:
            err = "upstream HTTP %s%s" % (result.get("status"),
                                          " (%s)" % result["reason"] if result.get("reason") else "")
            raise RuntimeError(err)
        if binary:
            return base64.b64decode(result["b64"])
        return result["text"]

    PAGE_FETCH_JS = """async ({url, headers, binary}) => {
  const init = {method: 'GET', headers: headers || {}, credentials: 'include', cache: 'default'};
  let r;
  try { r = await fetch(url, init); }
  catch (e) { return {status: 0, reason: String(e).slice(0, 120)}; }
  if (!r.ok) return {status: r.status, reason: (r.headers.get('X-VD') || '')};
  if (binary) {
    const buf = await r.arrayBuffer();
    const u8 = new Uint8Array(buf);
    let s = '', CH = 0x8000;
    for (let i = 0; i < u8.length; i += CH) {
      s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    }
    return {status: 200, b64: btoa(s)};
  }
  return {status: 200, text: await r.text()};
}"""

    async def fetch_text(self, url, entry, attempts=3):
        # Первым делом — fetch из живой страницы (самый достоверный клиент).
        try:
            headers = await self.request_headers(entry, url)
            text = await self._page_fetch(entry, url, headers, binary=False, timeout=30)
            MediaRoute.note(None)
            final = self.upstream_url(entry, url)
            if entry.get("provider") == "backup":
                try:
                    final = Backup.canonical_url(url, final)
                except Exception:
                    pass
            return text, final
        except StreamError:
            raise
        except Exception as exc:
            MediaRoute.note(exc)
            last = exc
        last = None
        proxy = MediaRoute.proxy_media()
        if self.cf is not None:
            # curl_cffi (отпечаток Chrome): CDИ block plain-python clients.
            for attempt in range(attempts):
                try:
                    r = await self.cf.get(self.upstream_url(entry, url), headers=await self.request_headers(entry, url),
                                          proxy=proxy, timeout=20)
                    if r.status_code != 200:
                        last = RuntimeError("upstream HTTP %d%s" % (r.status_code,
                            " (" + r.headers.get("X-VD") + ")" if r.headers.get("X-VD") else ""))
                        MediaRoute.note(last)
                        if 400 <= r.status_code < 500:
                            break  # узел CDN отказал — повтор не поможет
                        continue
                    MediaRoute.note(None)
                    return r.text, (Backup.canonical_url(url, str(r.url)) if entry.get("provider") == "backup" else str(r.url))
                except Exception as exc:
                    last = exc
                    MediaRoute.note(exc)
                    await asyncio.sleep(0.3 * (attempt + 1))
            raise RuntimeError("playlist fetch failed: %s" % last)
        for attempt in range(attempts):
            try:
                async with self.http.get(self.upstream_url(entry, url), headers=await self.request_headers(entry, url),
                                         timeout=aiohttp.ClientTimeout(total=20),
                                         proxy=MediaRoute.proxy_media()) as r:
                    if r.status != 200:
                        last = RuntimeError("upstream HTTP %d%s" % (r.status, " (" + r.headers["X-VD"] + ")" if r.headers.get("X-VD") else ""))
                        MediaRoute.note(last)
                        if 400 <= r.status < 500:
                            break  # узел CDN отказал — повтор не поможет
                        raise last
                    MediaRoute.note(None)
                    return await r.text(errors="replace"), (Backup.canonical_url(url, str(r.url)) if entry.get("provider") == "backup" else str(r.url))
            except Exception as exc:
                last = exc
                MediaRoute.note(exc)
                await asyncio.sleep(0.3 * (attempt + 1))
        raise RuntimeError("playlist fetch failed: %s" % last)

    async def fetch_first(self, urls, entry, memo_key):
        """Перебирает зеркала CDN ("url1 or url2"), запоминая рабочее."""
        good = entry.setdefault("good", {})
        now = time.monotonic()
        # сначала проверенное зеркало, сломанные узлы — в конец
        order = sorted(urls, key=lambda u: (u != good.get(memo_key),
                                            self.bad_hosts.get(urlparse(u).hostname, 0) > now))
        last = None
        for url in order:
            try:
                text, final_url = await self.fetch_text(url, entry, attempts=2)
            except Exception as exc:
                last = exc
                if "HTTP 403" in str(exc) or "HTTP 404" in str(exc):
                    self.bad_hosts[urlparse(url).hostname] = time.monotonic() + 600
                continue
            if good.get(memo_key) != url:
                good[memo_key] = url
                if url != urls[0]:
                    log.info("CDN mirror fallback: %s -> %s", urlparse(urls[0]).hostname, urlparse(url).hostname)
            return text, final_url
        raise RuntimeError(str(last))

    async def _download(self, url, entry):
        # Apply the same bound to curl_cffi and aiohttp, including prefetch.
        async with self.sem:
            try:
                return await self._download_bounded(url, entry)
            except Exception as exc:
                log.info("segment session failed: %s", exc)
                return None

    async def _download_bounded(self, url, entry):
        last = None
        headers = await self.request_headers(entry, url)
        upurl = self.upstream_url(entry, url)
        # v3.4.4: curl_cffi — первый клиент (Chrome-отпечаток). In-page fetch
        # на userplay-хосте стабильно падает с TypeError (CORS) и съедал
        # ~0,5 с на каждый сегмент — источник подлагивания.
        for attempt in range(3):
            try:
                if self.cf is not None:
                    r = await self.cf.get(upurl, headers=headers, proxy=MediaRoute.proxy_media(),
                                          timeout=90)
                    if r.status_code != 200:
                        raise RuntimeError("upstream HTTP %d" % r.status_code)
                    body = r.content
                    ctype = r.headers.get("Content-Type", "video/mp4") or "video/mp4"
                else:
                    async with self.http.get(upurl, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=60, sock_connect=10),
                                             proxy=MediaRoute.proxy_media()) as r:
                        if r.status != 200:
                            raise RuntimeError("upstream HTTP %d" % r.status)
                        body = await r.read()
                        ctype = r.headers.get("Content-Type", "video/mp4")
                    MediaRoute.note(None)
                MediaRoute.note(None)
                self.cache.put(url, body, ctype)
                return body, ctype
            except Exception as exc:
                last = exc
                MediaRoute.note(exc)
                if re.search(r"HTTP (401|403|410|429)\b", str(exc)):
                    break  # Repeating a rejected session cannot renew it.
                await asyncio.sleep(0.4 * (attempt + 1))
        log.info("segment prefetch failed: %s", last)
        return None

    def _start_download(self, url, entry):
        if self.cache.get(url) is not None or url in self.inflight:
            return
        task = asyncio.ensure_future(self._download(url, entry))
        self.inflight[url] = task
        task.add_done_callback(lambda _t, u=url: self.inflight.pop(u, None))

    def warm_start(self, entry):
        """Первые сегменты нового плейлиста — до того, как их попросит плеер."""
        for url in (self.last_segments or [])[:1]:
            self._start_download(url, entry)

    def prefetch_after(self, url, entry):
        pos = self.position.get(url)
        if not pos:
            return
        segments, index = pos
        for nxt in segments[index + 1:index + 1 + PREFETCH_AHEAD]:
            if self.cache.get(nxt) is None and nxt not in self.inflight:
                self.stats["prefetched"] += 1
                self._start_download(nxt, entry)

    async def segment(self, request, url, entry):
        self.prefetch_after(url, entry)
        range_header = request.headers.get("Range")
        cached = self.cache.get(url)
        if cached is None and url in self.inflight:
            self.stats["wait"] += 1
            cached = await asyncio.shield(self.inflight[url])
        elif cached is not None:
            self.stats["hit"] += 1
        if cached is not None:
            return self._serve_bytes(cached[0], cached[1], range_header)

        # Промах: качаем полностью (кэш + Range нарезаем локально) — единый
        # HTTP-клиент curl_cffi, чтобы не плодить разные TLS-отпечатки.
        self.stats["miss"] += 1
        got = self.cache.get(url)
        if got is None and url in self.inflight:
            self.stats["wait"] += 1
            try:
                got = await asyncio.shield(self.inflight[url])
            except Exception:
                got = None
        if got is None:
            got = await self._download(url, entry)
        if not got:
            return web.Response(status=502, text="upstream error")
        body, ctype = got
        return self._serve_bytes(body, ctype, range_header)

    @staticmethod
    def _serve_bytes(body, ctype, range_header):
        total = len(body)
        m = re.match(r"bytes=(\d*)-(\d*)$", range_header or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
            else:
                start, end = max(0, total - int(m.group(2))), total - 1
            end = min(end, total - 1)
            if start > end:
                return web.Response(status=416, headers={"Content-Range": "bytes */%d" % total})
            return web.Response(status=206, body=body[start:end + 1], content_type=None, headers={
                "Content-Type": ctype, "Content-Range": "bytes %d-%d/%d" % (start, end, total),
                "Accept-Ranges": "bytes"})
        return web.Response(body=body, content_type=None, headers={"Content-Type": ctype, "Accept-Ranges": "bytes"})


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

def base_url(request):
    return "%s://%s" % (request.scheme, request.host)


def jerror(message, status=502):
    return web.json_response({"error": str(message)}, status=status)


def target_from(query):
    target = {}
    for name in ("season", "episode", "tid"):
        if (query.get(name) or "").isdigit():
            target[name] = int(query[name])
    for name in ("quality", "group"):
        if query.get(name):
            target[name] = query[name][:40]
    return target


def quality_alternatives(qualities, height):
    raw = str(qualities.get(str(height), qualities.get(height, "")) or "")
    return [u for u in (P.clean(x) for x in raw.split(" or ")) if P.allowed_media_url(u)]


def stream_payload(entry, base, prefer=""):
    audios = []
    for index, audio in enumerate(entry["data"].get("hlsSource") or []):
        qualities = audio.get("quality") or {}
        heights = sorted((int(h) for h in qualities if str(h).isdigit() and int(h) <= MAX_HEIGHT), reverse=True)
        urls = OrderedDict()
        for height in heights:
            if quality_alternatives(qualities, height):
                urls["%dp" % height] = "%s/hls/%s/%d/%d.m3u8" % (base, entry["key"], index, height)
        if not urls:
            continue
        audios.append({
            "index": index,
            "label": audio.get("label") or "Аудиодорожка %d" % (index + 1),
            "audio_id": audio.get("audioId"),
            "url": next(iter(urls.values())),
            "quality": urls,
            "qualities": [int(q[:-1]) for q in urls],
        })
    tracks = []
    for track in entry["data"].get("tracks") or []:
        src = track.get("src", "")
        if P.allowed_media_url(src):
            tracks.append({**track, "src": MediaProxy.proxy_url(base, entry["key"], src)})
    default = match_audio(audios, prefer)
    if default is None and audios and len({a["label"] for a in audios}) == 1:
        default = 0  # подписи одинаковые — выбирать по ним нечего
    return {"key": entry["key"], "file_id": entry["file_id"], "audios": audios, "tracks": tracks,
            "default_audio": default}


def make_app():
    site = Site()
    streams = StreamStore()
    browser = Browser(streams)
    media = MediaProxy(streams)
    media.signer = browser.sign_headers

    async def _route_flip():
        # Смена выхода (DIRECT<->VPN): кэш «сломанных» узлов CDN и адрес
        # Chromium больше не соответствуют действительности.
        media.bad_hosts.clear()
        log.info("route flip: media bad-hosts cleared, relaunching Chromium")
        await browser.ensure()

    MediaRoute.on_flip = _route_flip
    routes = web.RouteTableDef()
    backup_cache = TTLCache(600, 100)
    backup_flight = SingleFlight()

    async def resolve_backup(embed, page_url, file_id, target, force=False):
        async def resolve():
            detail = await site.detail(page_url)
            alternate = detail.get('backup_embed_url')
            if not alternate:
                raise StreamError('У этой карточки нет второго плеера')
            cache_key = (alternate, target.get('season'), target.get('episode'))
            hit = None if force else backup_cache.get(cache_key)
            if hit and streams.by(hit['key']):
                return hit
            response = await site.session.get(alternate, proxy=UPSTREAM_PROXY,
                headers={'Referer': page_url, 'User-Agent': UA})
            if response.status_code != 200:
                raise StreamError('Видео 2: сервер недоступен')
            server_clock = Backup.clock(response.text)
            source = Backup.episode_source(response.text, target.get('season'), target.get('episode'))
            if not P.allowed_media_url(source):
                raise StreamError('Видео 2: неподдерживаемый сервер видео')
            headers = {'Referer': 'https://api.nextembed.ws/', 'Origin': 'https://api.nextembed.ws', 'User-Agent': UA}
            response = await site.session.get(Backup.cdn_url(source, server_clock), proxy=UPSTREAM_PROXY, headers=headers)
            if response.status_code != 200:
                raise StreamError('Видео 2: плейлист недоступен')
            master = Backup.Master(response.text, Backup.canonical_url(source, str(response.url)), P.allowed_media_url)
            data = {'hlsSource': [{'label': master.label(i), 'audioId': str(i),
                'quality': {str(h): source for h in master.heights}} for i in range(len(master.names))], 'tracks': []}
            entry = streams.put(alternate, file_id, data, page_url)
            entry.update(provider='backup', backup_master=master, backup_clock=server_clock, media_headers=headers, target=dict(target))
            backup_cache.set(cache_key, entry)
            log.info('Video 2 ready season=%s episode=%s qualities=%s', target.get('season'), target.get('episode'), master.heights)
            return entry
        return await backup_flight.run((page_url, target.get('season'), target.get('episode'), force), resolve)


    @web.middleware
    async def cors_and_timing(request, handler):
        started = time.monotonic()
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        elapsed = time.monotonic() - started
        if not request.path.startswith("/p/") or elapsed > 3:
            log.info("%s %s -> %s %.2fs", request.method, request.path, response.status, elapsed)
        return response

    @routes.get("/api/health")
    async def health(_request):
        return web.json_response({
            "ok": True, "service": "kinokrad-lampa", "version": APP_VERSION,
            "browser": bool(browser.browser and browser.browser.is_connected()),
            "player_pages": len(browser.sessions), "streams": len(streams.by_key),
            "segment_cache_mb": round(media.cache.size / 1048576, 1), "segments": media.stats,
        })

    @routes.get("/api/search")
    async def api_search(request):
        query = P.clean(request.query.get("q", ""))
        if not query or len(query) > 120:
            return web.json_response({"error": "query must contain 1-120 characters", "items": []}, status=400)
        try:
            items = await site.search(query)
            return web.json_response({"query": query, "items": items, "count": len(items)})
        except Exception as exc:
            log.exception("search failed")
            return web.json_response({"error": str(exc), "items": []}, status=502)

    def file_targets(detail):
        playback = detail.get("playback") or {}
        for item in playback.get("options") or []:
            yield item["file_id"], {"tid": item.get("translation_id"), "quality": item.get("quality") or "",
                                    "group": item.get("group") or ""}
        for season in playback.get("seasons") or []:
            for episode in season.get("episodes") or []:
                for item in episode.get("translations") or []:
                    yield item["file_id"], {"tid": item.get("translation_id"), "quality": item.get("quality") or "",
                                            "season": item.get("season"), "episode": item.get("episode")}

    def schedule_warm(detail):
        for file_id, target in file_targets(detail):
            streams.remember_target(detail["embed_url"], file_id, {k: v for k, v in target.items() if v not in (None, "")})
        asyncio.ensure_future(browser.warm(detail["embed_url"], detail["url"]))

    @routes.get("/api/detail")
    async def api_detail(request):
        url = request.query.get("url", "")
        if not P.allowed_page_url(url):
            return jerror("invalid KinoKrad URL", 400)
        try:
            detail = await site.detail(url)
        except Exception as exc:
            log.exception("detail failed")
            return jerror(exc)
        schedule_warm(detail)
        return web.json_response(detail)

    @routes.get("/api/prepare")
    async def api_prepare(request):
        """Поиск + лучшая карточка + прогрев плеера одним запросом."""
        title = P.clean(request.query.get("title", ""))[:120]
        original = P.clean(request.query.get("original_title", ""))[:120]
        year = (request.query.get("year") or "")[:4]
        media_type = request.query.get("type", "")
        if not title and not original:
            return jerror("title is required", 400)
        try:
            items = []
            for query in search_queries(title or original):
                more = await site.search(query)
                seen = {i["url"] for i in items}
                items += [i for i in more if i["url"] not in seen]
            best, confident = pick_best(items, title, original, year, media_type)
            if not confident and original and original.casefold() != title.casefold():
                more = await site.search(original)
                seen = {i["url"] for i in items}
                items = items + [i for i in more if i["url"] not in seen]
                best, confident = pick_best(items, title, original, year, media_type)
            if not confident:
                try:
                    more = await site.search_full(title or original)
                    seen = {i["url"] for i in items}
                    items = items + [i for i in more if i["url"] not in seen]
                    best, confident = pick_best(items, title, original, year, media_type)
                except Exception as exc:
                    log.info("full search failed: %s", exc)
            detail = None
            if best and confident:
                detail = await site.detail(best["url"])
                schedule_warm(detail)
            return web.json_response({"items": items, "best": best, "confident": confident, "detail": detail})
        except Exception as exc:
            log.exception("prepare failed")
            return jerror(exc)

    def stream_args(request):
        embed = request.query.get("embed_url", "")
        page_url = request.query.get("page_url", "")
        file_id = request.query.get("file_id", "")
        if not P.is_player_url(embed) or not P.allowed_page_url(page_url) or not file_id.isdigit():
            return None
        return embed, page_url, int(file_id), target_from(request.query)

    async def primary_payload(entry, base, prefer):
        payload = stream_payload(entry, base, prefer)
        # A captured request is not evidence that its session was accepted.
        if entry.get('session') and payload['audios']:
            index = payload['default_audio'] if payload['default_audio'] is not None else 0
            selected = payload['audios'][index]
            source = entry['data']['hlsSource'][selected['index']]
            height = selected['qualities'][0]
            await media.fetch_first(quality_alternatives(source['quality'], height), entry, (selected['index'], str(height)))
        return payload

    async def primary_resilient(entry, base, prefer):
        """Первичный поток; если маршрут сменился прямо во время попытки
        (DIRECT заблокирован, включился VPN-фолбэк), повторяем один раз —
        список «сломанных» узлов при смене выхода недействителен."""
        try:
            return await primary_payload(entry, base, prefer)
        except Exception:
            if not FALLBACK_PROXY:
                raise
            media.bad_hosts.clear()
            await asyncio.sleep(0.2)
            return await primary_payload(entry, base, prefer)

    @routes.get("/api/resolve")
    async def api_resolve(request):
        args = stream_args(request)
        if not args:
            return jerror("invalid stream parameters", 400)
        embed, page_url, file_id, target = args
        prefer = request.query.get("prefer", "")
        force = bool(request.query.get("refresh"))
        streams.remember_target(embed, file_id, target)
        if request.query.get('provider') == 'backup':
            try:
                entry = await resolve_backup(embed, page_url, file_id, target, force)
                payload = stream_payload(entry, base_url(request))
                payload.update(provider='backup', default_audio=None)
                return web.json_response(payload)
            except Exception as exc:
                log.warning('Video 2 resolve failed: %s', exc)
                return jerror(exc)
        if not force and not streams.get(embed, file_id):
            sibling = streams.sibling_with_audio(embed, target, prefer)
            if sibling:
                log.info("resolve file=%s reused audio set of file=%s", file_id, sibling["file_id"])
                try:
                    return web.json_response(await primary_resilient(sibling, base_url(request), prefer))
                except Exception:
                    pass  # маршрут сменился и убил старую сессию — нужна свежая генерация
        try:
            entry = await browser.resolve(embed, page_url, file_id, target, force=force)
            return web.json_response(await primary_resilient(entry, base_url(request), prefer))
        except Exception as exc:
            # Один свежий resolve: при смене DIRECT<->VPN Chromium перезапускается
            # и старую страницу/сессию нужно пересоздать, а не переиспользовать.
            try:
                entry = await browser.resolve(embed, page_url, file_id, target, force=True)
                return web.json_response(await primary_resilient(entry, base_url(request), prefer))
            except Exception as exc2:
                log.warning("resolve failed file=%s: %s / fresh retry: %s", file_id, exc, exc2)
                return jerror(exc)

    @routes.get("/api/prefetch")
    async def api_prefetch(request):
        args = stream_args(request)
        if not args:
            return jerror("invalid stream parameters", 400)
        embed, page_url, file_id, target = args
        streams.remember_target(embed, file_id, target)
        # Do not sign speculative voices/episodes: the provider can revoke a
        # playback session when another file is selected in its player.
        # Catalog/detail warming remains available; sign only on selection.
        return web.json_response({"ok": True}, status=202)

    @routes.get("/hls/{key}/{audio:\\d+}/{height:\\d+}.m3u8")
    async def hls(request):
        entry = streams.by(request.match_info["key"])
        if not entry:
            return web.Response(status=410, text="stream expired")
        if entry.get('replacement_key'):
            entry = streams.by(entry['replacement_key']) or entry
        sources = entry["data"].get("hlsSource") or []
        audio = int(request.match_info["audio"])
        if audio >= len(sources):
            return web.Response(status=404, text="audio not found")
        qualities = sources[audio].get("quality") or {}
        height = request.match_info["height"]
        # В VPN-маршруте полоса ограничена (~11–19 Мбит/с): 1080 не успевает
        # за буфером — зависания и fragLoadError. Отдаём ближайшее ≤720p.
        if not request.query.get("full") and MediaRoute.route in ("vpn", "probe") and int(height) > 720:
            height = "720"
            log.info("vpn route: 1080 -> 720 (bandwidth cap)")
        if entry.get('provider') == 'backup':
            master = entry['backup_master']
            if int(height) not in master.heights:
                return web.Response(status=404, text='quality not found')
            body = media.rewrite(master.playlist(audio, height), master.url, base_url(request), entry['key'])
            return web.Response(text=body, content_type='application/vnd.apple.mpegurl')
        alternatives = quality_alternatives(qualities, height)
        if not alternatives:
            return web.Response(status=404, text="quality not found")
        try:
            text, final_url = await media.fetch_first(alternatives, entry, (audio, height))
        except Exception as exc:
            # Все выданные узлы CDN отказали: новый подписанный запрос обычно
            # даёт другие узлы. Ключ потока тот же, поэтому плеер не замечает.
            if not entry.get("page_url") or time.monotonic() - entry.get("created", 0) < 2:
                return web.Response(status=502, text=str(exc))
            log.info("all CDN mirrors failed for file=%s (%s), re-resolving", entry["file_id"], exc)
            try:
                media.stats["refreshed"] += 1
                previous = entry
                entry = await browser.resolve(entry["embed"], entry["page_url"], entry["file_id"],
                                              entry.get("target") or {}, force=True)
                previous['replacement_key'] = entry['key']
                fresh = (entry["data"].get("hlsSource") or [])
                alternatives = quality_alternatives((fresh[audio].get("quality") or {}) if audio < len(fresh) else {}, height)
                text, final_url = await media.fetch_first(alternatives, entry, (audio, height))
            except Exception as exc2:
                return web.Response(status=502, text=str(exc2))
        # All playlists and media share the signing session and egress.
        body = media.rewrite(text, final_url, base_url(request), entry["key"])
        if "#EXTINF" in text:
            media.warm_start(entry)
        return web.Response(text=body, content_type="application/vnd.apple.mpegurl")

    @routes.get("/p/{key}")
    async def proxy(request):
        entry = streams.by(request.match_info["key"])
        if not entry:
            return web.Response(status=410, text="stream expired")
        try:
            url = b64d(request.query.get("u", ""))
        except Exception:
            return web.Response(status=400, text="bad url")
        if not P.allowed_media_url(url):
            return web.Response(status=403, text="media host not allowed")
        if urlparse(url).path.endswith(".m3u8"):
            try:
                text, final_url = await media.fetch_text(url, entry)
            except Exception as exc:
                return web.Response(status=502, text=str(exc))
            # v3.4.3: сегменты снова идут через мини — CDN привязывает
            # медиа к подписанной сессии мини (Chromium), прямые запросы
            # от ТВ отклоняются (fragLoadError). Возврат к проверенной
            # схеме v3.3.8: master->/hls, variant и сегменты -> /p/.
            body = media.rewrite(text, final_url, base_url(request), entry["key"])
            if "#EXTINF" in text:
                media.warm_start(entry)
            log.info("p m3u8 variant -> /p/ (segments via mini) %s", final_url[:90])
            return web.Response(text=body, content_type="application/vnd.apple.mpegurl")
        try:
            return await media.segment(request, url, entry)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.info("segment proxy error: %s", exc)
            return web.Response(status=502, text="segment error")

    @routes.get("/plugin.js")
    async def plugin(request):
        code = (ROOT / "plugin.js").read_text(encoding="utf-8").replace("__BASE_URL__", base_url(request))
        return web.Response(text=code, content_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    async def add_cors(_request, response):
        # Сигнал срабатывает до отправки заголовков — в т.ч. у потоковых
        # ответов, которым middleware заголовки добавить уже не успевает.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"

    app = web.Application(middlewares=[cors_and_timing])
    app.on_response_prepare.append(add_cors)
    app.add_routes(routes)
    app["site"], app["browser"], app["media"], app["streams"] = site, browser, media, streams

    async def on_startup(_app):
        await site.start()
        await media.start()

        async def warmup():
            try:
                await site.refresh_hash()
            except Exception as exc:
                log.warning("initial dle hash refresh failed: %s", exc)
            try:
                await browser.ensure()
            except Exception as exc:
                log.warning("chromium start failed: %s", exc)

        async def janitor():
            while True:
                await asyncio.sleep(60)
                try:
                    await browser.reap_idle()
                except Exception as exc:
                    log.info("janitor: %s", exc)

        _app["bg"] = [asyncio.ensure_future(warmup()), asyncio.ensure_future(janitor())]

    async def on_cleanup(_app):
        for task in _app.get("bg", []):
            task.cancel()
        await media.close()
        await site.close()
        await browser.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT, access_log=None)
