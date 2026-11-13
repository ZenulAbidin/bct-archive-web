// SPDX-License-Identifier: MIT — BBCode composer. Your text stays in the browser:
// the draft lives in localStorage and the preview is rendered locally. The text is never sent to the
// server; only a picture you choose to upload is, on its way to TalkImg.
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
      codes.push(c.replace(/^\n/, '').replace(/\n$/, '')); return '\u0000' + (codes.length - 1) + '\u0000';
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
      s = s.replace(/\[img[^\]]*\]([^\[\s]+)\[\/img\]/gi, function (_, u) {
        return shown[u] ? '<img src="' + shown[u] + '" alt="Uploaded image">' : '<span class="img-gone">Image: ' + u + '</span>';
      });
      s = s.replace(/\[quote(?:\s+author=([^\]]*?))?(?:\s+link=[^\]\s]*)?(?:\s+date=(\d+))?\]([\s\S]*?)\[\/quote\]/gi, function (_, a, d, c) {
        var head = a ? 'Quote from: ' + a + (d ? ' on ' + new Date(+d * 1000).toLocaleString() : '') : 'Quote';
        return '<div class="quoteheader">' + head + '</div><div class="quote">' + c + '</div>';
      });
      s = s.replace(/\[list\]([\s\S]*?)\[\/list\]/gi, function (_, c) {
        return '<ul>' + c.replace(/\[li\]([\s\S]*?)\[\/li\]/gi, '<li>$1</li>').replace(/\[\*\]([^\[\n]*)/g, '<li>$1</li>') + '</ul>';
      });
    } while (s !== prev);
    s = s.replace(/\[hr\]/gi, '<hr>');
    // Like the forum's own renderer: one line break right after a block starts or ends is not shown
    // (quote, code, list, centred text, horizontal line), and breaks between list items are dropped.
    s = s.replace(/(<div class="quote">|<div align="(?:center|left|right)">|<ul>|<pre>)\n/g, '$1')
         .replace(/(<\/div>|<\/ul>|<\/pre>|<hr>|\u0000\d+\u0000)\n/g, '$1')
         .replace(/\n*(<\/?li>|<\/ul>)\n*/g, '$1').replace(/<ul>\n+/g, '<ul>');
    s = s.replace(/\n/g, '<br>');
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
  // Pictures: button, paste or drop. The file goes to TalkImg through this site; the text never does.
  var shown = {};
  try { shown = JSON.parse(sessionStorage.getItem('uploaded') || '{}'); } catch (e) {}
  function insert(text) {
    msg.setRangeText(text, msg.selectionStart, msg.selectionEnd, 'end');
    msg.focus(); update();
  }
  function send(file) {
    if (!file || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return say('Only PNG, JPEG, GIF and WebP pictures can be uploaded.');
    if (file.size > 8 * 1024 * 1024) return say('The picture is too large (limit 8 MB).');
    say('Uploading picture…');
    fetch('/upload-image', { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream', 'X-Composer': '1' } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (r) {
        if (!r.ok || !r.j.url) return say(r.j.error || 'The upload failed.');
        shown[r.j.url] = r.j.preview;
        try { sessionStorage.setItem('uploaded', JSON.stringify(shown)); } catch (e) {}
        insert('[img]' + r.j.url + '[/img]');
        say('Picture uploaded.');
      }, function () { say('The upload failed. Check your connection and try again.'); });
  }
  var up = document.getElementById('upload'), file = document.getElementById('file');
  if (up && file) {
    up.addEventListener('click', function () { file.click(); });
    file.addEventListener('change', function () { send(file.files[0]); file.value = ''; });
    msg.addEventListener('paste', function (ev) {
      var items = (ev.clipboardData || {}).files || [];
      if (items.length && /^image\//.test(items[0].type)) { ev.preventDefault(); send(items[0]); }
    });
    msg.addEventListener('dragover', function (ev) { ev.preventDefault(); });
    msg.addEventListener('drop', function (ev) {
      var f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (f) { ev.preventDefault(); send(f); }
    });
  }
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
