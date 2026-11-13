// Site chrome: the theme toggle and the phone menu. site-head.html applies the stored theme before the first paint;
// load this at the end of <body>.
(function () {
  var KEY = 'octoprox_docs_theme', root = document.documentElement;

  // Light by default; dark is data-theme="dark" on <html>, stored under the docs' key
  function syncToggles() {
    var dark = root.getAttribute('data-theme') === 'dark';
    document.querySelectorAll('.theme-toggle').forEach(function (b) { b.setAttribute('aria-pressed', dark ? 'true' : 'false'); });
  }
  function toggleTheme() {
    var dark = root.getAttribute('data-theme') !== 'dark';
    if (dark) root.setAttribute('data-theme', 'dark'); else root.removeAttribute('data-theme');
    try { localStorage.setItem(KEY, dark ? 'dark' : 'light'); } catch (e) {}
    syncToggles();
  }
  syncToggles();

  // The phone menu is the panel under the header, open while its button says so; while open, the page behind does not
  // scroll. A header whose button opens something else (the docs drawer) leaves that to its own script.
  var button = document.querySelector('.site-header .menu-btn');
  var menu = button && document.getElementById(button.getAttribute('aria-controls'));
  if (menu && !menu.classList.contains('mobile-menu')) menu = null;
  function isOpen() { return !!menu && button.getAttribute('aria-expanded') === 'true'; }
  function hold(e) { if (!menu.contains(e.target)) e.preventDefault(); }
  function setMenu(open) {
    menu.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) document.addEventListener('touchmove', hold, { passive: false });
    else document.removeEventListener('touchmove', hold);
  }
  // Turning a tablet past 900 px closes the menu, so it never holds the wider layout
  if (menu) matchMedia('(min-width: 900px)').addEventListener('change', function () { setMenu(false); });

  document.addEventListener('click', function (e) {
    var t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    // A tap outside the header while the menu is open only closes it
    if (isOpen() && !button.closest('.site-header').contains(t)) {
      e.preventDefault();
      e.stopPropagation();
      setMenu(false);
      return;
    }
    if (t.closest('.theme-toggle')) { toggleTheme(); return; }
    if (menu && button.contains(t)) { setMenu(!isOpen()); return; }
    if (isOpen() && t.closest('a')) setMenu(false);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !isOpen()) return;
    if (menu.contains(document.activeElement)) button.focus();
    setMenu(false);
  });

  // Smooth in-page scrolling (site.css) starts once the page has loaded, after the browser has jumped to any #target
  function smooth() { requestAnimationFrame(function () { root.classList.add('smooth-scroll'); }); }
  if (document.readyState === 'complete') smooth(); else addEventListener('load', smooth);
})();
