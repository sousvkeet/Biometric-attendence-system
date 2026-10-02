document.addEventListener("click", function (e) {
  const t = e.target.closest("[data-toggle-sidebar]");
  if (t) { document.body.classList.toggle("sidebar-open"); }
  const c = e.target.closest("a[data-confirm], button[data-confirm]");
  if (c && !confirm(c.getAttribute("data-confirm"))) { e.preventDefault(); e.stopPropagation(); }
});
document.addEventListener("submit", function (e) {
  const f = e.target;
  if (f.dataset.confirm && !confirm(f.dataset.confirm)) { e.preventDefault(); return; }
  if (f.method.toLowerCase() !== "post") return;
  const btn = e.submitter;
  if (btn && !("noSpin" in btn.dataset)) {
    // disable after the browser has captured the submitter's name/value
    setTimeout(function () {
      btn.disabled = true;
      btn.insertAdjacentHTML("afterbegin", '<span class="spinner-border spinner-border-sm me-1"></span>');
    }, 0);
  }
});
