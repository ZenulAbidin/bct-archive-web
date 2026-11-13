// SPDX-License-Identifier: MIT — optional progressive enhancement; every page works without it.
(function () {
  // 1. Show dates in the reader's own time zone (the server prints UTC).
  var fmt;
  try { fmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch (e) {}
  var sfmt;
  try { sfmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); } catch (e) {}
  document.querySelectorAll('time[datetime]').forEach(function (t) {
    var d = new Date(t.getAttribute('datetime'));
    if (isNaN(d) || !fmt) return;
    t.title = t.textContent;
    t.textContent = (/UTC$/.test(t.textContent) ? fmt : sfmt).format(d);
  });
  // 2. Replace images that no longer exist with a short note instead of a broken icon.
  function gone(img) {
    var s = document.createElement('span');
    s.className = 'img-gone';
    s.textContent = 'Image no longer available';
    img.replaceWith(s);
  }
  document.querySelectorAll('.content img').forEach(function (img) {
    if (img.complete && img.naturalWidth === 0 && img.currentSrc) gone(img);
    else img.addEventListener('error', function () { gone(img); }, { once: true });
  });
  // 3. Old links such as /threads/123#456 where post 456 is on a later page: ask the server for the right page.
  var m = /^#(?:msg)?(\d+)$/.exec(location.hash), t = /^\/threads\/(\d+)/.exec(location.pathname);
  if (m && t && !document.getElementById(m[1]) && !/[?&]post=/.test(location.search)) {
    location.replace('/threads/' + t[1] + '?post=' + m[1] + '#' + m[1]);
  }
})();
