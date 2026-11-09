/*
 * Nuage Libre: theme switcher.
 *
 * The three themes are pure CSS (css/themes.css): a theme is selected by
 * the data-theme attribute on <html>. This script only:
 *   1. wires the three radio buttons of the switcher (mouse + keyboard),
 *   2. remembers the choice in localStorage,
 *   3. plays a "circular reveal" when switching: the new theme grows as a
 *      circle from the clicked button, like dawn spreading over the sky.
 *
 * The initial theme is applied earlier by a tiny inline script in the
 * <head> of index.html (to avoid a flash of the wrong theme). This file
 * reads that value and keeps it in sync.
 *
 * Other scripts can react to changes by listening to the "themechange"
 * event on document (js/sky.js starts/stops the star animation with it).
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'nuage-libre:theme';
  const THEMES = ['day', 'night', 'pro'];

  // Colour of the mobile browser's address bar for each theme.
  const THEME_COLORS = { day: '#eef8ff', night: '#0f172a', pro: '#f4f5f7' };

  const root = document.documentElement;
  const switcher = document.getElementById('theme-switcher');
  const options = [...switcher.querySelectorAll('[data-theme-value]')];
  const metaThemeColor = document.querySelector('meta[name="theme-color"]');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const currentTheme = () => (THEMES.includes(root.dataset.theme) ? root.dataset.theme : 'day');

  /** Update the switcher's state (ARIA + sliding indicator) to match `theme`. */
  function syncSwitcher(theme) {
    const index = THEMES.indexOf(theme);
    switcher.style.setProperty('--theme-index', String(index));
    for (const option of options) {
      const checked = option.dataset.themeValue === theme;
      option.setAttribute('aria-checked', String(checked));
      // "Roving tabindex": only the selected radio is reachable with Tab;
      // the arrow keys move between the options (WAI-ARIA radio pattern).
      option.tabIndex = checked ? 0 : -1;
    }
    if (metaThemeColor) metaThemeColor.content = THEME_COLORS[theme];
  }

  /** Apply the theme instantly (no animation). */
  function applyTheme(theme) {
    root.dataset.theme = theme;
    syncSwitcher(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Storage blocked (private mode...): the theme still applies, it is
      // just not remembered for the next visit.
    }
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  }

  /**
   * Switch theme with a circular reveal centred on (x, y).
   *
   * The View Transitions API takes a screenshot of the page, lets us change
   * the DOM, then animates between the "old" and the "new" screenshots. We
   * animate the new one with a growing clip-path circle. Browsers without
   * the API, and users who prefer reduced motion, get an instant switch
   * (the CSS transitions on colours still soften it).
   */
  function switchTheme(theme, x, y) {
    if (theme === currentTheme()) return;

    if (!document.startViewTransition || reducedMotion.matches) {
      applyTheme(theme);
      return;
    }

    // Radius needed to cover the whole screen from the click point: the
    // distance to the farthest corner (Pythagoras).
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));

    const transition = document.startViewTransition(() => applyTheme(theme));
    transition.ready
      .then(() => {
        root.animate(
          { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
          { duration: 750, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', pseudoElement: '::view-transition-new(root)' },
        );
      })
      .catch(() => {
        // The transition can be skipped (e.g. tab hidden): the theme is
        // already applied by then, nothing else to do.
      });
  }

  // ------------------------------------------------------------- events
  for (const option of options) {
    option.addEventListener('click', (event) => {
      const rect = option.getBoundingClientRect();
      // Keyboard "clicks" have no pointer coordinates: use the button centre.
      const x = event.clientX || rect.left + rect.width / 2;
      const y = event.clientY || rect.top + rect.height / 2;
      switchTheme(option.dataset.themeValue, x, y);
    });
  }

  // Arrow keys move the selection, like native radio buttons.
  switcher.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = options[(THEMES.indexOf(currentTheme()) + step + THEMES.length) % THEMES.length];
    next.focus();
    next.click();
  });

  syncSwitcher(currentTheme());
})();
