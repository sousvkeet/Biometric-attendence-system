// Confirmation dialog for every link, button or form with data-confirm="message" (replaces the browser's
// confirm() popup). Optional attributes: data-confirm-title, data-confirm-ok (button text) and
// data-confirm-variant ("danger" / "primary"). By default the confirm button repeats the text of the button
// that was clicked and is red when that button is a "danger" button.
function confirmOptions(el, button) {
  const label = button ? (button.textContent.trim() || button.title || "") : "";
  return {
    title: el.dataset.confirmTitle || "Please confirm",
    message: el.dataset.confirm,
    ok: el.dataset.confirmOk || label || "OK",
    danger: el.dataset.confirmVariant ? el.dataset.confirmVariant === "danger"
      : !!(button && /\bbtn-(outline-)?danger\b/.test(button.className)),
  };
}

function showConfirm(opts) {
  return new Promise(function (resolve) {
    const previous = document.activeElement;
    const wrap = document.createElement("div");
    wrap.className = "confirm-dialog";
    wrap.innerHTML =
      '<div class="modal d-block" tabindex="-1" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-message">' +
      '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
      '<div class="modal-header border-0 pb-0"><h5 class="modal-title" id="confirm-title"><i class="bi me-2"></i><span></span></h5>' +
      '<button type="button" class="btn-close" data-answer="no" aria-label="Cancel"></button></div>' +
      '<div class="modal-body pt-2" id="confirm-message"></div>' +
      '<div class="modal-footer border-0 pt-0"><button type="button" class="btn btn-light" data-answer="no">Cancel</button>' +
      '<button type="button" class="btn" data-answer="yes"></button></div></div></div></div>' +
      '<div class="modal-backdrop show"></div>';
    const icon = wrap.querySelector(".modal-title i"), ok = wrap.querySelector('.modal-footer [data-answer="yes"]'),
          cancel = wrap.querySelector('.modal-footer [data-answer="no"]');
    icon.classList.add(opts.danger ? "bi-exclamation-triangle-fill" : "bi-question-circle-fill",
                       opts.danger ? "text-danger" : "text-primary");
    wrap.querySelector(".modal-title span").textContent = opts.title;
    wrap.querySelector(".modal-body").textContent = opts.message;
    ok.textContent = opts.ok;
    ok.classList.add(opts.danger ? "btn-danger" : "btn-primary");
    // Inside an open Bootstrap modal the dialog must live in that modal, or its focus trap steals the focus.
    (document.querySelector(".modal.show") || document.body).appendChild(wrap);
    document.body.classList.add("confirm-open");
    (opts.danger ? cancel : ok).focus();  // destructive actions: Enter does not confirm by accident

    function close(answer) {
      document.removeEventListener("keydown", onKey, true);
      wrap.remove();
      if (!document.querySelector(".confirm-dialog")) document.body.classList.remove("confirm-open");
      if (previous && previous.focus) previous.focus();
      resolve(answer);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(false); }
      if (e.key === "Tab") {  // keep the focus inside the dialog
        const items = Array.from(wrap.querySelectorAll("button"));
        const i = items.indexOf(document.activeElement);
        e.preventDefault();
        items[(i + (e.shiftKey ? items.length - 1 : 1)) % items.length].focus();
      }
    }
    document.addEventListener("keydown", onKey, true);
    wrap.addEventListener("click", function (e) {
      e.stopPropagation();
      const b = e.target.closest("[data-answer]");
      if (b) close(b.dataset.answer === "yes");
      else if (!e.target.closest(".modal-content")) close(false);  // click outside the dialog
    });
  });
}

document.addEventListener("click", function (e) {
  const t = e.target.closest("[data-toggle-sidebar]");
  if (t) { document.body.classList.toggle("sidebar-open"); }
  const c = e.target.closest("a[data-confirm], button[data-confirm]");
  if (!c) return;
  if (c.dataset.confirmed) { delete c.dataset.confirmed; return; }  // second, confirmed click
  e.preventDefault();
  e.stopPropagation();
  showConfirm(confirmOptions(c, c)).then(function (yes) {
    if (yes) { c.dataset.confirmed = "1"; c.click(); }
  });
});

// "Records to keep" period pickers on the clear-attendance forms (devices/_keep_period_fields.html)
document.querySelectorAll("select[data-keep-period]").forEach(function (select) {
  const form = select.closest("form"), range = form.querySelector("[data-keep-range]"),
        help = form.querySelector("[data-keep-help]");
  function update() {
    const custom = select.value === "range";
    range.classList.toggle("d-none", !custom);
    range.querySelectorAll("input").forEach(function (i) { i.required = custom; });
    const opt = select.selectedOptions[0];
    help.textContent = custom ? "Every device record between these dates must be in the portal before clearing."
      : Number(opt.dataset.count).toLocaleString() + " punches " +
        (select.dataset.keepPeriod === "all" ? "from all devices" : "from this device") + " since " +
        opt.dataset.from + " are in the portal now; they stay there.";
  }
  select.addEventListener("change", update);
  update();
});

document.addEventListener("submit", function (e) {
  const f = e.target;
  if (f.dataset.confirm && !f.dataset.confirmed) {
    e.preventDefault();
    const submitter = e.submitter || null;
    showConfirm(confirmOptions(f, submitter)).then(function (yes) {
      if (!yes) return;
      f.dataset.confirmed = "1";
      if (submitter) f.requestSubmit(submitter); else f.requestSubmit();
    });
    return;
  }
  delete f.dataset.confirmed;  // ask again next time (e.g. after the back button)
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
