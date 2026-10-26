const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function renderer() {
  let now = 0;
  const ctx = new Proxy({measureText: () => ({width: 100})}, {
    get(target, key) { return target[key] ?? (() => {}); }
  });
  const canvas = {getContext: () => ctx, getBoundingClientRect: () => ({width: 640, height: 360})};
  const sandbox = {
    performance: {now: () => now},
    window: {devicePixelRatio: 1, addEventListener() {}},
    requestAnimationFrame() {}, console,
  };
  vm.createContext(sandbox);
  const html = fs.readFileSync('templates/danmaku.html', 'utf8');
  let script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  script = script.slice(0, script.indexOf('    let engine, wsClient;'));
  script = script.replace(/\{\{.*?\}\}/g, '""');
  vm.runInContext(script + '\nglobalThis.Engine = DanmakuEngine;', sandbox);
  return {engine: new sandbox.Engine(canvas), at: n => {now = n;}};
}

test('queued fixed messages receive a full display duration when placed', () => {
  const {engine, at} = renderer();
  engine.add('first 居中', {messageId: '1'});
  const second = engine.add('second 居中', {messageId: '2'});
  assert.equal(engine.pendingDanmakus.length, 1);
  at(5200);
  engine.flushPending(5200);
  second.update(16);
  assert.equal(second.createdAt, 5200);
  assert.equal(second.alive, true);
});

test('stale queued messages are discarded without occupying tracks', () => {
  const {engine, at} = renderer();
  engine.add('first 居中');
  engine.add('stale 居中');
  at(11000);
  engine.flushPending(11000);
  assert.equal(engine.pendingDanmakus.length, 0);
  assert.equal(engine.topTracks[0], 5100);
});

test('empty group selection and screen lock reject incoming messages', () => {
  const {engine} = renderer();
  engine.setFilter(true, []);
  assert.equal(engine.add('unauthorized', {groupId: '101'}), null);
  engine.setFilter(true, ['101']);
  engine.setScreenCleared(true);
  assert.equal(engine.add('cleared', {groupId: '101'}), null);
});
