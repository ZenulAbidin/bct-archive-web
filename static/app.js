// SPDX-License-Identifier: MIT — optional progressive enhancement; every page works without it.
(function () {
  // 1. Dates in the reader's own time zone (the server prints UTC).
  var fmt, sfmt, tfmt, today = new Date().toDateString();
  try {
    fmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'long', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    sfmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    tfmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch (e) {}
  document.querySelectorAll('time[datetime]').forEach(function (t) {
    var d = new Date(t.getAttribute('datetime'));
    if (isNaN(d) || !fmt) return;
    t.title = t.textContent;
    if (d.toDateString() === today && tfmt) {  // like the forum itself: "Today at 06:35 AM"
      var b = document.createElement('b');
      b.textContent = 'Today';
      t.textContent = '';
      t.append(b, ' at ' + tfmt.format(d));
      return;
    }
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
  // 7. Tell the server this topic page is on a reader's screen, so it is re-checked for edits first.
  //    Only the topic and page number are sent; nothing is stored about the reader.
  var tp = /^\/threads\/(\d+)/.exec(location.pathname);
  if (tp && document.querySelector('table.posts') && document.visibilityState !== 'hidden') {
    var pg = /[?&]page=(\d+)/.exec(location.search);
    try { fetch('/seen/' + tp[1] + '?p=' + (pg ? pg[1] : 1), { cache: 'no-store', keepalive: true }); } catch (e) {}
  }
  // 6. Merit sender lists: remember whether the reader keeps them folded.
  var folded = false;
  try { folded = localStorage.getItem('merit-folded') === '1'; } catch (e) {}
  document.querySelectorAll('details.merited').forEach(function (d) {
    if (folded) d.open = false;
    d.addEventListener('toggle', function () { try { localStorage.setItem('merit-folded', d.open ? '0' : '1'); } catch (e) {} });
  });
  // 8. Date fields in the search form: a Clear button, shown only while a date is set.
  document.querySelectorAll('button[data-clear]').forEach(function (c) {
    var f = document.getElementById(c.getAttribute('data-clear'));
    if (!f) return;
    var show = function () { c.hidden = !f.value; };
    f.addEventListener('input', show); f.addEventListener('change', show);
    c.addEventListener('click', function () { f.value = ''; show(); });
    show();
  });
  // 9. Phones: a signature wider than the screen (banner tables, block art, wide pictures) is scaled down to fit.
  var narrow = window.matchMedia('(max-width: 720px)');
  function fitSigs() {
    document.querySelectorAll('.sig').forEach(function (s) {
      var i = s.firstElementChild;
      if (!i || !i.classList.contains('sigin')) return;
      i.style.transform = i.style.width = s.style.height = '';
      s.classList.remove('fit', 'min');
      if (!narrow.matches) return;
      var room = s.clientWidth, need = i.scrollWidth;
      if (!room) return;
      var cs = getComputedStyle(s), k;
      var edge = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
      var cap = parseFloat(cs.maxHeight);   // the usual limit on a signature's height, kept in proportion
      // Banners (tables, block art, pictures) get squeezed by a narrow column: their cells stack and most of the
      // banner ends up below the height limit. Those are laid out at their natural width instead, then scaled.
      var banner = i.querySelector('table, pre, tt, img') && cap && i.offsetHeight > cap - edge + 1;
      if (need <= room + 1 && !banner) return;
      if (banner) {
        i.style.width = 'max-content';
        need = Math.max(room, Math.min(i.offsetWidth, 900));
      }
      for (var n = 0; n < 6; n++) {
        i.style.width = need + 'px';
        if (i.scrollWidth <= need + 1) break;
        need = i.scrollWidth;
      }
      if (need <= room + 1) { i.style.width = ''; return; }
      k = Math.max(room / need, 0.3);
      i.style.transform = 'scale(' + k + ')';
      var h = i.offsetHeight;
      if (cap) h = Math.min(h, cap - edge);
      s.style.height = Math.ceil(h * k + edge) + 'px';
      s.classList.add('fit');
      if (room / need < 0.3) s.classList.add('min');
    });
  }
  if (document.querySelector('.sig')) {
    fitSigs();
    window.addEventListener('load', fitSigs);
    window.addEventListener('resize', fitSigs);
    document.querySelectorAll('.sig img').forEach(function (img) { if (!img.complete) img.addEventListener('load', fitSigs, { once: true }); });
  }
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
