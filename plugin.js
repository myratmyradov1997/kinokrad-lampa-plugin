// Отключаем рекламные вставки Lampa для всех источников воспроизведения.
(function () {
  'use strict';
  function install() {
    if (window.lampa_noads_plugin || !window.Lampa || !Lampa.Player || !Lampa.Player.listener) return;
    window.lampa_noads_plugin = '1.1.0';
    if (Lampa.Storage) Lampa.Storage.set('video_quality_default', '2160');
    Lampa.Player.listener.follow('create', function (event) {
      var data = event && event.data;
      if (!data) return;
      delete data.vast_url;
      delete data.vast_banner;
      var list = Lampa.PlayerPlaylist.get() || [];
      if (list.indexOf(data) < 0) {
        var index = list.findIndex(function (item) { return item.url === data.url; });
        list = index >= 0 ? list.slice() : [data];
        if (index >= 0) list[index] = data;
        Lampa.PlayerPlaylist.set(list);
      }
      // App's native skip-preroll path, also excludes in-player ad banners.
      data.continue_play = true;
    });
    // Empty only Lampa's ad catalogs; normal search/metadata requests stay intact.
    var prototype = window.XMLHttpRequest && XMLHttpRequest.prototype;
    var descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, 'responseText');
    if (descriptor && descriptor.get && descriptor.configurable) {
      Object.defineProperty(prototype, 'responseText', {
        configurable: true, enumerable: descriptor.enumerable,
        get: function () {
          if (/\/api\/ad\/get\/(?:preroll|banner)(?:[?#]|$)/.test(this.responseURL || '')) return '{"ad":[]}';
          return descriptor.get.call(this);
        }
      });
    }
    console.log('[LampaNoAds] реклама Lampa отключена для всех источников');
  }
  if (window.Lampa) install();
  else if (window.addEventListener) window.addEventListener('load', install, { once: true });
})();

// == KinoKrad для Lampa v3 — домашний сервер, мгновенный выбор озвучки/серии ==
(function () {
  'use strict';

  var VERSION = '3.4.5';
  var BASE_URL = '__BASE_URL__';
  if (BASE_URL.indexOf('__BASE' + '_URL__') >= 0) BASE_URL = '';
  var SOURCE = 'KinoKrad';
  var PREFS_KEY = 'kinokrad_prefs';
  var ICON = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M4 3h16a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2m5 4v10l8-5z"/></svg>';

  // Результаты /api/prepare по id карточки TMDB: запускаются при открытии
  // карточки, поэтому к нажатию кнопки поиск, озвучки и плеер уже готовы.
  var prepared = {};

  function esc(value) {
    return String(value || '').replace(/[&<>"']/g, function (x) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[x];
    });
  }

  function qs(params) {
    var out = [];
    for (var key in params) {
      if (params.hasOwnProperty(key) && params[key] !== undefined && params[key] !== null && params[key] !== '') {
        out.push(encodeURIComponent(key) + '=' + encodeURIComponent(params[key]));
      }
    }
    return out.join('&');
  }

  function api(path, success, error, timeout) {
    var request = new Lampa.Reguest();
    if (timeout) { try { request.timeout(timeout); } catch (e) {} }
    request.silent(BASE_URL + path, success, error || function () {
      Lampa.Noty.show('KinoKrad: домашний сервер недоступен');
    });
    return request;
  }

  function fire(path) {
    try { new Lampa.Reguest().silent(BASE_URL + path, function () {}, function () {}); } catch (e) {}
  }

  function prefs(url, value) {
    var all = {};
    try { all = Lampa.Storage.get(PREFS_KEY, '{}') || {}; } catch (e) {}
    if (typeof all === 'string') { try { all = JSON.parse(all); } catch (e) { all = {}; } }
    if (value === undefined) return all[url] || {};
    all[url] = Object.assign({}, all[url] || {}, value, { at: Date.now() });
    var keys = Object.keys(all);
    if (keys.length > 300) {
      keys.sort(function (a, b) { return (all[a].at || 0) - (all[b].at || 0); });
      keys.slice(0, keys.length - 300).forEach(function (k) { delete all[k]; });
    }
    try { Lampa.Storage.set(PREFS_KEY, all); } catch (e) {}
    return all[url];
  }

  function cardYear(card) {
    return String(card.release_date || card.first_air_date || card.year || '').slice(0, 4);
  }

  function cardType(card) {
    return (card.name || card.first_air_date || card.number_of_seasons) ? 'series' : 'movie';
  }

  function prepareParams(card) {
    return qs({
      title: card.title || card.name || '',
      original_title: card.original_title || card.original_name || '',
      year: cardYear(card),
      type: cardType(card)
    });
  }

  function prepare(card, done) {
    var id = String(card.id || card.title || card.name || '');
    var slot = prepared[id];
    if (slot && slot.at > Date.now() - 10 * 60 * 1000) {
      if (done) {
        if (slot.result || slot.failed) done(slot.result || null);
        else slot.waiting.push(done);
      }
      return;
    }
    slot = prepared[id] = { at: Date.now(), waiting: done ? [done] : [], result: null, failed: false };
    api('/api/prepare?' + prepareParams(card), function (json) {
      slot.result = json && !json.error ? json : null;
      slot.failed = !slot.result;
      slot.waiting.splice(0).forEach(function (cb) { cb(slot.result); });
    }, function () {
      slot.failed = true;
      slot.waiting.splice(0).forEach(function (cb) { cb(null); });
    }, 30000);
  }

  function searchCard(item) {
    return {
      id: item.id,
      title: item.title,
      name: item.title,
      original_title: item.title,
      original_name: item.title,
      year: item.year,
      release_date: item.year ? item.year + '-01-01' : '',
      img: item.poster,
      poster: item.poster,
      overview: item.description || '',
      vote_average: parseFloat(item.kinopoisk || item.imdb || 0),
      url: item.url,
      kinokrad: true,
      source: 'kinokrad'
    };
  }

  function KinoKradOnline(object) {
    var self = this;
    var scroll = new Lampa.Scroll({ mask: true, over: true });
    scroll.render().addClass('kk-online-scroll');
    var requests = [];
    var last = null;
    var mode = 'loading';
    var movie = object.movie || object.card || object.element || {};
    var detail = null;
    var searchItems = [];
    var currentSeason = null;
    var currentEpisode = null;
    var currentFile = null;
    var lastStream = null;
    var focusTimer = null;
    var destroyed = false;
    var silent = false;

    function remember(r) { if (r) requests.push(r); return r; }

    function active() {
      try { return Lampa.Activity.active().activity === self.activity; } catch (e) { return true; }
    }

    // Вызывается самим контроллером Lampa: только привязать список.
    // Не вызывать отсюда Controller.toggle — это бесконечная рекурсия.
    function bindCollection() {
      try {
        Lampa.Controller.collectionSet(scroll.render());
        Lampa.Controller.collectionFocus(last || false, scroll.render());
      } catch (e) {}
    }

    function focus() {
      if (!active() || destroyed || silent) return;
      try { Lampa.Controller.toggle('content'); } catch (e) {}
    }

    function scrollToFocused() {
      setTimeout(function () {
        if (!active()) return;
        var target = scroll.render().find('.selector.focus').first();
        if (!target.length) return;
        last = target[0];
        scroll.immediate(target, true);
      }, 0);
    }

    function state(text, isError) {
      last = null;
      scroll.reset();
      scroll.body().empty().append('<div class="kk-state ' + (isError ? 'kk-error' : '') + '">' +
        (isError ? '' : '<div class="kk-spinner"></div>') + '<div>' + esc(text) + '</div></div>');
    }

    // items: [{title, subtitle, data, onFocus, focus: bool}]
    function picker(title, subtitle, items, callback, nextMode) {
      mode = nextMode;
      last = null;
      scroll.reset();
      scroll.body().empty().append('<div class="kk-online-head"><div class="kk-brand">KinoKrad</div><h2>' + esc(title) +
        '</h2><p>' + esc(subtitle || '') + '</p></div>');
      var focusNode = null;
      items.forEach(function (item) {
        var node = $('<div class="kk-online-item selector' + (item.accent ? ' kk-accent' : '') + '"><div class="kk-online-title">' +
          esc(item.title) + '</div><div class="kk-online-subtitle">' + esc(item.subtitle || '') + '</div></div>');
        var entering = false;
        node.on('hover:focus', function () {
          last = node[0];
          scroll.immediate(node, true);
          clearTimeout(focusTimer);
          if (item.onFocus) focusTimer = setTimeout(item.onFocus, 450);
        });
        node.on('hover:enter click', function () {
          if (entering) return;
          entering = true;
          setTimeout(function () { entering = false; }, 700);
          clearTimeout(focusTimer);
          callback(item);
        });
        scroll.append(node);
        if (item.focus && !focusNode) focusNode = node[0];
      });
      if (!items.length) scroll.body().append('<div class="kk-empty">Ничего не найдено</div>');
      last = focusNode;
      focus();
      if (focusNode && !silent) scroll.immediate($(focusNode), true);
    }

    // Перерисовать список под плеером, не забирая пульт у плеера.
    function renderBehindPlayer() {
      silent = true;
      try {
        if (detail.playback.type === 'movie') showMovieOptions(true);
        else if (currentSeason) showEpisodes(currentSeason);
      } catch (e) {}
      silent = false;
    }

    // ---- поиск и карточка -----------------------------------------
    function showSearchResults(items, query) {
      searchItems = items;
      picker('Результаты поиска', query, items.map(function (item) {
        var rating = item.kinopoisk ? 'КП ' + item.kinopoisk : (item.imdb ? 'IMDb ' + item.imdb : '');
        return { title: item.title, subtitle: [item.year, item.media_type === 'series' ? 'сериал' : '', rating].filter(Boolean).join(' • '), data: item };
      }), function (choice) { loadDetail(choice.data); }, 'results');
    }

    function loadDetail(item) {
      movie = Object.assign({}, movie, { kk_url: item.url });
      mode = 'loading-detail';
      state('Получаю озвучки и серии…');
      focus();
      remember(api('/api/detail?url=' + encodeURIComponent(item.url), function (json) {
        if (!json || json.error) return state((json && json.error) || 'Не удалось открыть KinoKrad', true);
        onDetail(json);
      }, function () { state('Не удалось открыть KinoKrad', true); }, 30000));
    }

    function onDetail(json) {
      detail = json;
      if (detail.playback.type === 'movie') showMovieOptions();
      else if (detail.playback.seasons.length === 1 && !prefs(detail.url).episode) showEpisodes(detail.playback.seasons[0]);
      else showSeasons();
    }

    // ---- параметры потока -------------------------------------------
    function streamQuery(file, extra) {
      return qs(Object.assign({
        embed_url: detail.embed_url,
        page_url: detail.url,
        file_id: file.file_id,
        tid: file.translation_id,
        quality: file.quality,
        group: file.group,
        season: file.season,
        episode: file.episode,
        prefer: file.label,
        provider: prefs(detail.url).provider || 'main'
      }, extra || {}));
    }

    function prefetch(file) {
      if (file && detail) fire('/api/prefetch?' + streamQuery(file));
    }

    function preferredTranslation(episode) {
      var pref = prefs(detail.url);
      var list = episode.translations || [];
      if (pref.label) {
        for (var i = 0; i < list.length; i++) if (list[i].label === pref.label) return list[i];
      }
      return list.length === 1 ? list[0] : null;
    }

    function playerChoiceItem() {
      return { title: 'Плеер: ' + (prefs(detail.url).provider === 'backup' ? 'Видео 2' : 'Основной'),
        subtitle: 'Сменить плеер • качество и озвучки могут отличаться', data: { switchPlayer: true } };
    }

    function choosePlayer() {
      picker('Выберите плеер', 'У второго плеера собственные качества и аудиодорожки', [
        { title: 'Основной', subtitle: 'Выбор привычных озвучек и максимального качества', data: 'main' },
        { title: 'Видео 2', subtitle: 'Запасной источник без VPN; озвучка и качество могут отличаться', data: 'backup' }
      ], function (item) {
        prefs(detail.url, { provider: item.data });
        if (currentSeason) showEpisodes(currentSeason);
        else showMovieOptions(true);
      }, 'players');
    }

    function unavailable() {
      if (detail.backup_embed_url && prefs(detail.url).provider !== 'backup') {
        picker('Основной плеер недоступен', 'Сайт отклонил подключение. У второго плеера могут отличаться озвучка и качество.', [
          { title: 'Выбрать дорожку в Видео 2', subtitle: 'Видео загружается без VPN', data: 'backup' },
          { title: 'Вернуться', data: 'back' }
        ], function (item) {
          if (item.data === 'backup') { prefs(detail.url, { provider: 'backup' }); startPlayback(currentFile); }
          else if (currentSeason) showEpisodes(currentSeason);
          else showMovieOptions(true);
        }, 'unavailable');
      } else state('Не удалось получить видеопоток. Попробуй позже или выбери другой плеер.', true);
    }

    // ---- фильм -------------------------------------------------------
    function showMovieOptions(listOnly) {
      var options = detail.playback.options || [];
      var groups = {};
      options.forEach(function (o) { groups[o.group || ''] = true; });
      var manyGroups = Object.keys(groups).length > 1;
      var pref = prefs(detail.url);
      if (pref.provider === 'backup') {
        return picker(detail.title, 'Видео 2 • дорожка и доступное качество выбираются при запуске', [
          playerChoiceItem(),
          { title: 'Выбрать аудиодорожку', data: options[0] }
        ], function (item) { if (item.data.switchPlayer) choosePlayer(); else startPlayback(item.data); }, 'movie');
      }
      if (options.length === 1 && !listOnly && !detail.backup_embed_url) return startPlayback(options[0]);
      var items = options.map(function (o) {
        var sub = [o.quality, o.uhd ? '4K' : '', manyGroups && o.group === 'directors' ? 'режиссёрская версия' : ''].filter(Boolean).join(' • ');
        return { title: o.label || 'Озвучка', subtitle: sub, data: o, focus: pref.label === o.label,
          onFocus: function () { prefetch(o); } };
      });
      if (detail.backup_embed_url) items.unshift(playerChoiceItem());
      picker('Выберите озвучку', detail.title, items, function (item) { if (item.data.switchPlayer) choosePlayer(); else startPlayback(item.data); }, 'movie');
    }

    // ---- сериал ------------------------------------------------------
    function findEpisode(seasonNo, episodeNo) {
      var seasons = detail.playback.seasons || [];
      for (var i = 0; i < seasons.length; i++) {
        if (seasons[i].season !== seasonNo) continue;
        for (var j = 0; j < seasons[i].episodes.length; j++) {
          if (seasons[i].episodes[j].episode === episodeNo) return { season: seasons[i], episode: seasons[i].episodes[j] };
        }
      }
      return null;
    }

    function nextAfter(seasonNo, episodeNo) {
      var seasons = detail.playback.seasons || [];
      for (var i = 0; i < seasons.length; i++) {
        var eps = seasons[i].episodes;
        for (var j = 0; j < eps.length; j++) {
          if (seasons[i].season === seasonNo && eps[j].episode === episodeNo) {
            if (j + 1 < eps.length) return { season: seasons[i], episode: eps[j + 1] };
            if (i + 1 < seasons.length && seasons[i + 1].episodes.length) return { season: seasons[i + 1], episode: seasons[i + 1].episodes[0] };
            return null;
          }
        }
      }
      return null;
    }

    function continueItem() {
      var pref = prefs(detail.url);
      if (!pref.season || !pref.episode) return null;
      var target = pref.watched ? nextAfter(pref.season, pref.episode) : findEpisode(pref.season, pref.episode);
      if (!target) target = findEpisode(pref.season, pref.episode);
      if (!target) return null;
      return {
        title: '▶ Продолжить: сезон ' + target.season.season + ', серия ' + target.episode.episode,
        subtitle: pref.provider === 'backup' ? 'Видео 2' + (pref.audio ? ' • ' + pref.audio : '') : (pref.label ? 'Озвучка: ' + pref.label : ''),
        accent: true, focus: true, data: { cont: target },
        onFocus: function () { prefetch(preferredTranslation(target.episode) || target.episode.translations[0]); }
      };
    }

    function showSeasons() {
      var items = [];
      var cont = continueItem();
      if (cont) items.push(cont);
      (detail.playback.seasons || []).forEach(function (s) {
        items.push({ title: 'Сезон ' + s.season, subtitle: s.episodes.length + ' серий', data: s });
      });
      picker(detail.title, 'Выберите сезон', items, function (item) {
        if (item.data.cont) {
          currentSeason = item.data.cont.season;
          return chooseTranslation(item.data.cont.episode);
        }
        showEpisodes(item.data);
      }, 'seasons');
    }

    function showEpisodes(season) {
      currentSeason = season;
      var pref = prefs(detail.url);
      var items = [];
      if (detail.backup_embed_url) items.push(playerChoiceItem());
      if (pref.label && pref.provider !== 'backup') {
        items.push({ title: 'Озвучка: ' + pref.label, subtitle: 'Нажмите, чтобы выбрать другую', data: { resetVoice: true } });
      }
      var focusEpisode = pref.season === season.season ? (pref.watched ? pref.episode + 1 : pref.episode) : 1;
      season.episodes.forEach(function (ep) {
        var count = ep.translations.length;
        items.push({
          title: 'Серия ' + ep.episode,
          subtitle: (pref.season === season.season && ep.episode < focusEpisode ? '✓ просмотрено • ' : '') + (pref.provider === 'backup' ? 'Видео 2 • дорожка при запуске' : count + ' ' + (count === 1 ? 'озвучка' : 'озвучек')),
          data: ep,
          focus: ep.episode === focusEpisode,
          onFocus: function () { prefetch(preferredTranslation(ep) || ep.translations[0]); }
        });
      });
      picker('Сезон ' + season.season, detail.title, items, function (item) {
        if (item.data.switchPlayer) return choosePlayer();
        if (item.data.resetVoice) {
          prefs(detail.url, { label: '' });
          return showEpisodes(season);
        }
        chooseTranslation(item.data);
      }, 'episodes');
    }

    function chooseTranslation(episode) {
      currentEpisode = episode;
      if (prefs(detail.url).provider === 'backup') return startPlayback(episode.translations[0]);
      var preferred = preferredTranslation(episode);
      if (preferred) return startPlayback(preferred);
      var items = episode.translations.map(function (t) {
        return { title: t.label || 'Озвучка', subtitle: t.quality || '', data: t, onFocus: function () { prefetch(t); } };
      });
      picker('Серия ' + episode.episode, 'Выберите озвучку', items, function (item) { startPlayback(item.data); }, 'translations');
    }

    // ---- воспроизведение --------------------------------------------
    function startPlayback(file, refresh) {
      currentFile = file;
      prefs(detail.url, { label: file.label, season: file.season || 0, episode: file.episode || 0, watched: false });
      mode = 'resolving';
      state('Подготавливаю видеопоток…');
      focus();
      remember(api('/api/resolve?' + streamQuery(file, refresh ? { refresh: Date.now() } : null), function (json) {
        if (!json || !json.audios || !json.audios.length) return state((json && json.error) || 'Поток не найден', true);
        lastStream = json;
        if (json.default_audio !== null && json.default_audio !== undefined) return play(json.audios[json.default_audio], json);
        picker('Аудиодорожка', prefs(detail.url).provider === 'backup' ? 'Видео 2' : (file.label || detail.title), json.audios.map(function (audio) {
          return { title: audio.label, subtitle: (audio.qualities || []).map(function (q) { return q + 'p'; }).join(' • '), data: audio };
        }), function (item) { play(item.data, json); }, 'audios');
      }, unavailable, 60000));
    }

    function fileTitle(file, audio) {
      return detail.title + (file.season ? ' — S' + file.season + 'E' + file.episode : '') +
        ' — ' + (prefs(detail.url).provider === 'backup' ? ('Видео 2' + (audio ? ' • ' + audio.label : '')) : (file.label || (audio && audio.label) || SOURCE));
    }

    function timelineFor(file) {
      try {
        var hash = Lampa.Utils.hash([detail.url, file.season || 0, file.episode || 0].join(':'));
        return Lampa.Timeline.view(hash);
      } catch (e) { return {}; }
    }

    function recoveryFor(file, audio) {
      var retries = 0;
      return function (work, useReserve) {
        if (retries >= 2) return;
        retries += 1;
        api('/api/resolve?' + streamQuery(file, { refresh: Date.now() }), function (fresh) {
          if (!fresh || !fresh.audios || !fresh.audios.length) return;
          var renewed = fresh.audios.filter(function (a) { return a.index === audio.index; })[0] || fresh.audios[0];
          var currentHeight = parseInt(work.quality_switched || Object.keys(audio.quality || {})[0], 10);
          // Ошибка AV1/4K не должна повторять тот же неподдерживаемый поток.
          var fallback = currentHeight > 1080 ? renewed.quality['1080p'] : (retries > 1 ? renewed.quality['720p'] : '');
          var url = fallback || (work.quality_switched && renewed.quality[work.quality_switched] ? renewed.quality[work.quality_switched] : renewed.url);
          if (fallback) work.quality_switched = currentHeight > 1080 ? '1080p' : '720p';
          work.url = url;
          work.quality = renewed.quality;
          useReserve(url);
        }, function () {}, 60000);
      };
    }

    function playlistFor(file) {
      // Остальные серии сезона: ссылка запрашивается только при выборе.
      if (!file.season || !currentSeason) return null;
      var list = [];
      currentSeason.episodes.forEach(function (ep) {
        var t = preferredTranslation(ep) || ep.translations[0];
        if (!t) return;
        var cell = { title: fileTitle(t), timeline: timelineFor(t) };
        if (t.file_id === file.file_id) cell.url = '__current__';
        else cell.url = function (call) {
          api('/api/resolve?' + streamQuery(t), function (json) {
            var a = json && json.audios && (json.audios.filter(function (x) { return x.label === prefs(detail.url).audio; })[0] || json.audios[json.default_audio || 0]);
            if (a) {
              cell.title = fileTitle(t, a);
              cell.url = a.url;
              cell.quality = a.quality;
              cell.error = recoveryFor(t, a);
              cell.card = movie;
              cell.isonline = true;
              cell.tracks = json.tracks || [];
              prefs(detail.url, { season: t.season, episode: t.episode, watched: false });
            }
            call();
          }, function () { call(); }, 60000);
        };
        list.push(cell);
      });
      return list.length > 1 ? list : null;
    }

    function play(audio, stream) {
      var file = currentFile;
      var element = {
        title: fileTitle(file, audio),
        url: audio.url,
        quality: audio.quality,
        timeline: timelineFor(file),
        isonline: true,
        card: movie,
        hls_manifest_timeout: 25000,
        hls_retry_timeout: 45000,
        tracks: stream.tracks || []
      };
      element.error = recoveryFor(file, audio);
      prefs(detail.url, { audio: audio.label });
      renderBehindPlayer();
      var list = null;
      try { list = playlistFor(file); } catch (e) { list = null; }
      if (list) list.forEach(function (cell) { if (cell.url === '__current__') { cell.url = element.url; cell.quality = element.quality; } });
      // Lampa пропускает преролл для элемента текущего плейлиста с continue_play.
      // Передаём тот же объект, а не копию: проверка в ядре использует indexOf.
      element.continue_play = true;
      list = list || [element];
      list = list.map(function (cell) { return cell.url === element.url ? element : cell; });
      if (list.indexOf(element) < 0) list.unshift(element);
      Lampa.Player.playlist(list);
      Lampa.Player.play(element);
      try {
        Lampa.Player.listener.follow('destroy', function onDestroy() {
          Lampa.Player.listener.remove('destroy', onDestroy);
          var t = element.timeline || {};
          if (t.percent && t.percent > 85) prefs(detail.url, { watched: true });
        });
      } catch (e) {}
    }

    // ---- навигация -----------------------------------------------------
    function begin() {
      if (movie.url && movie.kinokrad) return loadDetail(movie);
      var query = movie.title || movie.name || movie.original_title || movie.original_name || object.search || '';
      state('Ищу «' + query + '» на KinoKrad…');
      prepare(movie, function (json) {
        if (destroyed) return;
        if (!json) return state('Поиск KinoKrad временно недоступен', true);
        if (json.confident && json.detail) return onDetail(json.detail);
        if (!json.items || !json.items.length) return state('На KinoKrad ничего не найдено', true);
        showSearchResults(json.items, query);
      });
    }

    function goBack() {
      if ((mode === 'players' || mode === 'unavailable') && detail) return currentSeason ? showEpisodes(currentSeason) : showMovieOptions(true);
      if (mode === 'episodes' && detail && detail.playback.seasons.length > 1) return showSeasons();
      if (mode === 'translations') return showEpisodes(currentSeason);
      if (mode === 'audios') return detail.playback.type === 'movie' ? showMovieOptions() : showEpisodes(currentSeason);
      if (mode === 'resolving' && detail) return detail.playback.type === 'movie' ? showMovieOptions() : (currentSeason ? showEpisodes(currentSeason) : showSeasons());
      if ((mode === 'movie' || mode === 'seasons' || mode === 'episodes' || mode === 'loading-detail') && searchItems.length) {
        return showSearchResults(searchItems, object.search || movie.title || '');
      }
      Lampa.Activity.backward();
    }

    this.create = begin;
    this.start = function () {
      Lampa.Controller.add('content', {
        toggle: bindCollection,
        left: function () { if (Navigator.canmove('left')) { Navigator.move('left'); scrollToFocused(); } else Lampa.Controller.toggle('menu'); },
        right: function () { Navigator.move('right'); scrollToFocused(); },
        up: function () { if (Navigator.canmove('up')) { Navigator.move('up'); scrollToFocused(); } else Lampa.Controller.toggle('head'); },
        down: function () { Navigator.move('down'); scrollToFocused(); },
        enter: function () {
          var target = scroll.render().find('.selector.focus').first();
          if (!target.length) target = scroll.render().find('.selector').first();
          if (target.length) target.trigger('hover:enter');
        },
        back: goBack
      });
      focus();
    };
    this.pause = function () {};
    this.stop = function () {};
    this.render = function () { return scroll.render(); };
    this.destroy = function () {
      destroyed = true;
      clearTimeout(focusTimer);
      requests.forEach(function (r) { try { r.clear(); } catch (e) {} });
      scroll.destroy();
    };
  }

  function addSearchSource() {
    if (!Lampa.Search || !Lampa.Search.addSource) return;
    var request = new Lampa.Reguest();
    Lampa.Search.addSource({
      title: SOURCE,
      search: function (params, complete) {
        var query = params.query || '';
        try { query = decodeURIComponent(query); } catch (e) {}
        request.silent(BASE_URL + '/api/search?q=' + encodeURIComponent(query), function (json) {
          var cards = ((json && json.items) || []).map(searchCard);
          complete(cards.length ? [{ title: SOURCE, results: cards }] : []);
        }, function () { complete([]); });
      },
      onCancel: function () { request.clear(); },
      onMore: function (params, close) { close(); },
      onSelect: function (params, close) {
        close();
        Lampa.Activity.push({ component: 'kinokrad_online', title: SOURCE, movie: params.element, search: params.element.title });
      },
      params: { lazy: true, align_left: true, card_events: { onMenu: function () {} } }
    });
  }

  function addFullButton(event) {
    var activity = event.object.activity.render();
    if (activity.find('.kinokrad--button').length) return;
    var movie = event.data.movie;
    if (!movie) return;
    // Фоном: поиск на KinoKrad, карточка и прогрев плеера.
    prepare(movie);
    var button = $('<div class="full-start__button selector view--online kinokrad--button" data-subtitle="KinoKrad">' +
      ICON + '<span>KinoKrad</span></div>');
    var opening = false;
    button.on('hover:enter click', function () {
      if (opening) return;
      opening = true;
      setTimeout(function () { opening = false; }, 700);
      Lampa.Activity.push({ component: 'kinokrad_online', title: SOURCE, movie: movie, search: movie.title || movie.name || '' });
    });
    var torrent = activity.find('.view--torrent').first();
    if (torrent.length) torrent.after(button);
    else activity.find('.full-start__buttons').first().append(button);
  }

  function start() {
    if (window.kinokrad_lampa_plugin) return;
    window.kinokrad_lampa_plugin = VERSION;
    Lampa.Component.add('kinokrad_online', KinoKradOnline);
    addSearchSource();
    Lampa.Listener.follow('full', function (event) {
      if (event.type === 'complite') addFullButton(event);
    });
    try {
      var active = Lampa.Activity.active();
      if (active.component === 'full') addFullButton({ object: active, data: { movie: active.card } });
    } catch (e) {}
  }

  if (window.appready) start();
  else Lampa.Listener.follow('app', function (event) { if (event.type === 'ready') start(); });

  $('head').append('<style>' +
    '.kinokrad--button svg{width:1.7em;height:1.7em}.kk-online-scroll{height:100%}.kk-online-scroll .scroll__body{min-height:100%}.kk-state{min-height:65vh;display:flex;gap:1em;flex-direction:column;align-items:center;justify-content:center;text-align:center}' +
    '.kk-spinner{width:2.5em;height:2.5em;border:.22em solid #ffffff22;border-top-color:#e53935;border-radius:50%;animation:kkspin .8s linear infinite}.kk-error{color:#ef9a9a}' +
    '.kk-online-head{padding:2.2em 2.8em 1em}.kk-online-head h2{font-size:2.5em;margin:.25em 0}.kk-online-head p{color:#aaa;margin:0}.kk-brand{display:inline-block;padding:.3em .7em;border-radius:1em;background:#e53935;font-weight:800}' +
    '.kk-online-item{margin:.65em 2.8em;padding:1em 1.2em;border-radius:.7em;background:rgba(255,255,255,.08)}.kk-online-item.focus{background:#e53935;transform:scale(1.015)}.kk-accent{border:.12em solid #e53935}' +
    '.kk-online-title{font-size:1.2em;font-weight:700}.kk-online-subtitle{color:#bbb;margin-top:.3em}.kk-online-item.focus .kk-online-subtitle{color:#fff}.kk-empty{padding:2em 2.8em;color:#aaa}' +
    '@keyframes kkspin{to{transform:rotate(360deg)}}@media(max-width:700px){.kk-online-head{padding:1.4em 1.4em .7em}.kk-online-item{margin:.55em 1.4em}.kk-online-head h2{font-size:2em}}' +
  '</style>');
})();
