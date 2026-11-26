import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function loadShared({ matchesDark = false } = {}) {
  const storage = new Map();
  let mediaChangeHandler = null;
  const listeners = new Map();
  const toggleBtn = {
    innerHTML: '',
    addEventListener(type, fn) {
      listeners.set(type, fn);
    },
  };
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
    navigator: {},
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
    clickToggle: () => listeners.get('click')?.(),
  };
}

test('esc and safeUrl sanitize untrusted inputs', () => {
  const { api } = loadShared();
  assert.equal(api.esc('<script>"&\x27'), '&lt;script&gt;&quot;&amp;&#39;');
  assert.equal(api.safeUrl('javascript:alert(1)'), '#');
  assert.equal(api.safeUrl('https://example.com/a?b=1'), 'https://example.com/a?b=1');
});

const clone = (v) => JSON.parse(JSON.stringify(v));

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

test('index.html and gadget.html link shared assets and have valid inline JS', () => {
  for (const fn of ['index.html', 'gadget.html']) {
    const html = fs.readFileSync(fn, 'utf8');
    assert.ok(html.includes('<link rel="stylesheet" href="shared.css">'), `${fn} missing shared.css`);
    assert.ok(html.includes('<script src="shared.js"></script>'), `${fn} missing shared.js`);
    const match = html.match(/<script>\s*\(\(\)\s*=>\s*\{[\s\S]*?<\/script>/);
    assert.ok(match, `${fn} missing inline script`);
    const code = match[0].slice('<script>'.length, -'</script>'.length);
    new vm.Script(code, { filename: `${fn}:inline.js` });
  }
});
