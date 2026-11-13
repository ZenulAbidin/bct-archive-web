// SPDX-License-Identifier: MIT — BBCode composer. Everything here stays in the browser:
// the draft lives in localStorage and the preview is rendered locally. Nothing is sent to the server.
(function () {
  var box = document.getElementById('composer'); if (!box) return;
  var msg = document.getElementById('msg'), subject = document.getElementById('subject');
  var preview = document.getElementById('preview'), status = document.getElementById('status');
  var key = 'draft:' + box.dataset.key, target = box.dataset.target;
  var prefill = document.getElementById('prefill').value;

  function load() {
    var d = {};
    try { d = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) {}
    msg.value = d.msg || '';
    if (subject) subject.value = d.subject || '';
    if (prefill && msg.value.indexOf(prefill.trim()) === -1) msg.value += (msg.value ? '\n' : '') + prefill;
  }
  function save() {
    try { localStorage.setItem(key, JSON.stringify({ msg: msg.value, subject: subject ? subject.value : '' })); } catch (e) {}
  }
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function safeUrl(u) { return /^(https?:\/\/|\/)[^\s"<>]*$/i.test(u) ? u : '#'; }
  // BBCode -> HTML for the preview, following how Bitcointalk (SMF) renders the common tags.
  function render(src) {
    var codes = [];
    var s = esc(src).replace(/\[code\]([\s\S]*?)\[\/code\]/gi, function (_, c) {
      codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000';
    });
    var simple = { b: 'b', i: 'i', u: 'u', s: 'del', tt: 'tt', sub: 'sub', sup: 'sup', pre: 'pre' };
    var prev;
    do {
      prev = s;
      s = s.replace(/\[(b|i|u|s|tt|sub|sup|pre)\]([\s\S]*?)\[\/\1\]/gi, function (_, t, c) { t = simple[t.toLowerCase()]; return '<' + t + '>' + c + '</' + t + '>'; });
      s = s.replace(/\[(center|left|right)\]([\s\S]*?)\[\/\1\]/gi, '<div align="$1">$2</div>');
      s = s.replace(/\[size=(\d{1,2})(pt|px)?\]([\s\S]*?)\[\/size\]/gi, function (_, n, unit, c) {
        var pt = Math.min(Math.max(+n * (unit === 'px' ? 0.75 : 1), 1), 72);
        return '<span style="font-size: calc(var(--pt) * ' + pt + '); line-height: 1.3em">' + c + '</span>';
      });
      s = s.replace(/\[color=(#[0-9a-f]{3,8}|[a-z]{3,20})\]([\s\S]*?)\[\/color\]/gi, '<span style="color: $1">$2</span>');
      s = s.replace(/\[url=([^\]\s]+)\]([\s\S]*?)\[\/url\]/gi, function (_, u, c) { return '<a href="' + safeUrl(u) + '" rel="noopener nofollow" target="_blank">' + c + '</a>'; });
      s = s.replace(/\[url\]([^\[\s]+)\[\/url\]/gi, function (_, u) { return '<a href="' + safeUrl(u) + '" rel="noopener nofollow" target="_blank">' + u + '</a>'; });
      s = s.replace(/\[img[^\]]*\]([^\[\s]+)\[\/img\]/gi, '<span class="img-gone">Image: $1</span>');
      s = s.replace(/\[quote(?:\s+author=([^\]]*?))?(?:\s+link=[^\]\s]*)?(?:\s+date=(\d+))?\]([\s\S]*?)\[\/quote\]/gi, function (_, a, d, c) {
        var head = a ? 'Quote from: ' + a + (d ? ' on ' + new Date(+d * 1000).toLocaleString() : '') : 'Quote';
        return '<div class="quoteheader">' + head + '</div><div class="quote">' + c.replace(/^\n+|\n+$/g, '') + '</div>';
      });
      s = s.replace(/\[list\]([\s\S]*?)\[\/list\]/gi, function (_, c) {
        return '<ul>' + c.replace(/\[li\]([\s\S]*?)\[\/li\]/gi, '<li>$1</li>').replace(/\[\*\]([^\[\n]*)/g, '<li>$1</li>') + '</ul>';
      });
    } while (s !== prev);
    s = s.replace(/\[hr\]/gi, '<hr>').replace(/\n/g, '<br>');
    return s.replace(/\u0000(\d+)\u0000/g, function (_, i) { return '<div class="codeheader">Code:</div><div class="code">' + codes[+i] + '</div>'; });
  }
  function update() { preview.innerHTML = render(msg.value); save(); }
  function wrap(tag) {
    var a = msg.selectionStart, b = msg.selectionEnd, sel = msg.value.slice(a, b), open = '[' + tag + ']', close = '[/' + tag + ']';
    if (tag === 'url' && sel && !/^https?:\/\//i.test(sel)) open = '[url=https://]';
    if (tag === 'size') open = '[size=12pt]';
    if (tag === 'color') open = '[color=red]';
    if (tag === 'list') { open = '[list]\n[li]'; close = '[/li]\n[/list]'; }
    if (tag === 'hr') { open = '[hr]'; close = ''; }
    msg.setRangeText(open + sel + close, a, b, 'end');
    if (!sel && close) msg.selectionStart = msg.selectionEnd = a + open.length;
    msg.focus(); update();
  }
  function say(t) { status.textContent = t; }
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (ok, fail) {
      msg.focus(); msg.select();
      try { document.execCommand('copy') ? ok() : fail(); } catch (e) { fail(e); }
    });
  }
  box.querySelectorAll('[data-tag]').forEach(function (b) { b.addEventListener('click', function () { wrap(b.dataset.tag); }); });
  box.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      copy(document.getElementById(b.dataset.copy).value).then(function () { say('Copied.'); }, function () { say('Could not copy; select the text and copy it yourself.'); });
    });
  });
  document.getElementById('go').addEventListener('click', function () {
    var w = window.open(target, 'bitcointalk', 'popup=yes,width=1000,height=760');
    copy(msg.value).then(function () {
      say(w ? 'Copied. Paste your message in the Bitcointalk window and press Post.' : 'Copied. Your browser blocked the window; open Bitcointalk and paste.');
    }, function () { say('Could not copy automatically; select the text, copy it, then paste it on Bitcointalk.'); });
  });
  document.getElementById('clear').addEventListener('click', function () {
    msg.value = ''; if (subject) subject.value = '';
    try { localStorage.removeItem(key); } catch (e) {}
    update(); say('Draft cleared.');
  });
  msg.addEventListener('input', update);
  if (subject) subject.addEventListener('input', save);
  load(); update();
})();
