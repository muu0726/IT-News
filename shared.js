/**
 * Shared frontend utilities for IT Info Hub (index.html & gadget.html).
 * Exposes window.ITInfoHub.
 */
(() => {
  'use strict';

  const THEME_KEY = 'itinfohub_theme';
  const SUN_SVG = `<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>`;
  const MOON_SVG = `<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;

  const store = {
    get(key, fallback) {
      try {
        return JSON.parse(localStorage.getItem(key)) ?? fallback;
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch {
        /* storage unavailable */
      }
    },
  };

  const esc = (s) =>
    String(s ?? '').replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );

  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || '')) ? String(u) : '#');

  function createToaster(toastWrap) {
    return function showToast(message, actionLabel = null, onAction = null) {
      if (!toastWrap) return;
      const el = document.createElement('div');
      el.className = 'toast';
      const span = document.createElement('span');
      span.textContent = message;
      el.appendChild(span);
      if (actionLabel && onAction) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'toast-action';
        btn.textContent = actionLabel;
        btn.addEventListener('click', () => {
          onAction();
          el.remove();
        });
        el.appendChild(btn);
      }
      toastWrap.appendChild(el);
      setTimeout(() => {
        if (el.parentNode) el.remove();
      }, 4000);
    };
  }

  function createThemeController({ toggleBtn, onChange } = {}) {
    function updateIcon() {
      if (!toggleBtn) return;
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      toggleBtn.innerHTML = isDark ? SUN_SVG : MOON_SVG;
    }

    function toggle() {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        /* ignore */
      }
      updateIcon();
      if (typeof onChange === 'function') onChange(next);
    }

    function init() {
      let saved = null;
      try {
        saved = localStorage.getItem(THEME_KEY);
      } catch {
        /* ignore */
      }
      if (saved) {
        document.documentElement.setAttribute('data-theme', saved.replace(/"/g, ''));
      } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        document.documentElement.setAttribute('data-theme', 'dark');
      }
      updateIcon();
      toggleBtn?.addEventListener('click', toggle);
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        let curSaved = null;
        try {
          curSaved = localStorage.getItem(THEME_KEY);
        } catch {
          /* ignore */
        }
        if (!curSaved) {
          const next = e.matches ? 'dark' : 'light';
          document.documentElement.setAttribute('data-theme', next);
          updateIcon();
          if (typeof onChange === 'function') onChange(next);
        }
      });
    }

    return { init, toggle, updateIcon };
  }

  function parseSearchQuery(raw) {
    const tokens = String(raw || '').trim().split(/\s+/).filter(Boolean);
    const include = [];
    const exclude = [];
    tokens.forEach((t) => {
      if (t.startsWith('-') && t.length > 1) exclude.push(t.slice(1).toLowerCase());
      else include.push(t.toLowerCase());
    });
    return { include, exclude };
  }

  function highlightText(rawText, terms) {
    const str = String(rawText ?? '');
    if (!terms || !terms.length || !str) return esc(str);
    const safeTerms = terms
      .map((t) => String(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
      .filter(Boolean);
    if (!safeTerms.length) return esc(str);
    const re = new RegExp(`(${safeTerms.join('|')})`, 'gi');
    let out = '';
    let lastIndex = 0;
    let match;
    while ((match = re.exec(str)) !== null) {
      if (match[0].length === 0) {
        re.lastIndex += 1;
        continue;
      }
      if (match.index > lastIndex) {
        out += esc(str.slice(lastIndex, match.index));
      }
      out += `<mark class="hl">${esc(match[0])}</mark>`;
      lastIndex = re.lastIndex;
    }
    if (lastIndex < str.length) {
      out += esc(str.slice(lastIndex));
    }
    return out;
  }

  function debounce(fn, delay = 150) {
    let timer = null;
    const wrapped = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn(...args);
      }, delay);
    };
    wrapped.cancel = () => {
      clearTimeout(timer);
      timer = null;
    };
    return wrapped;
  }

  function copyToClipboard(text, successMsg, showToast) {
    if (!navigator.clipboard) {
      showToast('クリップボードへのコピーが利用できません');
      return;
    }
    navigator.clipboard
      .writeText(text)
      .then(() => showToast(successMsg))
      .catch(() => showToast('クリップボードへのコピーに失敗しました'));
  }

  function trimAndSaveSet(urlSet, storageKey, maxItems = 3000) {
    const arr = [...urlSet].slice(-maxItems);
    const trimmed = new Set(arr);
    store.set(storageKey, arr);
    return trimmed;
  }

  function bindModal({ modal, openBtn, closeBtn }) {
    if (!modal) return;
    openBtn?.addEventListener('click', () => modal.classList.add('visible'));
    closeBtn?.addEventListener('click', () => modal.classList.remove('visible'));
    modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.classList.remove('visible');
    });
  }

  function registerServiceWorker(swPath = 'sw.js') {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker
        .register(swPath)
        .catch((err) => console.log('SW registration failed:', err));
    }
  }

  window.ITInfoHub = {
    store,
    esc,
    safeUrl,
    createToaster,
    createThemeController,
    parseSearchQuery,
    highlightText,
    debounce,
    copyToClipboard,
    trimAndSaveSet,
    bindModal,
    registerServiceWorker,
  };
})();
