// SPDX-License-Identifier: MIT — runs before first paint: light by default, dark only if the reader chose it.
(function () {
  var t = 'light';
  try { if (localStorage.getItem('theme') === 'dark') t = 'dark'; } catch (e) {}
  var m = /[?&]theme=(dark|light)\b/.exec(location.search);  // lets a link ask for a theme
  if (m) { t = m[1]; try { localStorage.setItem('theme', t); } catch (e) {} }
  document.documentElement.setAttribute('data-theme', t);
})();
