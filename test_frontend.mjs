import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function makeClassList() {
  const classes = new Set();
  return {
    add: (...c) => c.forEach((x) => classes.add(x)),
    remove: (...c) => c.forEach((x) => classes.delete(x)),
    toggle: (c, force) => {
      if (force === undefined) {
        if (classes.has(c)) { classes.delete(c); return false; }
        classes.add(c);
        return true;
      }
      if (force) classes.add(c);
      else classes.delete(c);
      return force;
    },
    contains: (c) => classes.has(c),
  };
}

function makeElement(tag = 'div') {
  const listeners = new Map();
  const el = {
    tagName: tag.toUpperCase(),
    className: '',
    textContent: '',
    innerHTML: '',
    type: '',
    children: [],
    parentNode: null,
    classList: makeClassList(),
    appendChild(child) {
      child.parentNode = el;
      el.children.push(child);
      return child;
    },
    remove() {
      if (!el.parentNode) return;
      el.parentNode.children = el.parentNode.children.filter((c) => c !== el);
      el.parentNode = null;
    },
    addEventListener(type, fn) {
      listeners.set(type, fn);
    },
    trigger(type, event = {}) {
      listeners.get(type)?.({ target: el, ...event });
    },
  };
  return el;
}

function loadShared({ matchesDark = false, clipboard = null } = {}) {
  const storage = new Map();
  let mediaChangeHandler = null;
  const toggleBtn = makeElement('button');
  const docAttrs = {};
  const sandbox = {
    window: {
      matchMedia: () => ({
        matches: matchesDark,
        addEventListener(type, fn) {
          if (type === 'change') mediaChangeHandler = fn;
        },
      }),
    },
    document: {
      createElement: (tag) => makeElement(tag),
      documentElement: {
        setAttribute(k, v) {
          docAttrs[k] = v;
        },
        getAttribute(k) {
          return docAttrs[k] ?? null;
        },
      },
    },
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
    },
    navigator: clipboard ? { clipboard } : {},
    console,
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(fs.readFileSync('shared.js', 'utf8'), sandbox, { filename: 'shared.js' });
  return {
    api: sandbox.window.ITInfoHub,
    docAttrs,
    toggleBtn,
    triggerMediaChange: (matches) => mediaChangeHandler?.({ matches }),
    clickToggle: () => toggleBtn.trigger('click'),
  };
}

const clone = (v) => JSON.parse(JSON.stringify(v));

test('esc and safeUrl sanitize untrusted inputs', () => {
  const { api } = loadShared();
  assert.equal(api.esc('<script>"&\x27'), '&lt;script&gt;&quot;&amp;&#39;');
  assert.equal(api.safeUrl('javascript:alert(1)'), '#');
  assert.equal(api.safeUrl('https://example.com/a?b=1'), 'https://example.com/a?b=1');
});

test('parseSearchQuery and highlightText handle include/exclude and HTML escaping', () => {
  const { api } = loadShared();
  assert.deepEqual(clone(api.parseSearchQuery('AI -spam  LLM')), {
    include: ['ai', 'llm'],
    exclude: ['spam'],
  });
  assert.equal(
    api.highlightText('<AI> tool', ['ai']),
    '&lt;<mark class="hl">AI</mark>&gt; tool'
  );
});

test('store and trimAndSaveSet persist and cap history', () => {
  const { api } = loadShared();
  api.store.set('k', { a: 1 });
  assert.deepEqual(clone(api.store.get('k', null)), { a: 1 });

  const trimmed = api.trimAndSaveSet(new Set(['u1', 'u2', 'u3']), 'reads', 2);
  assert.deepEqual([...trimmed], ['u2', 'u3']);
  assert.deepEqual(clone(api.store.get('reads', [])), ['u2', 'u3']);
});

test('debounce triggers after delay and supports cancel()', async () => {
  const { api } = loadShared();
  let calls = 0;
  const d = api.debounce(() => {
    calls += 1;
  }, 20);
  d();
  d.cancel();
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(calls, 0);

  d();
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(calls, 1);
});

test('createThemeController handles toggle and OS theme change callback', () => {
  const { api, docAttrs, toggleBtn, triggerMediaChange, clickToggle } = loadShared({ matchesDark: false });
  const changes = [];
  const theme = api.createThemeController({
    toggleBtn,
    onChange: (next) => changes.push(next),
  });
  theme.init();
  assert.equal(docAttrs['data-theme'] ?? null, null);

  triggerMediaChange(true);
  assert.equal(docAttrs['data-theme'], 'dark');
  assert.deepEqual(changes, ['dark']);

  clickToggle();
  assert.equal(docAttrs['data-theme'], 'light');
  assert.deepEqual(changes, ['dark', 'light']);
});

test('createToaster renders message and handles action button click', () => {
  const { api } = loadShared();
  const wrap = makeElement('div');
  const showToast = api.createToaster(wrap);
  let undone = false;
  showToast('2件を既読にしました', '元に戻す', () => {
    undone = true;
  });
  assert.equal(wrap.children.length, 1);
  const toast = wrap.children[0];
  assert.equal(toast.children[0].textContent, '2件を既読にしました');
  toast.children[1].trigger('click');
  assert.equal(undone, true);
  assert.equal(wrap.children.length, 0);
});

test('bindModal opens, closes on button, and closes only on backdrop click', () => {
  const { api } = loadShared();
  const modal = makeElement('div');
  const openBtn = makeElement('button');
  const closeBtn = makeElement('button');
  const inner = makeElement('div');
  api.bindModal({ modal, openBtn, closeBtn });

  openBtn.trigger('click');
  assert.equal(modal.classList.contains('visible'), true);

  // 内側要素クリックでは閉じない
  modal.trigger('click', { target: inner });
  assert.equal(modal.classList.contains('visible'), true);

  // 背景クリックで閉じる
  modal.trigger('click', { target: modal });
  assert.equal(modal.classList.contains('visible'), false);

  openBtn.trigger('click');
  closeBtn.trigger('click');
  assert.equal(modal.classList.contains('visible'), false);
});

test('copyToClipboard handles success, rejection, and missing clipboard API', async () => {
  const msgs = [];
  const noClip = loadShared();
  noClip.api.copyToClipboard('x', 'OK', (m) => msgs.push(m));
  assert.deepEqual(msgs, ['クリップボードへのコピーが利用できません']);

  const okMsgs = [];
  const okClip = loadShared({ clipboard: { writeText: async () => {} } });
  okClip.api.copyToClipboard('x', 'Copied!', (m) => okMsgs.push(m));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(okMsgs, ['Copied!']);

  const errMsgs = [];
  const errClip = loadShared({ clipboard: { writeText: async () => { throw new Error('denied'); } } });
  errClip.api.copyToClipboard('x', 'Copied!', (m) => errMsgs.push(m));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(errMsgs, ['クリップボードへのコピーに失敗しました']);
});

test('sw.js compiles, precaches shared assets, and guards same-origin Network First', () => {
  const swSource = fs.readFileSync('sw.js', 'utf8');
  new vm.Script(swSource, { filename: 'sw.js' });
  for (const asset of ['./index.html', './gadget.html', './shared.css', './shared.js', './manifest.json']) {
    assert.ok(swSource.includes(`'${asset}'`), `sw.js missing precache asset ${asset}`);
  }
  assert.ok(swSource.includes('url.origin === self.location.origin'), 'sw.js missing same-origin guard');
});

test('index.html and gadget.html link shared assets, have valid inline JS, and have unique static IDs', () => {
  for (const fn of ['index.html', 'gadget.html']) {
    const html = fs.readFileSync(fn, 'utf8');
    assert.ok(html.includes('<link rel="stylesheet" href="shared.css">'), `${fn} missing shared.css`);
    assert.ok(html.includes('<script src="shared.js"></script>'), `${fn} missing shared.js`);

    const staticMarkup = html.split('<script')[0];
    const ids = [...staticMarkup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
    const dups = ids.filter((id, i) => ids.indexOf(id) !== i);
    assert.deepEqual(dups, [], `${fn} has duplicate static IDs: ${dups}`);

    const match = html.match(/<script>\s*\(\(\)\s*=>\s*\{[\s\S]*?<\/script>/);
    assert.ok(match, `${fn} missing inline script`);
    const code = match[0].slice('<script>'.length, -'</script>'.length);
    new vm.Script(code, { filename: `${fn}:inline.js` });
  }
  const indexHtml = fs.readFileSync('index.html', 'utf8');
  assert.ok(indexHtml.includes('name="memo"'), 'index.html memo input missing name attribute');
});
