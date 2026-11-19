/* Twin-patient card: three states as tabs. Without this script all three
   states are shown one under the other. Auto-advance (unless the reader
   prefers reduced motion) stops at the first press. ?state=N opens state N
   (0-2) without auto-advance; ?static disables auto-advance. */
(function () {
  "use strict";
  var card = document.getElementById("card");
  if (!card) return;
  var tabs = Array.prototype.slice.call(card.querySelectorAll('[role="tab"]'));
  var panels = Array.prototype.slice.call(card.querySelectorAll('[role="tabpanel"]'));
  var pause = card.querySelector(".pause");
  if (!tabs.length || tabs.length !== panels.length) return;
  var i = 0, timer = null;

  function show(n, focus) {
    i = (n % tabs.length + tabs.length) % tabs.length;
    for (var k = 0; k < tabs.length; k++) {
      var on = k === i;
      tabs[k].setAttribute("aria-selected", on ? "true" : "false");
      tabs[k].tabIndex = on ? 0 : -1;
      panels[k].hidden = !on;
    }
    if (focus) tabs[i].focus();
  }
  function dwell() {
    return parseInt(panels[i].getAttribute("data-dwell"), 10) || 3000;
  }
  function stop() {
    if (timer) clearTimeout(timer);
    timer = null;
    if (pause) { pause.textContent = "Play"; pause.setAttribute("aria-pressed", "true"); }
  }
  function tick() {
    timer = setTimeout(function () { show(i + 1); tick(); }, dwell());
  }
  function start() {
    if (timer) return;
    if (pause) { pause.textContent = "Pause"; pause.setAttribute("aria-pressed", "false"); }
    tick();
  }

  card.classList.add("js");
  tabs.forEach(function (t, k) {
    t.addEventListener("click", function () { stop(); show(k); });
  });
  card.querySelector('[role="tablist"]').addEventListener("keydown", function (e) {
    var n = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (n === undefined) return;
    e.preventDefault();
    stop();
    show(n, true);
  });
  if (pause) pause.addEventListener("click", function () { if (timer) stop(); else start(); });

  var q = window.location.search;
  var m = /[?&]state=(\d)/.exec(q);
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  show(m ? parseInt(m[1], 10) : 0);
  if (m || /[?&]static/.test(q) || reduce) stop(); else start();
  window.gapcheckCard = { show: function (n) { stop(); show(n); }, stop: stop };
})();
