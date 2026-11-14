# KinoKrad и Kinogo для Lampa

Два источника для Lampa в одном репозитории:

| Папка | Источник | Порт по умолчанию | Что даёт |
|---|---|---|---|
| `/` (корень) | **KinoKrad** | 5200 | каталог, карточки, сериалы, озвучки, выбор качества, второй плеер |
| `kinogo/` | **Kinogo** | 5210 | каталог и карточки Kinogo, точный выбор серии/озвучки |

Оба сервиса — обычные Python-приложения (aiohttp). Их можно поставить на домашний компьютер
(Mac mini, мини-ПК, NAS) или на VPS: телевизору нужен только адрес сервиса в локальной сети.

Рядом живут ещё два проекта того же семейства:

- [dorama-lampa-plugin](https://github.com/myratmyradov1997/dorama-lampa-plugin) — подборки дорам;
- [rutube-lampa-plugin](https://github.com/myratmyradov1997/rutube-lampa-plugin) — RuTube и подробный
  разбор того, как вообще запускается фильм в Lampa
  ([docs](https://github.com/myratmyradov1997/rutube-lampa-plugin/blob/main/docs/HOW_LAMPA_PLAYS.md)).

## Как это работает

```
Телевизор (Lampa) ──▶ plugin.js ──▶ ваш сервис ──▶ сайт источника ──▶ CDN
        ▲                                  │
        └──────── HLS через сервис ────────┘
```

Коротко:

- **поиск и карточки** забираются обычными HTTP-запросами (`curl_cffi` с TLS-отпечатком Chrome),
  без запуска браузера — быстро и без лишней нагрузки;
- **подпись плеера** источника требует настоящего Chromium: сервис держит один постоянный
  экземпляр, прогревает страницу плеера и переключает серии/озвучки меню самого плеера;
- **видео** идёт через сервис: плейлисты переписываются, сегменты отдаются с поддержкой `Range`,
  поэтому перемотка работает, а телевизору не нужны cookie/Referer источника;
- **рекламные вставки Lampa** (прероллы, баннеры, переход к следующей серии) отключаются плагином
  на стороне телевизора; вшитая в сам файл реклама этим не убирается.

Подробности и типичные ошибки — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Сравнение источников и их ограничения — [`docs/SOURCES.md`](docs/SOURCES.md).

## Требования

- Python 3.10+ (проверено на 3.12);
- Chromium/Google Chrome для подписи потока KinoKrad и Playwright Chromium для Kinogo:

  ```bash
  python -m playwright install chromium-headless-shell
  ```

- хост, доступный телевизору по сети; открытые порты 5200 и 5210.

## Быстрый старт

```bash
git clone https://github.com/myratmyradov1997/kinokrad-lampa-plugin.git
cd kinokrad-lampa-plugin

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# KinoKrad
PORT=5200 python server.py

# Kinogo — во втором терминале, можно из того же venv
cd kinogo && PORT=5210 python server.py
```

Проверка:

```bash
curl http://127.0.0.1:5200/api/health     # {"ok": true, "service": "kinokrad-lampa", ...}
curl http://127.0.0.1:5210/api/health     # {"ok": true, "service": "kinogo-film", ...}
curl 'http://127.0.0.1:5200/api/search?q=аватар'
```

## Подключение в Lampa

1. **Настройки → Расширения → Добавить плагин**.
2. Вписать адрес плагина KinoKrad: `http://<адрес-сервиса>:5200/plugin.js`.
3. Вписать адрес плагина Kinogo: `http://<адрес-сервиса>:5210/plugin.js`.
4. Перезапустить Lampa.

После этого в карточке фильма появляется меню **Смотреть → Источники → KinoKrad / Kinogo / Rutube**
(меню создаёт плагин Kinogo, а пункт Rutube работает, только если подключён его плагин).
В поиске Lampa каждый источник показывается отдельным блоком.

Адрес сервиса в плагинах не зашит: сервер подставляет свой `origin` в момент отдачи `/plugin.js`,
поэтому одинаковый файл работает и на `localhost`, и за обратным прокси.

## Настройки KinoKrad

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `PORT` | `5200` | Порт сервиса |
| `HOST` | `0.0.0.0` | Адрес прослушивания |
| `KK_UPSTREAM_PROXY` | пусто | HTTP-прокси для Chromium и всех запросов к CDN (единый выход, если источник капризен к IP) |
| `KK_FALLBACK_PROXY` | пусто | Второй выход, на который сервис переключается при блокировке |
| `KK_FORCE_ROUTE` | пусто | `vpn` или `direct` — принудительный маршрут для диагностики |
| `KK_BROWSER_CHANNEL` | `chrome` | Канал браузера для Playwright |
| `KK_IMPERSONATE` | `chrome` | TLS-отпечаток для `curl_cffi` |
| `KK_MAX_HEIGHT` | `1080` | Потолок качества |
| `KK_ALLOW_AV1` | `0` | Разрешить AV1 (нужен аппаратный декодер на ТВ) |
| `PREFETCH_AHEAD` | `6` | Сколько следующих сегментов подгружать заранее |
| `SEGMENT_PARALLEL` | `2` | Сколько сегментов тянуть параллельно |
| `SEGMENT_CACHE_MB` | `768` | Размер кэша сегментов в памяти |

## Настройки Kinogo

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `PORT` | `5210` | Порт сервиса |
| `KG_BROWSER_PROXY` | пусто | Прокси для браузера и медиазапросов Kinogo |

## API

KinoKrad:

| Метод | Что делает |
|---|---|
| `GET /api/health` | Версия и состояние |
| `GET /api/search?q=` | Поиск по каталогу |
| `GET /api/detail?url=` | Карточка: сезоны, серии, озвучки, второй плеер |
| `GET /api/resolve?…` | Подпись и подготовка потока (сезон/серия/озвучка) |
| `GET /api/prefetch?…` | То же в фоне (202) — прогрев при наведении |
| `GET /hls/<key>/<audio>/<height>.m3u8`, `GET /p/<key>?u=…` | HLS через сервис |
| `GET /plugin.js` | Плагин для Lampa |

Kinogo: `GET /api/health`, `/api/search?q=`, `/api/prepare`, `/api/detail?url=`, `/api/resolve?…`,
`/media?u=…` (HLS-прокси), `/plugin.js`.

## Постоянный запуск

Linux (systemd) — готовые unit-файлы в [`deploy/`](deploy):

```bash
sudo cp deploy/kinokrad-lampa.service deploy/kinogo-lampa.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kinokrad-lampa kinogo-lampa
```

macOS (launchd) — шаблоны в [`deploy/macos/`](deploy/macos): замените `com.example` на свой префикс,
проверьте пути и загрузите:

```bash
cp deploy/macos/com.example.kinokrad-lampa.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.kinokrad-lampa.plist
```

Служба поднимается после входа пользователя — автовход в macOS этим проектом не настраивается.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q                   # Python: парсеры, резолвер, реклама, восстановление
node tests/test_plugins.cjs # JS: кнопка в карточке и меню источников
```

## Ограничения

- источник может ответить 403/WS4005 при недоверенной сессии или на «плохом» выходном IP — сервис
  умеет переключаться на резервный маршрут, но гарантий от политики площадки нет;
- качество ограничено тем, что отдаёт источник: у части тайтлов максимум 720p;
- `bufferAppendError` и конфликты декодера на телевизоре — почти всегда железо ТВ, помогает полный
  перезапуск приложения Lampa/MSX;
- вшитая в видео реклама не вырезается, отключаются только рекламные вставки самого приложения.

## Права и законность

Проект показывает то, что уже опубликовано на сайтах-источниках, и не хранит копий видео.
Права на контент принадлежат правообладателям; используйте проект для личного просмотра
и в рамках закона своей страны.

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
