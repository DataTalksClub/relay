// Relay page behaviour on top of the dakit shell (dakit/shell.js owns the
// sidebar, drawer, popovers and theme switch; dakit/split.js owns list–detail
// keyboard and focus). Loaded once at the end of <body>.
(function () {
  "use strict";

  // Unsaved-changes guard for operator forms.
  const forms = [...document.querySelectorAll('main form[method="post"]')];
  const dirty = new Set();
  forms.forEach(function (form) {
    form.addEventListener("input", () => dirty.add(form));
    form.addEventListener("change", () => dirty.add(form));
    form.addEventListener("submit", () => dirty.delete(form));
  });
  window.addEventListener("beforeunload", function (event) {
    if (dirty.size) { event.preventDefault(); event.returnValue = ""; }
  });

  // Client switcher (brand-row menu): confirm before discarding edits.
  const switcher = document.querySelector("[data-client-switcher]");
  if (switcher) {
    switcher.addEventListener("submit", function (event) {
      if (dirty.size && !window.confirm("You have unsaved changes. Switch clients and discard them?")) {
        event.preventDefault();
        return;
      }
      dirty.clear();
    });
    // Typeahead for the role="menu" switcher: a printable key focuses the
    // next client whose name starts with it (dakit shell.md §11).
    switcher.addEventListener("keydown", function (event) {
      if (event.key.length !== 1 || event.ctrlKey || event.metaKey || event.altKey || event.key === " ") return;
      const items = [...switcher.querySelectorAll('[role="menuitemradio"]')];
      if (!items.length) return;
      const key = event.key.toLowerCase();
      const start = items.indexOf(document.activeElement);
      for (let step = 1; step <= items.length; step++) {
        const item = items[(start + step) % items.length];
        if (item.textContent.trim().toLowerCase().startsWith(key)) {
          item.focus();
          event.preventDefault();
          return;
        }
      }
    });
  }

  // Connect custom help/error containers to controls across the forms.
  document.querySelectorAll(".form-field").forEach(function (field) {
    const control = field.querySelector('input:not([type="hidden"]),select,textarea');
    if (!control || !control.id) return;
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && document.getElementById(id));
    field.querySelectorAll(".helptext,.field-errors").forEach(function (description, index) {
      if (!description.id) description.id = control.id + "-description-" + index;
      if (!ids.includes(description.id)) ids.push(description.id);
      if (description.classList.contains("field-errors")) control.setAttribute("aria-invalid", "true");
    });
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
  });
  const errors = document.querySelector("main .form-errors");
  if (errors) { errors.setAttribute("tabindex", "-1"); errors.setAttribute("role", "alert"); errors.focus(); }

  // Every operator POST carries the active client it was rendered for.
  const main = document.querySelector("[data-active-client]");
  if (main && main.dataset.activeClient) {
    main.querySelectorAll('form[method="post"]').forEach(function (form) {
      if (form.querySelector('[name="operator_client_id"]')) return;
      const context = document.createElement("input");
      context.type = "hidden";
      context.name = "operator_client_id";
      context.value = main.dataset.activeClient;
      form.appendChild(context);
    });
  }
})();
