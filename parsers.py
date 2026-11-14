"""Чистые парсеры KinoKrad: без сети, без браузера, удобно тестировать."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

SITE = "https://kinokrad.my"
PLAYER_HOST = "assortedia-as.stravers.live"
PLAYER_SUFFIX = ".stravers.live"


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def full_url(value, base=SITE):
    return urljoin(base + "/", value or "")


def stable_id(value):
    return int(hashlib.md5((value or "kinokrad").encode()).hexdigest()[:8], 16)


def allowed_page_url(value):
    p = urlparse(value or "")
    return p.scheme == "https" and (p.hostname or "").lower() in {"kinokrad.my", "www.kinokrad.my"}


def allowed_media_url(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    return p.scheme in {"http", "https"} and (
        host.endswith(PLAYER_SUFFIX) or host.endswith(".vkvideo.cloud") or host.endswith(".userplay.live") or host.endswith(".interkh.com")
    )


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


def parse_search(html):
    """Разбирает штатную выдачу `/search/<query>/` KinoKrad."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    seen = set()
    # При пустом поиске сайт выводит «Горячие новинки» в error-container.
    for node in soup.select(".error-container"):
        node.decompose()
    for card in soup.select(".kino-card"):
        anchor = card.select_one("a.kino-poster[href]")
        if not anchor:
            continue
        url = full_url(anchor.get("href"))
        if not allowed_page_url(url) or url in seen:
            continue
        seen.add(url)
        title_node = card.select_one(".kino-card-title")
        raw_title = clean(title_node.get_text(" ", strip=True) if title_node else anchor.get("title", ""))
        year_match = re.search(r"\((\d{4})\)", raw_title)
        year_node = card.select_one(".card-title-year, .kino-card-year")
        year = clean((year_node.get("title") or year_node.get_text(" ", strip=True)) if year_node else "").strip("()")
        year = year or (year_match.group(1) if year_match else "")
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", raw_title)
        image = anchor.find("img")
        poster = full_url((image.get("data-src") or image.get("src")) if image else "")
        description = card.select_one('[itemprop="description"]')
        imdb = card.select_one(".kino-poster-imdb-rting")
        kp = card.select_one(".kino-poster-kp-rting")
        item_type = (card.get("itemtype") or "").lower()
        result.append({
            "id": stable_id(url), "title": title, "year": year,
            "poster": poster, "url": url,
            "description": clean(description.get_text(" ", strip=True)) if description else "",
            "imdb": clean((imdb or {}).get("data-title", "")).replace("IMDb:", "").strip(),
            "kinopoisk": clean((kp or {}).get("data-title", "")).replace("KP:", "").strip(),
            "media_type": "series" if "tvseries" in item_type or re.search(r"\bсезон\b", title, re.I) else "movie",
        })
    # Некоторые варианты шаблона не содержат внешний `.kino-card`.
    return result


def parse_search_ajax(payload):
    """Разбирает быструю autocomplete-выдачу KinoKrad."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, dict) or str(data.get("error", "")).lower() == "true":
        raise ValueError((data or {}).get("text") or "KinoKrad AJAX search failed")
    soup = BeautifulSoup(data.get("content") or "", "html.parser")
    result = []
    for anchor in soup.select("a.search-card[href]"):
        url = full_url(anchor.get("href"))
        if not allowed_page_url(url):
            continue
        title_node = anchor.select_one(".searchheading")
        image = anchor.find("img")
        country_node = anchor.select_one(".search-card-country")
        country = clean(country_node.get_text(" ", strip=True) if country_node else "")
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", country)
        category_node = anchor.select_one(".search-card-categorys")
        category = clean(category_node.get_text(" ", strip=True) if category_node else "")
        imdb = anchor.select_one(".search-card-imdb-rting")
        kp = anchor.select_one(".search-card-kp-rting")
        result.append({
            "id": stable_id(url),
            "title": clean(title_node.get_text(" ", strip=True) if title_node else anchor.get("title", "")),
            "year": year_match.group(1) if year_match else "",
            "poster": full_url(image.get("src") if image else ""),
            "url": url,
            "description": category,
            "imdb": clean(imdb.get_text(" ", strip=True) if imdb else "").replace("IMDb:", "").strip(),
            "kinopoisk": clean(kp.get_text(" ", strip=True) if kp else "").replace("KP:", "").strip(),
            "media_type": "series" if re.search(r"\b(?:Сериал|Мультсериал)\b", category, re.I) else "movie",
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


def extract_embed_url(html):
    soup = BeautifulSoup(html, "html.parser")
    ld = parse_json_script(soup)
    iframe = soup.select_one('iframe[src*="stravers.live"]')
    button = soup.select_one('[data-url*="stravers.live"]')
    embed = (
        (iframe.get("src") if iframe else "")
        or (button.get("data-url") if button else "")
        or (ld.get("video") or {}).get("embedUrl", "")
        or ld.get("embedUrl", "")
    )
    if embed and embed.startswith("//"):
        embed = "https:" + embed
    return full_url(embed, SITE)


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
                        "season": int(re.sub(r"\D", "", str(season_key)) or 0),
                        "episode": int(re.sub(r"\D", "", str(episode_key)) or 0),
                    })
            episodes.append({"episode": int(re.sub(r"\D", "", str(episode_key)) or 0), "translations": translations})
        seasons.append({"season": int(re.sub(r"\D", "", str(season_key)) or 0), "episodes": episodes})
    return seasons


def parse_detail(html, url, player_html=None):
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
    if not embed or not is_player_url(embed):
        raise ValueError("trusted player iframe not found")
    if not player_html:
        raise ValueError("player HTML is required")
    files = parse_player_json(player_html, "fileList")
    result["media_type"] = "series" if files.get("type") == "serial" else "movie"
    result["playback"] = {
        "type": result["media_type"],
        "options": flatten_movie(files) if result["media_type"] == "movie" else [],
        "seasons": flatten_series(files) if result["media_type"] == "series" else [],
    }
    return result



def is_player_url(value):
    p = urlparse(value or "")
    return p.scheme == "https" and (p.hostname or "").lower().endswith(PLAYER_SUFFIX)


DLE_HASH_RE = re.compile(r"dle_login_hash\s*=\s*['\"]([a-f0-9]{32,64})", re.I)


def extract_dle_hash(html):
    m = DLE_HASH_RE.search(html or "")
    return m.group(1) if m else ""
