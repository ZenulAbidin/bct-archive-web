// SPDX-License-Identifier: MIT — optional progressive enhancement; every page works without it.
(function () {
  // 1. Dates in the reader's own time zone (the server prints UTC).
  var fmt, sfmt;
  try {
    fmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'long', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    sfmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch (e) {}
  document.querySelectorAll('time[datetime]').forEach(function (t) {
    var d = new Date(t.getAttribute('datetime'));
    if (isNaN(d) || !fmt) return;
    t.title = t.textContent;
    t.textContent = (/UTC$/.test(t.textContent) ? fmt : sfmt).format(d);
  });
  // 2. Images that cannot be loaded become a short note instead of a broken icon.
  function gone(img) {
    var s = document.createElement('span');
    s.className = 'img-gone';
    s.textContent = 'Image no longer available';
    img.replaceWith(s);
  }
  document.querySelectorAll('.content img, .sig img').forEach(function (img) {
    if (img.complete && img.naturalWidth === 0 && img.currentSrc) gone(img);
    else img.addEventListener('error', function () { gone(img); }, { once: true });
  });
  // 3. Old links such as /threads/123#456 where post 456 is on a later page.
  var m = /^#(?:msg)?(\d+)$/.exec(location.hash), t = /^\/threads\/(\d+)/.exec(location.pathname);
  if (m && t && !document.getElementById(m[1]) && !/[?&](post|page|start)=/.test(location.search)) {
    location.replace('/threads/' + t[1] + '?post=' + m[1] + '#' + m[1]);
  }
  // 4. Actions that happen on bitcointalk.org open there in one reusable popup window.
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest && ev.target.closest('a.pop');
    if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button) return;
    var w = window.open(a.href, 'bitcointalk', 'popup=yes,width=1000,height=760,noopener=no');
    if (w) { ev.preventDefault(); try { w.opener = null; w.focus(); } catch (e) {} }
  });
  // 5. Theme switch (light is the default).
  var b = document.getElementById('theme');
  if (b) {
    var root = document.documentElement;
    var label = function () { b.textContent = root.getAttribute('data-theme') === 'dark' ? 'Light theme' : 'Dark theme'; };
    b.hidden = false; label();
    b.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
      label();
    });
  }
})();
