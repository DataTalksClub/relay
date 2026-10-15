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

  // Mailbox list–detail (dakit docs/list-detail.md). Rows and Back are links,
  // so everything works without JS; with it, picking a row swaps only the
  // detail pane and pushes the URL, so the list keeps its scroll position
  // and focus, and Back on a phone returns to the row it came from.
  const inbox = document.querySelector("[data-inbox-split]");
  if (inbox && window.fetch && window.DOMParser && window.history.pushState) {
    const detail = inbox.querySelector("[data-inbox-detail]");
    let lastOpened = new URL(location.href).searchParams.get("message");
    let pending = null;

    const rowFor = (id) => inbox.querySelector(`.dk-split__list li[data-message-id="${CSS.escape(String(id))}"]`);
    const markCurrent = (id) => {
      inbox.querySelectorAll(".dk-split__item[aria-current]").forEach((item) => item.removeAttribute("aria-current"));
      const row = id && rowFor(id);
      if (row) row.querySelector(".dk-split__item").setAttribute("aria-current", "true");
    };
    const showList = (focus) => {
      markCurrent(null);
      inbox.setAttribute("data-view", "list");
      const row = lastOpened && rowFor(lastOpened);
      const item = row && row.querySelector(".dk-split__item");
      if (item) {
        item.scrollIntoView({ block: "nearest" });
        if (focus) item.focus({ preventScroll: true });
      }
    };
    const render = async (url, focus) => {
      const id = new URL(url, location.href).searchParams.get("message");
      if (!id) { showList(focus); return; }
      if (pending) pending.abort();
      pending = new AbortController();
      let doc;
      try {
        const response = await fetch(url, { signal: pending.signal, credentials: "same-origin" });
        if (!response.ok) throw new Error(String(response.status));
        doc = new DOMParser().parseFromString(await response.text(), "text/html");
      } catch (error) {
        if (error.name !== "AbortError") location.assign(url);
        return;
      }
      const fresh = doc.querySelector("[data-inbox-detail]");
      if (!fresh) { location.assign(url); return; }
      detail.innerHTML = fresh.innerHTML;
      // Opening marks the message read: take its row's new state, in place.
      const freshRow = doc.querySelector(`.dk-split__list li[data-message-id="${CSS.escape(id)}"]`);
      const row = rowFor(id);
      if (freshRow && row) {
        const hadFocus = row.contains(document.activeElement);
        const replacement = document.importNode(freshRow, true);
        row.replaceWith(replacement);
        if (hadFocus) replacement.querySelector(".dk-split__item").focus({ preventScroll: true });
      }
      document.title = doc.title;
      lastOpened = id;
      markCurrent(id);
      window.dakitSplit.show(inbox, "detail", { focus });
    };

    inbox.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target.closest(".dk-split__item, .dk-split__back");
      if (!link || !inbox.contains(link)) return;
      event.preventDefault();
      history.pushState(null, "", link.href);
      render(link.href, true);
    });
    window.addEventListener("popstate", function () {
      render(location.href, false);
    });
  }
})();
