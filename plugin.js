(function () {
  'use strict';

  var BASE_URL = '__BASE_URL__';
  if (BASE_URL.indexOf('__BASE' + '_URL__') >= 0) BASE_URL = '';
  var SOURCE = 'KinoKrad';
  var currentCard = null;
  var ICON = '<svg viewBox="0 0 24 24" width="24" height="24"><path fill="#e53935" d="M4 3h16a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2m5 4v10l8-5z"/></svg>';

  function esc(value) {
    return String(value || '').replace(/[&<>"']/g, function (x) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[x];
    });
  }

  function api(path, success, error) {
    new Lampa.Reguest().silent(BASE_URL + path, success, error || function () {
      Lampa.Noty.show('KinoKrad: ошибка сервера');
    });
  }

  function activate(root) {
    try {
      Lampa.Controller.collectionSet(root[0]);
      Lampa.Controller.collectionFocus(false, root[0]);
      Lampa.Controller.toggle('content');
    } catch (e) {}
  }

  function controller(self, back) {
    Lampa.Controller.add('content', {
      toggle: function () { activate(self.html); },
      left: function () { if (Navigator.canmove('left')) Navigator.move('left'); else Lampa.Controller.toggle('menu'); },
      right: function () { Navigator.move('right'); },
      up: function () { if (Navigator.canmove('up')) Navigator.move('up'); else Lampa.Controller.toggle('head'); },
      down: function () { Navigator.move('down'); },
      back: back
    });
    activate(self.html);
  }

  function state(root, text, error) {
    root.html('<div class="kk-state ' + (error ? 'kk-error' : '') + '">' +
      (error ? '' : '<div class="kk-spinner"></div>') + '<div>' + esc(text) + '</div></div>');
  }

  Lampa.Component.add('kinokrad_catalog', function () {
    var self = this;
    this.html = $('<div class="kk-root"></div>');
    this.sections = { movie: [], series: [] };

    this.create = function () {
      state(self.html, 'Загружаю топ KinoKrad за неделю...');
      self.loadKind('movie', 1, function () {
        self.loadKind('series', 1, function () { self.renderCatalog(); });
      });
    };

    this.loadKind = function (kind, page, done) {
      if (page > 3) return done();
      api('/api/catalog?type=' + kind + '&page=' + page, function (json) {
        self.sections[kind] = self.sections[kind].concat(json.items || []);
        self.loadKind(kind, page + 1, done);
      }, function () { done(); });
    };

    this.renderCatalog = function () {
      var html = '<div class="kk-page"><header class="kk-hero"><div class="kk-kicker">KinoKrad.my</div>' +
        '<h1>Топ за неделю</h1><p>Фильмы и сериалы с подробными карточками. Просмотр и озвучки — только KinoKrad.</p></header>';
      [['movie', 'Фильмы'], ['series', 'Сериалы']].forEach(function (entry) {
        var items = self.sections[entry[0]];
        html += '<section><h2>' + entry[1] + ' <b>' + items.length + '</b></h2><div class="kk-grid">';
        items.forEach(function (item, index) {
          html += '<div class="kk-card selector" data-kind="' + entry[0] + '" data-index="' + index + '">' +
            '<div class="kk-rank">' + (index + 1) + '</div><img src="' + esc(item.poster) + '" loading="lazy">' +
            '<div class="kk-card-title">' + esc(item.title) + '</div><div class="kk-muted">' + esc(item.year) + '</div></div>';
        });
        html += '</div></section>';
      });
      html += '</div>';
      self.html.html(html);
      self.html.find('.kk-card').off('hover:enter click').on('hover:enter click', function () {
        var node = $(this);
        var item = self.sections[node.attr('data-kind')][parseInt(node.attr('data-index'), 10)];
        currentCard = item;
        Lampa.Activity.push({ component: 'kinokrad_detail', title: item.title, card: item, params: { card: item } });
      });
      activate(self.html);
    };
    this.render = function (js) { return js ? self.html : $(self.html); };
    this.start = function () { controller(self, function () { Lampa.Activity.backward(); }); };
    this.destroy = function () { self.html.remove(); };
  });

  Lampa.Component.add('kinokrad_detail', function () {
    var self = this;
    this.html = $('<div class="kk-root"></div>');
    this.card = null;
    this.detail = null;
    this.mode = 'detail';
    this.selectedFile = null;

    this.create = function () {
      self.card = (self.activity && (self.activity.card || (self.activity.params || {}).card)) || currentCard || {};
      state(self.html, 'Открываю карточку...');
      api('/api/detail?url=' + encodeURIComponent(self.card.url || ''), function (json) {
        if (json.error) return state(self.html, json.error, true);
        self.detail = json;
        self.renderDetail();
      }, function () { state(self.html, 'Не удалось загрузить карточку KinoKrad', true); });
    };

    this.meta = function () {
      var d = self.detail, parts = [];
      if (d.year) parts.push(d.year);
      if (d.country) parts.push(d.country);
      if (d.duration) parts.push(d.duration);
      if (d.quality) parts.push(d.quality);
      return parts.join(' • ');
    };

    this.renderDetail = function () {
      self.mode = 'detail';
      var d = self.detail;
      var ratings = [];
      if (d.rating) ratings.push('KinoKrad ' + d.rating);
      if (d.kinopoisk) ratings.push('КП ' + d.kinopoisk);
      if (d.imdb) ratings.push('IMDb ' + d.imdb);
      var html = '<div class="kk-detail"><div class="kk-backdrop" style="background-image:url(\'' + esc(d.poster) + '\')"></div>' +
        '<div class="kk-detail-body"><img class="kk-detail-poster" src="' + esc(d.poster) + '"><main><div class="kk-kicker">' + (d.media_type === 'series' ? 'Сериал' : 'Фильм') + '</div>' +
        '<h1>' + esc(d.title) + '</h1><div class="kk-original">' + esc(d.original_title) + '</div>' +
        '<div class="kk-ratings">' + ratings.map(function (x) { return '<span>' + esc(x) + '</span>'; }).join('') + '</div>' +
        '<div class="kk-meta">' + esc(self.meta()) + '</div><div class="kk-genres">' + esc((d.genres || []).join(' • ')) + '</div>' +
        '<p class="kk-overview">' + esc(d.description || 'Описание отсутствует.') + '</p>' +
        (d.directors && d.directors.length ? '<div class="kk-fact"><b>Режиссёр:</b> ' + esc(d.directors.join(', ')) + '</div>' : '') +
        (d.actors && d.actors.length ? '<div class="kk-fact"><b>В ролях:</b> ' + esc(d.actors.slice(0, 10).join(', ')) + '</div>' : '') +
        '<button class="kk-watch selector">▶ Смотреть на KinoKrad</button></main></div></div>';
      self.html.html(html);
      self.html.find('.kk-watch').off('hover:enter click').on('hover:enter click', function () {
        if (d.playback.type === 'movie') self.renderMovieOptions();
        else self.renderSeasons();
      });
      activate(self.html);
    };

    this.picker = function (title, subtitle, items, callback) {
      self.html.html('<div class="kk-page"><header class="kk-hero compact"><div class="kk-kicker">KinoKrad</div><h1>' + esc(title) +
        '</h1><p>' + esc(subtitle || '') + '</p></header><div class="kk-options">' + items.map(function (item, i) {
          return '<div class="kk-option selector" data-index="' + i + '"><b>' + esc(item.title) + '</b><span>' + esc(item.subtitle || '') + '</span></div>';
        }).join('') + '</div></div>');
      self.html.find('.kk-option').off('hover:enter click').on('hover:enter click', function () {
        callback(items[parseInt($(this).attr('data-index'), 10)]);
      });
      activate(self.html);
    };

    this.renderMovieOptions = function () {
      self.mode = 'movie';
      var options = (self.detail.playback.options || []).map(function (x) {
        return { title: x.label, subtitle: x.quality + (x.uhd ? ' • 4K' : ''), data: x };
      });
      self.picker('Выберите озвучку', self.detail.title, options, function (item) { self.resolve(item.data); });
    };

    this.renderSeasons = function () {
      self.mode = 'seasons';
      var items = (self.detail.playback.seasons || []).map(function (x) {
        return { title: 'Сезон ' + x.season, subtitle: x.episodes.length + ' серий', data: x };
      });
      self.picker('Выберите сезон', self.detail.title, items, function (item) { self.renderEpisodes(item.data); });
    };

    this.renderEpisodes = function (season) {
      self.mode = 'episodes'; self.currentSeason = season;
      var items = season.episodes.map(function (x) {
        return { title: 'Серия ' + x.episode, subtitle: x.translations.length + ' озвучек', data: x };
      });
      self.picker('Сезон ' + season.season, 'Выберите серию', items, function (item) { self.renderTranslations(item.data); });
    };

    this.renderTranslations = function (episode) {
      self.mode = 'translations'; self.currentEpisode = episode;
      var items = episode.translations.map(function (x) {
        return { title: x.label, subtitle: x.quality, data: x };
      });
      self.picker('Серия ' + episode.episode, 'Выберите озвучку', items, function (item) { self.resolve(item.data); });
    };

    this.resolve = function (file) {
      self.selectedFile = file;
      Lampa.Loading.start(function () { Lampa.Loading.stop(); activate(self.html); });
      api('/api/resolve?embed_url=' + encodeURIComponent(self.detail.embed_url) + '&page_url=' + encodeURIComponent(self.detail.url) + '&file_id=' + file.file_id, function (json) {
        Lampa.Loading.stop();
        if (!json.audios || !json.audios.length) return Lampa.Noty.show(json.error || 'Поток не найден');
        if (json.audios.length === 1) self.play(json.audios[0], json.tracks || []);
        else {
          self.mode = 'audios'; self.audioPayload = json;
          self.picker('Аудиодорожка', file.label || self.detail.title, json.audios.map(function (x) {
            return { title: x.label, subtitle: (x.qualities || []).join('p • ') + 'p', data: x };
          }), function (item) { self.play(item.data, json.tracks || []); });
        }
      }, function () { Lampa.Loading.stop(); Lampa.Noty.show('Не удалось получить поток KinoKrad'); });
    };

    this.play = function (audio, tracks) {
      var file = self.selectedFile;
      var title = self.detail.title + ' — ' + (file.label || audio.label || 'KinoKrad');
      var retries = 0;
      var element = {
        title: title, url: audio.url, timeline: {}, isonline: true, card: self.card,
        hls_manifest_timeout: 25000, hls_retry_timeout: 45000, tracks: tracks || []
      };
      element.error = function (work, useReserve) {
        if (retries >= 2) return;
        retries += 1;
        api('/api/resolve?embed_url=' + encodeURIComponent(self.detail.embed_url) + '&page_url=' + encodeURIComponent(self.detail.url) + '&file_id=' + file.file_id + '&refresh=' + Date.now(), function (fresh) {
          if (fresh.audios && fresh.audios.length) {
            work.url = fresh.audios[0].url;
            useReserve(work.url);
          }
        });
      };
      Lampa.Player.play(element);
    };

    this.goBack = function () {
      if (self.mode === 'detail') return Lampa.Activity.backward();
      if (self.mode === 'episodes') return self.renderSeasons();
      if (self.mode === 'translations') return self.renderEpisodes(self.currentSeason);
      if (self.mode === 'audios') {
        if (self.detail.media_type === 'movie') return self.renderMovieOptions();
        return self.renderTranslations(self.currentEpisode);
      }
      return self.renderDetail();
    };
    this.render = function (js) { return js ? self.html : $(self.html); };
    this.start = function () { controller(self, function () { self.goBack(); }); };
    this.destroy = function () { self.html.remove(); };
  });

  function start() {
    if (window.kinokrad_lampa_plugin) return;
    window.kinokrad_lampa_plugin = true;
    var item = $('<li data-action="kinokrad" class="menu__item selector"><div class="menu__ico">' + ICON + '</div><div class="menu__text">KinoKrad</div></li>');
    $('.menu .menu__list').eq(0).append(item);
    item.on('hover:enter click', function () { Lampa.Activity.push({ component: 'kinokrad_catalog', title: SOURCE }); });
  }
  if (window.appready) start();
  else Lampa.Listener.follow('app', function (event) { if (event.type === 'ready') start(); });

  $('head').append('<style>' +
    '.kk-root{height:100%;overflow-y:auto;background:#0c0d10;color:#fff}.kk-page{padding:2.4em 2.8em 5em;background:radial-gradient(circle at 12% 0,rgba(229,57,53,.2),transparent 35em)}' +
    '.kk-hero{max-width:60em;margin-bottom:2.2em}.kk-hero.compact{margin-bottom:1.2em}.kk-hero h1,.kk-detail h1{font-size:3em;line-height:1.05;margin:.18em 0}.kk-hero p{font-size:1.08em;color:#b8bbc4}' +
    '.kk-kicker{display:inline-block;padding:.32em .7em;border-radius:1em;background:#e53935;font-weight:800}.kk-page section{margin-top:2.5em}.kk-page h2{font-size:1.55em}.kk-page h2 b{font-size:.65em;background:#e53935;padding:.25em .5em;border-radius:.5em}' +
    '.kk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(9.5em,1fr));gap:1.15em}.kk-card{position:relative;padding:.42em;border-radius:.8em;background:rgba(255,255,255,.05)}' +
    '.kk-card.focus,.kk-option.focus,.kk-watch.focus{transform:scale(1.035);box-shadow:0 0 0 .2em #ef5350;background:rgba(229,57,53,.22)}.kk-card img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:.6em}.kk-rank{position:absolute;top:.7em;left:.7em;background:#111c;padding:.35em .55em;border-radius:1em;font-weight:800}' +
    '.kk-card-title{font-weight:750;line-height:1.2;margin-top:.65em;min-height:2.4em}.kk-muted,.kk-original,.kk-genres{color:#aeb1ba}.kk-state{height:100%;display:flex;gap:1em;flex-direction:column;align-items:center;justify-content:center}.kk-spinner{width:2.5em;height:2.5em;border:.22em solid #ffffff22;border-top-color:#e53935;border-radius:50%;animation:kkspin .8s linear infinite}.kk-error{color:#ef9a9a}' +
    '.kk-detail{min-height:100%;position:relative;overflow:hidden}.kk-backdrop{position:absolute;inset:0;background-size:cover;background-position:center;filter:blur(35px);opacity:.2;transform:scale(1.1)}.kk-detail-body{position:relative;display:grid;grid-template-columns:18em 1fr;gap:2.5em;padding:3em;max-width:85em}.kk-detail-poster{width:100%;border-radius:1em;box-shadow:0 1em 3em #000}.kk-ratings{display:flex;gap:.6em;flex-wrap:wrap;margin:1em 0}.kk-ratings span{background:#fbc02d;color:#17130a;padding:.35em .62em;border-radius:.45em;font-weight:800}.kk-meta{font-size:1.08em;margin:.8em 0}.kk-overview{font-size:1.12em;line-height:1.55;max-width:56em}.kk-fact{margin-top:.8em;color:#d0d2d9}.kk-watch{margin-top:1.5em;padding:.8em 1.2em;border:0;border-radius:.65em;background:#e53935;color:#fff;font-size:1.15em;font-weight:800}' +
    '.kk-options{display:grid;grid-template-columns:repeat(auto-fill,minmax(15em,1fr));gap:1em}.kk-option{padding:1.1em;border-radius:.75em;background:#ffffff0c;display:flex;flex-direction:column;gap:.35em}.kk-option span{color:#aeb1ba}' +
    '@keyframes kkspin{to{transform:rotate(360deg)}}@media(max-width:700px){.kk-page{padding:1.3em}.kk-grid{grid-template-columns:repeat(auto-fill,minmax(7.3em,1fr))}.kk-detail-body{grid-template-columns:8em 1fr;padding:1.3em;gap:1.2em}.kk-detail h1,.kk-hero h1{font-size:2em}}' +
  '</style>');
})();
