/* Alert card: three examples as tabs. Without this script all three are shown
   one under the other. Nothing moves on its own. ?tab=N (0-2) opens tab N. */
(function () {
  "use strict";
  var card = document.getElementById("card");
  if (!card) return;
  var tabs = Array.prototype.slice.call(card.querySelectorAll('[role="tab"]'));
  var panels = Array.prototype.slice.call(card.querySelectorAll('[role="tabpanel"]'));
  if (!tabs.length || tabs.length !== panels.length) return;
  var i = 0;

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

  card.classList.add("js");
  panels.forEach(function (p) { p.tabIndex = 0; });
  tabs.forEach(function (t, k) {
    t.addEventListener("click", function () { show(k); });
  });
  card.querySelector('[role="tablist"]').addEventListener("keydown", function (e) {
    var n = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (n === undefined) return;
    e.preventDefault();
    show(n, true);
  });
  var m = /[?&]tab=(\d)/.exec(window.location.search);
  show(m ? parseInt(m[1], 10) : 0);
  window.gapcheckCard = { show: function (n) { show(n); } };
})();
