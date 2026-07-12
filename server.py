import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kinokrad")

app = Flask(__name__)
CORS(app)
APP_VERSION = "1.0.4"
SITE = "https://kinokrad.my"
PLAYER_HOST = "assortedia-as.stravers.live"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
STREAM_CACHE = {}
HTML_CACHE = {}
CACHE_LOCK = threading.Lock()
BROWSER_LOCK = threading.Lock()
STREAM_TTL = int(os.getenv("STREAM_TTL", "5400"))


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def full_url(value, base=SITE):
    return urljoin(base + "/", value or "")


def stable_id(value):
    return int(hashlib.md5((value or "kinokrad").encode()).hexdigest()[:8], 16)


def allowed_page_url(value):
    p = urlparse(value or "")
    return p.scheme == "https" and p.hostname in {"kinokrad.my", "www.kinokrad.my"}


def allowed_media_url(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    return p.scheme in {"http", "https"} and (
        host == PLAYER_HOST or host.endswith(".vkvideo.cloud")
    )


def proxy_url(value):
    return request.host_url.rstrip("/") + "/api/proxy?url=" + quote(value, safe="")


def fetch_html_browser(url, timeout=60):
    from playwright.sync_api import sync_playwright

    with BROWSER_LOCK, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=UA, locale="ru-RU")
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if not response or response.status >= 400:
            status = response.status if response else "no response"
            browser.close()
            raise RuntimeError("browser fetch failed: %s" % status)
        html = page.content()
        browser.close()
        return html


def fetch_html(url, timeout=25):
    now = time.time()
    with CACHE_LOCK:
        cached = HTML_CACHE.get(url)
        if cached and cached["expires"] > now:
            return cached["html"]
    try:
        response = SESSION.get(url, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", None)
        log.info("HTTP fetch failed for %s (%s), using Chromium fallback", urlparse(url).hostname, status or type(exc).__name__)
        html = fetch_html_browser(url, max(timeout, 60))
    with CACHE_LOCK:
        HTML_CACHE[url] = {"expires": now + 300, "html": html}
    return html


def fetch_player_html(page_url, expected_embed, timeout=60):
    """Получает fileList только через реальный сценарий карточка → Смотреть."""
    from playwright.sync_api import sync_playwright

    with BROWSER_LOCK, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=UA, locale="ru-RU")
        response = page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if not response or response.status >= 400:
            browser.close()
            raise RuntimeError("KinoKrad card is unavailable in browser")
        page.get_by_role("button", name="Смотреть").click(timeout=30000)
        deadline = time.time() + 20
        player_frame = None
        while time.time() < deadline and not player_frame:
            player_frame = next((frame for frame in page.frames if urlparse(frame.url).hostname == PLAYER_HOST), None)
            if not player_frame:
                page.wait_for_timeout(200)
        if not player_frame or urlparse(player_frame.url).hostname != urlparse(expected_embed).hostname:
            browser.close()
            raise RuntimeError("trusted KinoKrad player frame not found")
        page.wait_for_timeout(500)
        html = player_frame.content()
        browser.close()
        return html


def catalog_url(kind, page):
    base = "/f/s.trailers=0/x.type=%s/sort=7days/order=desc/" % kind
    return SITE + (base if page == 1 else base + "page/%d/" % page)


def parse_catalog(html):
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for anchor in soup.select("a.kino-poster[href]"):
        url = full_url(anchor.get("href"))
        if not allowed_page_url(url):
            continue
        title_text = clean(anchor.get_text(" ", strip=True))
        year_match = re.search(r"\((\d{4})\)\s*$", title_text)
        year = year_match.group(1) if year_match else ""
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", title_text)
        image = anchor.find("img")
        poster = full_url((image.get("data-src") or image.get("src")) if image else "")
        result.append({
            "id": stable_id(url), "title": title, "year": year,
            "poster": poster, "url": url,
        })
    return result


def parse_json_script(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
            if isinstance(data, dict) and data.get("name"):
                return data
        except (ValueError, TypeError):
            pass
    return {}


def text_after_label(soup, label):
    pattern = re.compile(r"^\s*" + re.escape(label), re.I)
    node = soup.find(string=pattern)
    if not node:
        return ""
    parent = node.parent
    text = clean(parent.get_text(" ", strip=True))
    return clean(pattern.sub("", text).lstrip(": "))


def info_value(soup, label):
    """Читает пару `.info-element-name` / `.info-element-content`."""
    wanted = label.lower().rstrip(":")
    for name in soup.select(".info-element-name"):
        if clean(name.get_text(" ", strip=True)).lower().rstrip(":") == wanted:
            content = name.parent.select_one(".info-element-content")
            return clean(content.get_text(" ", strip=True)) if content else ""
    return ""


def parse_player_json(html, variable):
    match = re.search(
        r"const\s+" + re.escape(variable) + r"\s*=\s*JSON\.parse\('((?:\\.|[^'])*)'\)",
        html, re.S,
    )
    if not match:
        raise ValueError("player JSON %s not found" % variable)
    raw = match.group(1).replace("\\'", "'")
    return json.loads(raw)


def flatten_movie(file_list):
    options = []
    for group_name, group in (file_list.get("all") or {}).items():
        if not isinstance(group, dict):
            continue
        for trans_key, qualities in group.items():
            if not isinstance(qualities, dict):
                continue
            for quality, item in qualities.items():
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                options.append({
                    "file_id": item["id"], "translation_id": item.get("id_translation"),
                    "label": item.get("translation") or trans_key,
                    "quality": item.get("quality") or quality,
                    "uhd": bool(item.get("uhd")), "group": group_name,
                })
    return options


def flatten_series(file_list):
    seasons = []
    all_seasons = file_list.get("all") or {}
    for season_key in sorted(all_seasons, key=lambda x: int(re.sub(r"\D", "", str(x)) or 0)):
        episodes = []
        raw_episodes = all_seasons[season_key]
        for episode_key in sorted(raw_episodes, key=lambda x: int(re.sub(r"\D", "", str(x)) or 0)):
            translations = []
            for _, item in (raw_episodes[episode_key] or {}).items():
                if isinstance(item, dict) and item.get("id"):
                    translations.append({
                        "file_id": item["id"], "translation_id": item.get("id_translation"),
                        "label": item.get("translation") or "Озвучка",
                        "quality": item.get("quality") or "",
                    })
            episodes.append({"episode": int(re.sub(r"\D", "", str(episode_key)) or 0), "translations": translations})
        seasons.append({"season": int(re.sub(r"\D", "", str(season_key)) or 0), "episodes": episodes})
    return seasons


def parse_detail(html, url):
    soup = BeautifulSoup(html, "html.parser")
    ld = parse_json_script(soup)
    title = clean(ld.get("name") or (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""))
    image = ld.get("image") or (ld.get("video") or {}).get("thumbnailUrl") or ""
    if isinstance(image, dict):
        image = image.get("url", "")
    rating = ld.get("aggregateRating") or {}
    iframe = soup.select_one('iframe[src*="stravers.live"]')
    embed = full_url(iframe.get("src"), SITE) if iframe else (ld.get("video") or {}).get("embedUrl", "") or ld.get("embedUrl", "")
    if embed and embed.startswith("//"):
        embed = "https:" + embed
    raw_genres = ld.get("genre") or info_value(soup, "Жанр")
    genres = raw_genres if isinstance(raw_genres, list) else [clean(x) for x in str(raw_genres).split(",") if clean(x)]
    genres = [x for x in genres if not re.search(r"^(Фильмы\s+\d{4}|Новинки кино|Сейчас в кино)$", x, re.I)]
    raw_actors = [x.get("name", "") for x in (ld.get("actor") or []) if isinstance(x, dict)]
    raw_directors = [x.get("name", "") for x in (ld.get("director") or []) if isinstance(x, dict)]
    if not raw_actors and info_value(soup, "Актёры"):
        raw_actors = [clean(x) for x in info_value(soup, "Актёры").split(",") if clean(x)]
    if not raw_directors and info_value(soup, "Режиссёр"):
        raw_directors = [clean(x) for x in info_value(soup, "Режиссёр").split(",") if clean(x)]
    country_ld = ld.get("countryOfOrigin") or ""
    if isinstance(country_ld, list):
        country_ld = ", ".join(x.get("name", "") if isinstance(x, dict) else str(x) for x in country_ld)
    elif isinstance(country_ld, dict):
        country_ld = country_ld.get("name", "")
    result = {
        "id": stable_id(url), "url": url, "title": title,
        "original_title": clean(ld.get("alternateName") or ""),
        "description": clean(ld.get("description") or ""),
        "poster": full_url(image), "genres": genres,
        "country": info_value(soup, "Страна") or clean(str(country_ld)),
        "year": info_value(soup, "Год") or str(ld.get("dateCreated") or ld.get("datePublished") or "")[:4],
        "rating": rating.get("ratingValue", ""), "rating_count": rating.get("ratingCount", ""),
        "actors": raw_actors, "directors": raw_directors,
        "duration": clean(ld.get("duration") or info_value(soup, "Время") or text_after_label(soup, "Продолжительность")),
        "quality": info_value(soup, "Качество"), "age": clean(ld.get("contentRating") or info_value(soup, "Возраст")),
        "embed_url": embed,
    }
    page_text = soup.get_text(" ", strip=True)
    for key, label in (("kinopoisk", "(?:Кинопоиск|КП)"), ("imdb", "IMDb")):
        m = re.search(label + r"\s*[: ]\s*([0-9]+(?:[.,][0-9]+)?)", page_text, re.I)
        result[key] = m.group(1).replace(",", ".") if m else ""
    if not embed or urlparse(embed).hostname != PLAYER_HOST:
        raise ValueError("trusted player iframe not found")
    player_html = fetch_player_html(url, embed)
    files = parse_player_json(player_html, "fileList")
    result["media_type"] = "series" if files.get("type") == "serial" else "movie"
    result["playback"] = {
        "type": result["media_type"],
        "options": flatten_movie(files) if result["media_type"] == "movie" else [],
        "seasons": flatten_series(files) if result["media_type"] == "series" else [],
    }
    return result


def resolve_with_browser(embed_url, file_id, page_url=""):
    """Даёт guard-слою Chromium подписать запрос для выбранного файла."""
    from playwright.sync_api import sync_playwright

    captured = {}
    response_objects = {}
    with BROWSER_LOCK, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=UA, locale="ru-RU")
        page = context.new_page()
        # Подменяем только active внутри JSON fileList до старта player.js. Так
        # защитный слой сам подписывает первый запрос именно для выбранной серии/
        # озвучки, без хрупких кликов по DOM плеера.
        page.add_init_script("""
          (() => {
            const wanted = %d;
            const original = JSON.parse;
            JSON.parse = function(text, reviver) {
              const value = original.call(JSON, text, reviver);
              if (value && value.active && value.all && Number(value.active.id) !== wanted) {
                let found = null;
                const walk = node => {
                  if (!node || found) return;
                  if (typeof node === 'object' && Number(node.id) === wanted) { found = node; return; }
                  if (typeof node === 'object') Object.values(node).forEach(walk);
                };
                walk(value.all);
                if (found) value.active = found;
              }
              return value;
            };
          })();
        """ % int(file_id))

        def on_request(req):
            if "/bnsi/movies/" in req.url:
                captured.update({"url": req.url, "headers": req.headers, "post": req.post_data or ""})

        def on_response(resp):
            if "/bnsi/movies/" in resp.url and resp.ok:
                try:
                    response_id = int(resp.url.rstrip("/").split("/")[-1])
                    response_objects[response_id] = resp
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)
        if page_url and allowed_page_url(page_url):
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.get_by_role("button", name="Смотреть").click(timeout=30000)
            except Exception:
                page.goto(embed_url, referer=page_url, wait_until="domcontentloaded", timeout=60000)
        else:
            page.goto(embed_url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + 25
        while time.time() < deadline and not (captured and response_objects):
            page.wait_for_timeout(250)
        if not captured:
            browser.close()
            raise RuntimeError("KinoKrad guard did not issue stream request")

        initial_id = int(captured["url"].rstrip("/").split("/")[-1])
        if initial_id != int(file_id) or initial_id not in response_objects:
            browser.close()
            raise RuntimeError("KinoKrad player did not select requested file")
        result = response_objects[initial_id].json()
        browser.close()
    if not result.get("hlsSource"):
        raise RuntimeError("KinoKrad returned no HLS sources")
    return result


def cache_stream(embed_url, file_id, page_url="", force=False):
    cache_key = hashlib.sha256((embed_url + "|" + str(file_id)).encode()).hexdigest()[:24]
    with CACHE_LOCK:
        item = STREAM_CACHE.get(cache_key)
        if not force and item and item["expires"] > time.time():
            return cache_key, item["data"]
    data = resolve_with_browser(embed_url, file_id, page_url)
    with CACHE_LOCK:
        STREAM_CACHE[cache_key] = {"expires": time.time() + STREAM_TTL, "data": data}
    return cache_key, data


@app.get("/api/catalog")
def api_catalog():
    kind = request.args.get("type", "movie")
    if kind not in {"movie", "series"}:
        return jsonify({"error": "invalid type"}), 400
    page = max(1, min(int(request.args.get("page", 1)), 20))
    items = parse_catalog(fetch_html(catalog_url(kind, page)))
    return jsonify({"type": kind, "page": page, "items": items, "has_more": bool(items)})


@app.get("/api/detail")
def api_detail():
    url = request.args.get("url", "")
    if not allowed_page_url(url):
        return jsonify({"error": "invalid KinoKrad URL"}), 400
    try:
        return jsonify(parse_detail(fetch_html(url), url))
    except Exception as exc:
        log.exception("detail failed")
        return jsonify({"error": str(exc)}), 502


@app.get("/api/resolve")
def api_resolve():
    embed = request.args.get("embed_url", "")
    page_url = request.args.get("page_url", "")
    file_id = request.args.get("file_id", "")
    if urlparse(embed).hostname != PLAYER_HOST or not file_id.isdigit():
        return jsonify({"error": "invalid stream parameters"}), 400
    try:
        key, data = cache_stream(embed, int(file_id), page_url, force=bool(request.args.get("refresh")))
        audios = []
        for index, audio in enumerate(data.get("hlsSource") or []):
            qualities = audio.get("quality") or {}
            audios.append({
                "label": audio.get("label") or "Аудиодорожка %d" % (index + 1),
                "audio_id": audio.get("audioId"),
                "url": request.host_url.rstrip("/") + "/api/master/%s/%d.m3u8?v=%d" % (key, index, int(time.time())),
                "qualities": sorted([int(x) for x in qualities if str(x).isdigit()], reverse=True),
            })
        tracks = []
        for track in data.get("tracks") or []:
            if allowed_media_url(track.get("src", "")):
                tracks.append({**track, "src": proxy_url(track["src"])})
        return jsonify({"key": key, "audios": audios, "tracks": tracks})
    except Exception as exc:
        log.exception("stream resolve failed")
        return jsonify({"error": str(exc)}), 502


@app.get("/api/master/<key>/<int:audio_index>.m3u8")
def api_master(key, audio_index):
    with CACHE_LOCK:
        cached = STREAM_CACHE.get(key)
    if not cached or cached["expires"] <= time.time():
        return Response("stream expired", status=410)
    sources = cached["data"].get("hlsSource") or []
    if audio_index >= len(sources):
        return Response("audio not found", status=404)
    quality = sources[audio_index].get("quality") or {}
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for height in sorted((int(x) for x in quality if str(x).isdigit()), reverse=True):
        raw = quality.get(str(height), quality.get(height, ""))
        url = clean(str(raw).split(" or ")[0])
        if not allowed_media_url(url):
            continue
        bandwidth = max(300000, height * height * 3)
        lines += ["#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d" % (bandwidth, int(height * 16 / 9), height), proxy_url(url)]
    return Response("\n".join(lines) + "\n", mimetype="application/vnd.apple.mpegurl")


def rewrite_manifest(text, source):
    def wrap(value):
        absolute = urljoin(source, value.strip())
        return proxy_url(absolute) if allowed_media_url(absolute) else absolute

    output = []
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            raw = re.sub(r'URI=("|\')([^"\']+)(\1)', lambda m: "URI=" + m.group(1) + wrap(m.group(2)) + m.group(3), raw)
            output.append(raw)
        elif raw.strip():
            output.append(wrap(raw))
        else:
            output.append(raw)
    return "\n".join(output) + "\n"


@app.get("/api/proxy")
def api_proxy():
    url = request.args.get("url", "")
    if not allowed_media_url(url):
        return jsonify({"error": "media host not allowed"}), 403
    headers = {"User-Agent": UA, "Referer": "https://%s/" % PLAYER_HOST}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=(10, 40), allow_redirects=True)
        if not allowed_media_url(upstream.url):
            upstream.close()
            return jsonify({"error": "redirect host not allowed"}), 403
        content_type = upstream.headers.get("Content-Type", "application/octet-stream")
        if "mpegurl" in content_type or urlparse(upstream.url).path.endswith(".m3u8"):
            body = rewrite_manifest(upstream.content.decode("utf-8", "replace"), upstream.url)
            upstream.close()
            return Response(body, status=upstream.status_code, mimetype="application/vnd.apple.mpegurl")
        passthrough = {k: v for k, v in upstream.headers.items() if k.lower() in {"content-type", "content-length", "content-range", "accept-ranges"}}
        return Response(stream_with_context(upstream.iter_content(256 * 1024)), status=upstream.status_code, headers=passthrough)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "kinokrad-lampa", "version": APP_VERSION})


@app.get("/plugin.js")
def plugin():
    path = os.path.join(os.path.dirname(__file__), "plugin.js")
    body = open(path, encoding="utf-8").read().replace("__BASE_URL__", request.host_url.rstrip("/"))
    return Response(body, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5200")), threaded=True)
