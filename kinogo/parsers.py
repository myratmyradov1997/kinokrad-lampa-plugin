"""Чистые парсеры HDKinogo (kinogo.net / kinogo.club зеркала)."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

SITE = "https://hdkinogo.my"
PLAYER_SUFFICES = (".stravers.live", ".stloadi.live", ".stloadi.co", ".stloadi.net")

KINOGO_CARD_RE = re.compile(
    r'(?s)<div class="short clearfix with-mask">.*?'
    r'<img src="(?P<poster>[^"]+)"\s+alt="(?P<alt>[^"]*)"'
    r".*?"
    r'<a class="short-title" href="(?P<url>https?://[^"]+)">(?P<title>[^<]+)</a>'
)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def full_url(value, base=SITE):
    return urljoin(base + "/", value or "")


def stable_id(value):
    return int(hashlib.md5((value or "kinogo").encode()).hexdigest()[:8], 16)


def allowed_page_url(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    return p.scheme == "https" and host in {
        "hdkinogo.my", "kinogo.bet", "1.kinogoline.net", "kinogoline.net",
    }


def allowed_media_url(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    return p.scheme in {"http", "https"} and (
        host.endswith(PLAYER_SUFFICES)
        or host.endswith(".vkvideo.cloud")
        or host.endswith(".userplay.live")
        or host.endswith(".interkh.com")
    )


def catalog_url(kind, page=1):
    """kind: serial | multfilm | anime | boevik | drama | criminal ..."""
    allowed = {"serial", "multfilm", "anime", "boevik", "drama", "criminal",
               "detektiv", "comedy", "fantastik", "fentezi", "istoria", "sport", "melodrama"}
    if kind not in allowed:
        kind = "serial"
    base = SITE + "/" + kind + "/"
    return base if page == 1 else base + "page/%d/" % page


def parse_catalog(html):
    """Разбирает карточки каталога HDKinogo."""
    result = []
    seen = set()
    for match in KINOGO_CARD_RE.finditer(html):
        poster, alt, url, title = match.group("poster"), match.group("alt"), match.group("url"), match.group("title")
        if not allowed_page_url(url) or url in seen:
            continue
        seen.add(url)
        raw_title = clean(title or alt)
        year_match = re.search(r"\((\d{4}(?:-\d{4})?)\)\s*$", raw_title)
        year = year_match.group(1)[:4] if year_match else ""
        title = re.sub(r"\s*\(\d{4}(?:-\d{4})?\)\s*$", "", raw_title)
        result.append({
            "id": stable_id(url), "title": title, "year": year,
            "poster": full_url(poster), "url": url,
            "media_type": "movie",
        })
    return result


def parse_search(html):
    """Поиск hdkinogo возвращает те же карточки, что и каталог."""
    return parse_catalog(html)




FASTSEARCH_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{2,120})</a>')


def parse_search_ajax(html):
    """Разбор результа поиска, HTML фрагмент с сайта."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tag in soup.select("a[href]"):
        href = tag.get("href", "")
        title = clean(tag.get_text(" ", strip=True))
        if not href or not title:
            continue
        url = full_url(href, SITE)
        if not allowed_page_url(url):
            continue
        year_m = re.search(r"\(([0-9]{4}(?:-[0-9]{4})?)\)", title)
        items.append({
            "id": stable_id(url),
            "title": clean(re.sub(r"\s*\([0-9]{4}(?:-[0-9]{4})?\)\s*$", "", title)),
            "year": year_m.group(1)[:4] if year_m else "",
            "poster": "", "url": url,
        })
    return items


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
    for iframe in soup.select("iframe[src]"):
        src = (iframe.get("src") or "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        host = (urlparse(src).hostname or "").lower()
        if host.endswith(PLAYER_SUFFICES):
            return full_url(src, SITE)
    return ""


def text_after_label(soup, label):
    pattern = re.compile(r"^\s*" + re.escape(label), re.I)
    node = soup.find(string=pattern)
    if not node:
        return ""
    parent = node.parent
    text = clean(parent.get_text(" ", strip=True))
    return clean(pattern.sub("", text).lstrip(": "))


def info_value(soup, label):
    wanted = label.lower().rstrip(":")
    for name in soup.select(".finfo .sd-line, .info-element-name"):
        text = clean(name.get_text(" ", strip=True))
        if text.lower().startswith(wanted):
            return clean(text[len(wanted):].lstrip(": "))
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
    h1 = soup.find("h1")
    title = clean(ld.get("name") or (h1.get_text(" ", strip=True) if h1 else ""))
    image = ld.get("image") or (ld.get("video") or {}).get("thumbnailUrl") or ""
    if isinstance(image, dict):
        image = image.get("url", "")
    if not image:
        img = soup.select_one(".short-img img, .movie-img img, .fimg img")
        if img:
            image = img.get("data-src") or img.get("src") or ""
    iframe_src = extract_embed_url(html)
    embed = full_url(iframe_src or (ld.get("video") or {}).get("embedUrl", "") or (ld.get("embedUrl", "") if ld else ""), SITE)
    description_node = soup.select_one(".short-desc, .film-description, .fdescription")
    raw_genres = ld.get("genre") or [a.get_text(" ", strip=True) for a in soup.select(".finfo a[href*=boevik], .finfo a[href*=drama]")]
    genres = raw_genres if isinstance(raw_genres, list) else [clean(x) for x in str(raw_genres).split(",") if clean(x)]
    year_m = re.search(r"\((\d{4}(?:-\d{4})?)\)", title)
    result = {
        "id": stable_id(url), "url": url, "title": title,
        "original_title": clean(ld.get("alternateName") or ""),
        "description": clean(description_node.get_text(" ", strip=True)) if description_node else clean(ld.get("description") or ""),
        "poster": full_url(image), "genres": [g for g in genres if g],
        "country": info_value(soup, "Страна"),
        "year": year_m.group(1)[:4] if year_m else "",
        "actors": [], "directors": [],
        "duration": info_value(soup, "Время") or text_after_label(soup, "Продолжительность"),
        "quality": info_value(soup, "Качество"),
        "age": clean(ld.get("contentRating") or info_value(soup, "Возраст")),
        "embed_url": embed,
    }
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
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host.endswith(s) for s in PLAYER_SUFFICES)


DLE_HASH_RE = re.compile(r"dle_login_hash\s*=\s*['\"]([a-f0-9]{32,64})", re.I)


def extract_dle_hash(html):
    m = DLE_HASH_RE.search(html or "")
    return m.group(1) if m else ""
