/*
 * Nuage Libre: layout switcher (split-screen / chat only / files only).
 *
 * The layout itself is pure CSS (css/app.css, section 3): the workspace is a
 * two-column grid whose column widths follow the data-view attribute of
 * <html>:
 *
 *     data-view="split"  ->  files 1fr | chat 1fr   (50 / 50)
 *     data-view="chat"   ->  files 0fr | chat 1fr   (chat in full screen)
 *     data-view="files"  ->  files 1fr | chat 0fr   (files in full screen)
 *
 * This script only:
 *   1. wires the three radio buttons (mouse + keyboard, WAI-ARIA radio group
 *      pattern, exactly like the theme switcher),
 *   2. remembers the choice in localStorage,
 *   3. makes the hidden panel `inert`: it can no longer receive focus or
 *      clicks and is removed from the accessibility tree. Without this, a
 *      keyboard user could Tab into a panel of zero width, and a screen
 *      reader would read content nobody can see.
 *   4. falls back to "chat" on small screens (< 900 px), where two panels
 *      side by side would each be too narrow to use. The user's preference
 *      is kept: rotating a tablet or widening the window restores the split.
 *
 * The initial value is applied earlier by the inline script in the <head>
 * of index.html (no flash of the wrong layout). Other scripts can react to
 * changes by listening to the "viewchange" event on document.
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'nuage-libre:view';
  const VIEWS = ['files', 'split', 'chat'];

  const root = document.documentElement;
  const switcher = document.getElementById('view-switcher');
  const options = [...switcher.querySelectorAll('[data-view-value]')];
  const panels = {
    files: document.getElementById('files-panel'),
    chat: document.getElementById('chat-panel'),
  };
  // Same breakpoint as the CSS that hides the "split" button.
  const narrowScreen = window.matchMedia('(max-width: 899px)');

  function readPreference() {
    let view = null;
    try { view = localStorage.getItem(STORAGE_KEY); } catch { /* storage blocked */ }
    return VIEWS.includes(view) ? view : 'split';
  }

  function savePreference(view) {
    try { localStorage.setItem(STORAGE_KEY, view); } catch { /* not remembered, still applied */ }
  }

  // What the user chose; what is shown may differ on a small screen.
  let preferred = readPreference();

  const effectiveView = (view) => (view === 'split' && narrowScreen.matches ? 'chat' : view);

  /** The radio buttons the user can currently reach ("split" is hidden on small screens). */
  const visibleOptions = () => options.filter((o) => !(narrowScreen.matches && o.dataset.viewValue === 'split'));

  function setPanelShown(panel, shown) {
    panel.inert = !shown;
    panel.classList.toggle('is-collapsed', !shown);
  }

  /** Show `view` (or its small-screen fallback) and sync the switcher. */
  function applyView(view) {
    const shown = effectiveView(view);
    const changed = root.dataset.view !== shown;
    root.dataset.view = shown;
    setPanelShown(panels.files, shown !== 'chat');
    setPanelShown(panels.chat, shown !== 'files');

    for (const option of options) {
      const checked = option.dataset.viewValue === shown;
      option.setAttribute('aria-checked', String(checked));
      // Roving tabindex: only the selected radio is reachable with Tab;
      // the arrow keys move between the options.
      option.tabIndex = checked ? 0 : -1;
    }
    if (changed) document.dispatchEvent(new CustomEvent('viewchange', { detail: { view: shown } }));
  }

  function chooseView(view) {
    preferred = view;
    savePreference(view);
    applyView(view);
  }

  // ------------------------------------------------------------- events
  for (const option of options) {
    option.addEventListener('click', () => chooseView(option.dataset.viewValue));
  }

  // Arrow keys move the selection, like native radio buttons.
  switcher.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const reachable = visibleOptions();
    const current = reachable.findIndex((o) => o.dataset.viewValue === root.dataset.view);
    const next = reachable[(current + step + reachable.length) % reachable.length];
    next.focus();
    chooseView(next.dataset.viewValue);
  });

  // Crossing the breakpoint (window resized, tablet rotated): re-apply the
  // preference, so "split" comes back as soon as there is room for it.
  narrowScreen.addEventListener('change', () => applyView(preferred));

  applyView(preferred);
})();
