// Проверка JS-плагинов без телевизора: Lampa подменяется минимальным стабом.
// Запуск: node tests/test_plugins.cjs
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

function makeLampa() {
  const state = { handlers: {}, buttons: [], activities: [], plays: [], Component: null };
  let select;

  function jq(input) {
    const o = {
      length: 0,
      addClass() { return this },
      empty() { return this },
      first() { return this },
      find() { return jq('container') },
      append(x) { if (x && x.html) state.buttons.push(x); return this },
      before(x) { state.buttons.push(x) },
      after(x) { state.buttons.push(x) },
      on(ev, fn) { this.handler = fn; return this }
    };
    if (typeof input === 'string' && input.startsWith('<div')) o.html = input;
    return o;
  }

  const ctx = {
    window: { appready: true },
    console,
    document: { createElement: () => ({}) },
    setTimeout: () => 0,
    clearTimeout: () => {},
    $: jq,
    Lampa: {
      Component: { add(name, ctor) { state.Component = ctor } },
      Scroll: function () {
        this.render = () => jq('scroll');
        this.body = () => jq('body');
        this.reset = () => {};
        this.append = (x) => state.buttons.push(x);
        this.immediate = () => {};
        this.destroy = () => {};
      },
      Player: { play: (x) => state.plays.push(x), playlist() {} },
      PlayerPlaylist: { get: () => [], set() {} },
      Search: { addSource() {} },
      Listener: { follow: (t, f) => { state.handlers[t] = f } },
      Storage: { get: () => ({}), set() {} },
      Reguest: function () { this.silent = () => {}; this.timeout = () => {}; this.clear = () => {} },
      Activity: { active: () => ({ component: 'full' }), push: (x) => state.activities.push(x), backward() {} },
      Select: { show: (x) => { select = x }, close() { if (select && select.onBack) select.onBack() } },
      Controller: { toggle() {}, add() {}, collectionSet() {}, collectionFocus() {} },
      Noty: { show() {} }
    }
  };
  state.select = () => select;
  return { ctx, state, jq };
}

function loadPlugin(file) {
  const { ctx, state, jq } = makeLampa();
  const code = fs.readFileSync(file, 'utf8');
  vm.runInNewContext(code, ctx);
  // Карточка фильма открылась — плагин должен добавить свою кнопку.
  state.handlers.full({ type: 'complite', object: { activity: { render: () => jq('card') } }, data: { movie: { title: 'Тест', id: 1 } } });
  return state;
}

// 1. Основной плагин KinoKrad: кнопка в карточке → экран kinokrad_online.
{
  const state = loadPlugin(path.join(__dirname, '..', 'plugin.js'));
  assert.equal(state.buttons.length, 1, 'KinoKrad: кнопка в карточке не создана');
  state.buttons[0].handler();
  assert.equal(state.activities[0].component, 'kinokrad_online');
  console.log('KinoKrad: кнопка и переход — OK');
}

// 2. Плагин Kinogo создаёт общее меню источников KinoKrad / Kinogo / Rutube.
{
  const state = loadPlugin(path.join(__dirname, '..', 'kinogo', 'plugin.js'));
  assert.equal(state.buttons.length, 1, 'Kinogo: кнопка в карточке не создана');
  state.buttons[0].handler();
  const select = state.select();
  assert.ok(select && select.items, 'Kinogo: меню источников не открылось');
  const titles = Array.from(select.items, (i) => i.title);
  assert.deepEqual(titles, ['KinoKrad', 'Kinogo', 'Rutube']);
  for (const item of select.items) select.onSelect(item);
  assert.deepEqual(state.activities.map((a) => a.component),
    ['kinokrad_online', 'kinogo_online', 'rutube_online']);
  console.log('Kinogo: меню из трёх источников — OK');
}
