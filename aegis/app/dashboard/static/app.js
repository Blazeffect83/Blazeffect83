// Minimal progressive enhancement (served from same origin; CSP forbids inline script).
(function () {
  var secs = parseInt(document.body.getAttribute("data-refresh") || "0", 10);
  if (secs > 0) {
    var timer = setInterval(function () {
      var a = document.activeElement;
      // Never refresh while the user is typing in a form.
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) return;
      if (document.querySelector("details[open].keep")) return;
      if (!document.hidden) location.reload();
    }, secs * 1000);
    window.addEventListener("beforeunload", function () { clearInterval(timer); });
  }
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });
})();
