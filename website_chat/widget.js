/* Snow White Linen Supply / The Laundry Group website chat bubble.
   One line per site:  <script src="https://chat.snowwhitelinen.com/widget.js?site=sw" async></script>   (site=tlg for TLG)
   Everything lives in a shadow root so the website's own styles can't break it (and it can't break the website). */
(function () {
  if (window.__swlChat) return;
  window.__swlChat = true;
  var CFG = __CONFIG__;
  var site = CFG.site, KEY = "swl-chat-" + CFG.site;
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
  if (!saved.at || Date.now() - saved.at > 6 * 3600 * 1000) saved = {};  // a visit after 6 quiet hours starts fresh (the server forgets it too)
  var session = saved.session || "", log = saved.log || [], busy = false;
  if (saved.site && CFG.companies[saved.site]) site = saved.site;

  var host = document.createElement("div");
  host.style.cssText = "position:fixed;right:0;bottom:0;z-index:2147483000";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML = '<style>' +
    ':host{all:initial}*{box-sizing:border-box;font-family:"Open Sans","Segoe UI",Roboto,Helvetica,Arial,sans-serif}' +
    '.bubble{position:fixed;right:max(18px,env(safe-area-inset-right,0px));bottom:max(18px,env(safe-area-inset-bottom,0px));width:60px;height:60px;border-radius:50%;border:3px solid var(--gold);background:var(--navy);color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center}' +
    '.bubble svg{width:28px;height:28px}' +
    '.panel{position:fixed;right:max(18px,env(safe-area-inset-right,0px));bottom:max(90px,calc(env(safe-area-inset-bottom,0px) + 90px));width:min(380px,calc(100vw - 24px));height:min(560px,calc(100vh - 120px));background:#fff;color:#1c2226;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.3);display:flex;flex-direction:column;overflow:hidden}' +
    '.panel[hidden]{display:none}' +
    '.head{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:3px solid var(--gold);background:#fff}' +
    '.head img{height:30px;width:auto;display:block}.head b{display:block;font-size:13px;color:var(--navy)}.head small{font-size:11.5px;color:#55626a}' +
    '.x{margin-left:auto;background:none;border:0;font-size:22px;line-height:1;color:#55626a;cursor:pointer;padding:4px 8px}' +
    '.log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}' +
    '.m{max-width:86%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.45}' +
    '.bot{background:#eef2f3;align-self:flex-start;border-bottom-left-radius:3px}.me{background:var(--navy);color:#fff;align-self:flex-end;border-bottom-right-radius:3px}' +
    '.typing{display:flex;gap:4px;align-items:center;padding:12px 14px}.typing i{width:7px;height:7px;border-radius:50%;background:#8a979e;animation:swlb 1.2s infinite}' +
    '.typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}@keyframes swlb{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}' +
    '.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}.chip{background:#fff;color:var(--navy);border:1.5px solid var(--navy);border-radius:16px;padding:6px 12px;font-size:13px;cursor:pointer;text-align:left}.chip:hover{background:var(--navy);color:#fff}' +
    '.note{align-self:center;color:#1f7a4d;font-size:12.5px;font-weight:600;background:none;padding:2px}' +
    '.hand{align-self:stretch;max-width:100%;background:#eaf0f1;color:var(--navy);border-left:3px solid var(--gold);font-size:12.5px;border-radius:6px}' +
    'form{display:flex;gap:8px;padding:10px;border-top:1px solid #e3e8ea}' +
    'textarea{flex:1;min-width:0;resize:none;height:48px;font-size:14px;padding:9px 10px;border:1px solid #d5dce0;border-radius:8px;color:#1c2226;background:#f6f8f9}' +
    '.send{background:var(--gold);color:#1c2226;border:0;border-radius:8px;padding:0 26px;min-height:48px;font-weight:700;font-size:16px;cursor:pointer}.send:disabled{opacity:.5}' +
    '.new{align-self:center;background:var(--navy);color:#fff;border:0;border-radius:6px;padding:5px 8px;font-weight:600;font-size:11px;cursor:pointer;white-space:nowrap}' +
    '.fine{font-size:10.5px;color:#7a868d;padding:0 12px 8px}' +
    'button:focus-visible,textarea:focus-visible{outline:2px solid var(--gold);outline-offset:2px}' +
    '</style>' +
    '<div class="panel" hidden role="dialog" aria-label="Chat"><div class="head"><img alt=""><div><b></b><small>Replies in seconds · our office follows up</small></div><button class="x" aria-label="Close chat">×</button></div>' +
    '<div class="log" aria-live="polite"></div><form><textarea aria-label="Your message" placeholder="Ask us anything…"></textarea><button class="send" type="submit">Send</button><button class="new" type="button" aria-label="Exit chat">Exit</button></form>' +
    '<div class="fine">Please don\'t share payment or medical details here.</div></div>' +
    '<button class="bubble" aria-label="Chat with us"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg></button>';

  var $ = function (s) { return root.querySelector(s); };
  var panel = $(".panel"), logEl = $(".log"), box = $("textarea"), sendBtn = $(".send");

  function brand() {
    var c = CFG.companies[site];
    host.style.setProperty("--navy", c.navy); host.style.setProperty("--gold", c.gold);
    root.host.style.setProperty("--navy", c.navy); root.host.style.setProperty("--gold", c.gold);
    $(".head img").src = c.logo; $(".head b").textContent = c.name;
  }
  function store() { try { localStorage.setItem(KEY, JSON.stringify({ session: session, site: site, log: log.slice(-60), at: Date.now(), open: !panel.hidden })); } catch (e) {} }
  function add(cls, text, keep) {
    var d = document.createElement("div"); d.className = "m " + cls; d.textContent = text; logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    if (keep !== false) { log.push([cls, text]); store(); }
    return d;
  }
  function render() {
    logEl.textContent = "";
    if (log.length) return log.forEach(function (x) { add(x[0], x[1], false); });
    // A fresh chat: a short hello in a couple of bubbles, then a few one-tap choices.
    CFG.companies[site].greeting.split("\n").forEach(function (line) { add("bot", line, false); });
    var chips = document.createElement("div"); chips.className = "chips";
    (CFG.companies[site].chips || []).forEach(function (label) {
      var b = document.createElement("button"); b.type = "button"; b.className = "chip"; b.textContent = label;
      b.addEventListener("click", function () { send(label); }); chips.appendChild(b);
    });
    logEl.appendChild(chips);
  }

  // Replies show after a short "typing" pause, like a fast typist: about 1 second plus a little per character,
  // never more than 6 seconds, counting the time the server already took.
  function typingTime(text) { return Math.min(6000, 900 + 18 * (text || "").length); }
  function pause(ms) { return new Promise(function (ok) { setTimeout(ok, Math.max(0, ms)); }); }
  function typing() { var d = add("bot typing", "", false); d.innerHTML = "<i></i><i></i><i></i>"; return d; }

  function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true;
    var chips = logEl.querySelector(".chips"); if (chips) chips.remove();
    if (!log.length) CFG.companies[site].greeting.split("\n").forEach(function (line) { log.push(["bot", line]); });
    add("me", text);
    var started = Date.now(), wait;
    pause(400).then(function () { if (busy) wait = typing(); });  // the dots appear a moment after sending
    fetch(CFG.api + "/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site: site, session: session, message: text, page: location.href }) })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (r.session) session = r.session;
        return pause(typingTime(r.reply) - (Date.now() - started)).then(function () {
          if (wait) wait.remove();
          if (r.reply) add("bot", r.reply);
          var chain = Promise.resolve();
          (r.events || []).forEach(function (e) {
            chain = chain.then(function () {
              if (e.type === "lead") add("note", "✓ Sent to our sales team");
              if (e.type === "request") add("note", "✓ Sent to our office");
              if (e.type === "handoff") { site = e.to; brand(); add("hand", "Connected to " + CFG.companies[site].name); }
              if (e.type === "continued" && e.reply) {
                var t = typing();
                return pause(typingTime(e.reply)).then(function () { t.remove(); add("bot", e.reply); });
              }
            });
          });
          return chain.then(function () {
            if (r.site && CFG.companies[r.site] && r.site !== site) { site = r.site; brand(); }
            store();
          });
        });
      })
      .catch(function () { if (wait) wait.remove(); add("bot", "Sorry, we couldn't connect. Please call us at (907) 222-7738.", false); })
      .then(function () { if (wait) wait.remove(); busy = false; sendBtn.disabled = false; box.focus(); });
  }

  $(".bubble").addEventListener("click", function () { panel.hidden = !panel.hidden; if (!panel.hidden) { render(); box.focus(); } if (log.length) store(); });
  $(".x").addEventListener("click", function () { panel.hidden = true; if (log.length) store(); });
  $(".new").addEventListener("click", function () {  // leave: forget this conversation and close; reopening starts fresh
    if (busy || !window.confirm("Exit this chat? The conversation will be cleared.")) return;
    session = ""; log = []; site = CFG.site;
    try { localStorage.removeItem(KEY); } catch (e) {}
    brand(); panel.hidden = true;  // the next time they open it, it starts fresh with the greeting
  });
  $("form").addEventListener("submit", function (e) { e.preventDefault(); var t = box.value; box.value = ""; send(t); });
  box.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").dispatchEvent(new Event("submit", { cancelable: true })); } });
  brand();
  // Moving to another page of the site: an open conversation reopens where it left off (minimized stays minimized).
  if (saved.open && log.length) { panel.hidden = false; render(); }
})();
