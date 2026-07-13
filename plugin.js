(function () {
  'use strict';

  var BASE_URL = '__BASE_URL__';
  if (BASE_URL.indexOf('__BASE' + '_URL__') >= 0) BASE_URL = '';
  var SOURCE = 'KinoKrad';
  var ICON = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M4 3h16a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2m5 4v10l8-5z"/></svg>';

  function esc(value) {
    return String(value || '').replace(/[&<>"']/g, function (x) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[x];
    });
  }

  function api(path, success, error) {
    new Lampa.Reguest().silent(BASE_URL + path, success, error || function () {
      Lampa.Noty.show('KinoKrad: сервер временно недоступен');
    });
  }

  function search(query, success, error) {
    api('/api/search?q=' + encodeURIComponent(query || ''), function (json) {
      success((json && json.items) || []);
    }, error);
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
      first_air_date: item.year ? item.year + '-01-01' : '',
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
    var network = new Lampa.Reguest();
    var scroll = new Lampa.Scroll({ mask: true, over: true });
    scroll.render().addClass('kk-online-scroll');
    var last = null;
    var mode = 'loading';
    var movie = object.movie || object.card || object.element || {};
    var detail = null;
    var selectedFile = null;
    var searchItems = [];
    var currentSeason = null;
    var currentEpisode = null;

    function active() {
      try { return Lampa.Activity.active().activity === self.activity; } catch (e) { return true; }
    }

    function focus() {
      if (!active()) return;
      try {
        Lampa.Controller.collectionSet(scroll.render());
        Lampa.Controller.collectionFocus(last || false, scroll.render());
        Lampa.Controller.toggle('content');
      } catch (e) {}
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
      scroll.body().empty().append('<div class="kk-state ' + (isError ? 'kk-error' : '') + '">' +
        (isError ? '' : '<div class="kk-spinner"></div>') + '<div>' + esc(text) + '</div></div>');
    }

    function picker(title, subtitle, items, callback, nextMode) {
      mode = nextMode;
      last = null;
      scroll.reset();
      scroll.body().empty().append('<div class="kk-online-head"><div class="kk-brand">KinoKrad</div><h2>' + esc(title) +
        '</h2><p>' + esc(subtitle || '') + '</p></div>');
      items.forEach(function (item) {
        var node = $('<div class="kk-online-item selector"><div class="kk-online-title">' + esc(item.title) +
          '</div><div class="kk-online-subtitle">' + esc(item.subtitle || '') + '</div></div>');
        var entering = false;
        var choose = function () {
          if (entering) return;
          entering = true;
          setTimeout(function () { entering = false; }, 700);
          callback(item);
        };
        node.on('hover:focus', function () {
          last = node[0];
          scroll.immediate(node, true);
        });
        node.on('hover:enter click', choose);
        scroll.append(node);
      });
      if (!items.length) scroll.body().append('<div class="kk-empty">Ничего не найдено</div>');
      focus();
    }

    function showSearchResults(items, query) {
      searchItems = items;
      picker('Результаты поиска', query, items.map(function (item) {
        var rating = item.kinopoisk ? 'КП ' + item.kinopoisk : (item.imdb ? 'IMDb ' + item.imdb : '');
        return { title: item.title, subtitle: [item.year, rating].filter(Boolean).join(' • '), data: item };
      }), function (choice) { loadDetail(choice.data); }, 'results');
    }

    function loadDetail(item) {
      movie = Object.assign({}, movie, searchCard(item));
      mode = 'loading-detail';
      state('Получаю озвучки и серии…');
      focus();
      api('/api/detail?url=' + encodeURIComponent(item.url), function (json) {
        if (!json || json.error) {
          state((json && json.error) || 'Не удалось открыть KinoKrad', true);
          return;
        }
        detail = json;
        if (detail.playback.type === 'movie') showMovieOptions();
        else showSeasons();
      }, function () { state('Не удалось открыть KinoKrad', true); });
    }

    function showMovieOptions() {
      var items = (detail.playback.options || []).map(function (item) {
        return { title: item.label || 'Озвучка', subtitle: item.quality + (item.uhd ? ' • 4K' : ''), data: item };
      });
      picker('Выберите озвучку', detail.title, items, function (item) { resolve(item.data); }, 'movie');
    }

    function showSeasons() {
      var items = (detail.playback.seasons || []).map(function (item) {
        return { title: 'Сезон ' + item.season, subtitle: item.episodes.length + ' серий', data: item };
      });
      picker('Выберите сезон', detail.title, items, function (item) { showEpisodes(item.data); }, 'seasons');
    }

    function showEpisodes(season) {
      currentSeason = season;
      var items = season.episodes.map(function (item) {
        return { title: 'Серия ' + item.episode, subtitle: item.translations.length + ' озвучек', data: item };
      });
      picker('Сезон ' + season.season, 'Выберите серию', items, function (item) { showTranslations(item.data); }, 'episodes');
    }

    function showTranslations(episode) {
      currentEpisode = episode;
      var items = episode.translations.map(function (item) {
        return { title: item.label || 'Озвучка', subtitle: item.quality || '', data: item };
      });
      picker('Серия ' + episode.episode, 'Выберите озвучку', items, function (item) { resolve(item.data); }, 'translations');
    }

    function resolve(file) {
      selectedFile = file;
      mode = 'resolving';
      state('Подготавливаю видеопоток…');
      focus();
      api('/api/resolve?embed_url=' + encodeURIComponent(detail.embed_url) + '&page_url=' +
        encodeURIComponent(detail.url) + '&file_id=' + file.file_id, function (json) {
        if (!json || !json.audios || !json.audios.length) {
          state((json && json.error) || 'Поток не найден', true);
          return;
        }
        if (json.audios.length === 1) play(json.audios[0], json.tracks || []);
        else picker('Аудиодорожка', file.label || detail.title, json.audios.map(function (audio) {
          return { title: audio.label, subtitle: (audio.qualities || []).map(function (q) { return q + 'p'; }).join(' • '), data: audio };
        }), function (item) { play(item.data, json.tracks || []); }, 'audios');
      }, function () { state('Не удалось получить видеопоток', true); });
    }

    function play(audio, tracks) {
      var retries = 0;
      var element = {
        title: detail.title + (selectedFile.season ? ' — S' + selectedFile.season + 'E' + selectedFile.episode : '') +
          ' — ' + (selectedFile.label || audio.label || SOURCE),
        url: audio.url,
        timeline: {},
        isonline: true,
        card: movie,
        hls_manifest_timeout: 25000,
        hls_retry_timeout: 45000,
        tracks: tracks || []
      };
      if (audio.quality && Object.keys(audio.quality).length) element.quality = audio.quality;
      element.error = function (work, useReserve) {
        if (retries >= 2) return;
        retries += 1;
        api('/api/resolve?embed_url=' + encodeURIComponent(detail.embed_url) + '&page_url=' +
          encodeURIComponent(detail.url) + '&file_id=' + selectedFile.file_id + '&refresh=' + Date.now(), function (fresh) {
          if (fresh.audios && fresh.audios.length) {
            var renewedAudio = fresh.audios.filter(function (item) {
              return String(item.audio_id) === String(audio.audio_id);
            })[0] || fresh.audios[0];
            var renewed = work.quality_switched && renewedAudio.quality && renewedAudio.quality[work.quality_switched] ?
              renewedAudio.quality[work.quality_switched] : renewedAudio.url;
            work.url = renewed;
            work.quality = renewedAudio.quality || work.quality;
            useReserve(renewed);
          }
        });
      };
      Lampa.Player.play(element);
    }

    function begin() {
      if (movie.url && movie.kinokrad) return loadDetail(movie);
      var query = movie.title || movie.name || movie.original_title || movie.original_name || object.search || '';
      state('Ищу «' + query + '» на KinoKrad…');
      search(query, function (items) {
        if (!items.length) return state('На KinoKrad ничего не найдено', true);
        showSearchResults(items, query);
      }, function () { state('Поиск KinoKrad временно недоступен', true); });
    }

    function goBack() {
      if (mode === 'episodes') return showSeasons();
      if (mode === 'translations') return showEpisodes(currentSeason);
      if (mode === 'audios') return detail.playback.type === 'movie' ? showMovieOptions() : showTranslations(currentEpisode);
      if ((mode === 'movie' || mode === 'seasons' || mode === 'loading-detail') && searchItems.length) {
        return showSearchResults(searchItems, object.search || movie.title || '');
      }
      Lampa.Activity.backward();
    }

    this.create = begin;
    this.start = function () {
      Lampa.Controller.add('content', {
        toggle: focus,
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
    this.render = function () { return scroll.render(); };
    this.destroy = function () { network.clear(); scroll.destroy(); };
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
    var button = $('<div class="full-start__button selector view--online kinokrad--button" data-subtitle="KinoKrad.my">' +
      ICON + '<span>Смотреть в KinoKrad</span></div>');
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
    window.kinokrad_lampa_plugin = true;
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
    '.kk-online-item{margin:.65em 2.8em;padding:1em 1.2em;border-radius:.7em;background:rgba(255,255,255,.08)}.kk-online-item.focus{background:#e53935;transform:scale(1.015)}' +
    '.kk-online-title{font-size:1.2em;font-weight:700}.kk-online-subtitle{color:#bbb;margin-top:.3em}.kk-online-item.focus .kk-online-subtitle{color:#fff}.kk-empty{padding:2em 2.8em;color:#aaa}' +
    '@keyframes kkspin{to{transform:rotate(360deg)}}@media(max-width:700px){.kk-online-head{padding:1.4em 1.4em .7em}.kk-online-item{margin:.55em 1.4em}.kk-online-head h2{font-size:2em}}' +
  '</style>');
})();
