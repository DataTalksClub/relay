// Newsletter editor progressive enhancement (templates/mailing/operator/newsletter_detail.html
// and newsletter_new.html). Every form works without this file as a normal POST/GET + redirect;
// with it, editor forms marked data-nl-ajax are sent with fetch and the page's regions are
// replaced from the returned HTML (the same page the redirect would show), so the editor
// keeps its scroll position, other unsaved forms, and the live preview reloads in place.
(() => {
  "use strict";

  let submittingNatively = false;

  // ------------------------------------------------------------------ unsaved changes

  const isDirty = form => form.dataset.dirty === "1";
  const dirtyForms = () => [...document.querySelectorAll("form[data-nl-track]")].filter(isDirty);

  document.addEventListener("input", event => {
    const form = event.target.closest && event.target.closest("form[data-nl-track]");
    if (form) form.dataset.dirty = "1";
  });
  document.addEventListener("change", event => {
    const form = event.target.closest && event.target.closest("form[data-nl-track]");
    if (form) form.dataset.dirty = "1";
  });

  window.addEventListener("beforeunload", event => {
    if (submittingNatively || !dirtyForms().length) return;
    event.preventDefault();
    event.returnValue = "";
  });

  // ------------------------------------------------------------------ new-newsletter page

  const busyForm = document.querySelector("form[data-nl-busy-form]");
  if (busyForm) {
    busyForm.addEventListener("submit", () => {
      submittingNatively = true;
      const button = busyForm.querySelector("[data-nl-busy-label]");
      const status = busyForm.querySelector("[data-nl-busy-status]");
      if (button) {
        // Disable after the submit has been dispatched so the browser still sends the form.
        setTimeout(() => {
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
          button.textContent = "Creating…";
        }, 0);
      }
      if (status && button) status.textContent = button.dataset.nlBusyLabel;
    });
    busyForm.querySelectorAll("input[data-nl-template]").forEach(radio => {
      radio.addEventListener("change", () => {
        // Reload the suggested values for the other template, unless fields were already typed in.
        if (busyForm.dataset.dirty === "1") return;
        const url = new URL(window.location.href);
        url.searchParams.set("template", radio.value);
        window.location.href = url.toString();
      });
    });
    busyForm.addEventListener("input", event => {
      if (!event.target.matches("input[data-nl-template]")) busyForm.dataset.dirty = "1";
    });
  }

  const editor = document.querySelector("[data-nl-editor]");
  if (!editor) return;
  const issueId = editor.dataset.nlEditor;

  // ------------------------------------------------------------------ helpers

  const scroller = element => {
    for (let node = element.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) return node;
    }
    return document.scrollingElement || document.documentElement;
  };

  const toast = (text, danger) => {
    document.querySelectorAll(".nl-toast").forEach(item => item.remove());
    if (!text) return;
    const box = document.createElement("div");
    box.className = "dk-toast nl-toast" + (danger ? " dk-toast--danger" : "");
    box.setAttribute("role", danger ? "alert" : "status");
    const message = document.createElement("span");
    message.className = "dk-toast__message";
    message.textContent = text;
    box.appendChild(message);
    document.body.appendChild(box);
    setTimeout(() => box.remove(), danger ? 9000 : 4500);
  };

  const blockIds = root => new Set([...root.querySelectorAll("[data-nl-block]")].map(item => item.id));

  // Replace the editor's regions with the ones from a freshly rendered editor page.
  const applyPage = (doc, submittedForm) => {
    const fresh = doc.querySelector("[data-nl-editor]");
    if (!fresh || fresh.dataset.nlEditor !== issueId) return false;
    const keepRow = submittedForm ? submittedForm.closest("[data-nl-block]") : null;

    fresh.querySelectorAll("[data-nl-region]").forEach(region => {
      const name = region.dataset.nlRegion;
      const current = editor.querySelector(`[data-nl-region="${name}"]`);
      if (!current) return;
      if (name === "blocks") {
        // Swap row by row: a row with unsaved typing that was not the one submitted stays as it is.
        const openRows = new Set([...current.querySelectorAll("[data-nl-edit][open]")]
          .map(item => item.closest("[data-nl-block]").id));
        const nodes = [...region.children].map(row => {
          const old = row.id ? current.querySelector(`#${CSS.escape(row.id)}`) : null;
          if (old && old !== keepRow && [...old.querySelectorAll("form[data-nl-track]")].some(isDirty)) return old;
          if (old && old !== keepRow && openRows.has(row.id)) {
            const details = row.querySelector("[data-nl-edit]");
            if (details) details.open = true;
          }
          return document.importNode(row, true);
        });
        current.replaceChildren(...nodes);
        return;
      }
      const busy = [...current.querySelectorAll("form[data-nl-track]")]
        .some(form => isDirty(form) && form !== submittedForm);
      if (!busy) current.replaceWith(document.importNode(region, true));
    });

    const frame = editor.querySelector("#nl-preview-frame");
    const freshFrame = fresh.querySelector("#nl-preview-frame");
    if (frame && freshFrame && frame.getAttribute("srcdoc") !== freshFrame.getAttribute("srcdoc")) {
      frame.setAttribute("srcdoc", freshFrame.getAttribute("srcdoc"));
    } else if (!frame !== !freshFrame) {
      const aside = editor.querySelector(".nl-preview__body");
      const freshAside = fresh.querySelector(".nl-preview__body");
      if (aside && freshAside) aside.replaceWith(document.importNode(freshAside, true));
    }

    const messages = [...doc.querySelectorAll(".messages .message")];
    document.querySelectorAll(".messages").forEach(list => list.remove());
    if (messages.length) {
      const danger = messages.some(item => item.classList.contains("error"));
      toast(messages.map(item => item.textContent.trim()).join(" "), danger);
    }
    return true;
  };

  const submitAjax = async (form, submitter) => {
    const method = (form.getAttribute("method") || "get").toUpperCase();
    const action = new URL(form.getAttribute("action") || window.location.href, window.location.href);
    action.hash = "";
    const data = new FormData(form, submitter || undefined);
    const init = { method, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } };
    let url = action;
    if (method === "GET") {
      url = new URL(action);
      url.search = new URLSearchParams(data).toString();
    } else {
      init.body = data;
    }

    const row = form.closest("[data-nl-block]");
    const anchor = row && row.isConnected ? row : form;
    const anchorTop = anchor.getBoundingClientRect().top;
    const box = scroller(editor);
    const before = blockIds(editor);
    if (submitter) {
      submitter.disabled = true;
      submitter.setAttribute("aria-busy", "true");
    }
    editor.setAttribute("aria-busy", "true");

    let response;
    try {
      response = await fetch(url.toString(), init);
    } catch (error) {
      // Network trouble: fall back to the normal page submit, which shows a proper error page.
      editor.removeAttribute("aria-busy");
      if (submitter) submitter.disabled = false;
      submittingNatively = true;
      form.dataset.nlNative = "1";
      form.requestSubmit(submitter || undefined);
      return;
    }
    editor.removeAttribute("aria-busy");
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    if (!applyPage(doc, form)) {
      // Not the editor (e.g. the active client changed in another tab): show what the server sent.
      submittingNatively = true;
      window.location.href = response.url;
      return;
    }

    // Keep the edited block where it was on screen, or bring a new block / the first error into view.
    const invalid = editor.querySelector("[data-nl-block] [aria-invalid='true'], #newsletter-details [aria-invalid='true']");
    const added = [...blockIds(editor)].find(id => !before.has(id));
    const target = invalid ? invalid.closest("[data-nl-block], #newsletter-details")
      : (added && document.getElementById(added)) || (row && row.id && document.getElementById(row.id))
        || document.getElementById("newsletter-blocks");
    if (target) {
      if (added || method === "GET" || !row) {
        target.scrollIntoView({ block: "start" });
      } else {
        box.scrollTop += target.getBoundingClientRect().top - anchorTop;
        // A saved block collapses: make sure its header is on screen, not just its tail.
        if (target.getBoundingClientRect().top < 0) target.scrollIntoView({ block: "start" });
      }
      if (!invalid && target.matches("[data-nl-block]:not([data-nl-block=new])")) {
        target.classList.add("nl-block--saved");
        setTimeout(() => target.classList.remove("nl-block--saved"), 2500);
      }
    }
    // A form that came back with errors still holds unsaved typing.
    editor.querySelectorAll("form[data-nl-track]").forEach(item => {
      if (item.querySelector("[aria-invalid='true']")) item.dataset.dirty = "1";
    });
    if (invalid) invalid.focus({ preventScroll: true });
    else if (added) {
      const first = document.getElementById(added).querySelector("input:not([type=hidden]), textarea, select");
      if (first && method === "GET") first.focus({ preventScroll: true });
    }
  };

  document.addEventListener("submit", event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !editor.contains(form)) return;
    const submitter = event.submitter;
    if (form.dataset.nlNative === "1" || !form.hasAttribute("data-nl-ajax")
        || (submitter && submitter.hasAttribute("data-nl-native"))) {
      submittingNatively = true;
      return;
    }
    if (form.hasAttribute("data-nl-type-change")) {
      const row = form.closest("[data-nl-block]");
      const pending = row && [...row.querySelectorAll("form[data-nl-track]")].some(isDirty);
      if (pending && !window.confirm("Changing the block type discards the unsaved changes in this block. Continue?")) {
        event.preventDefault();
        return;
      }
    }
    event.preventDefault();
    submitAjax(form, submitter);
  });

  // ------------------------------------------------------------------ clicks

  document.addEventListener("click", event => {
    const cancel = event.target.closest("[data-nl-cancel]");
    if (cancel && editor.contains(cancel)) {
      event.preventDefault();
      const row = cancel.closest("[data-nl-block]");
      if (row && row.dataset.nlBlock === "new") {
        row.remove();
        return;
      }
      const form = cancel.closest("form");
      if (form) {
        form.reset();
        delete form.dataset.dirty;
        form.querySelectorAll("[aria-invalid]").forEach(field => field.removeAttribute("aria-invalid"));
        form.querySelectorAll(".field-errors").forEach(item => item.remove());
      }
      const details = cancel.closest("[data-nl-edit]");
      if (details) details.open = false;
      return;
    }
    const open = event.target.closest("[data-nl-open]");
    if (open) {
      const details = document.querySelector(`#block-${CSS.escape(open.dataset.nlOpen)} [data-nl-edit]`);
      if (details) details.open = true;
      return;
    }
    const test = event.target.closest("[data-nl-test-block]");
    if (test) {
      const scope = document.getElementById("test-scope");
      const section = document.getElementById("newsletter-test-send");
      if (scope && section) {
        event.preventDefault();
        scope.value = test.dataset.nlTestBlock;
        section.scrollIntoView({ block: "start" });
        scope.focus({ preventScroll: true });
      }
    }
  });

  // ------------------------------------------------------------------ test send (not fetch: shows per-address results)

  document.addEventListener("submit", event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id !== "newsletter-test-form") return;
    const status = document.getElementById("newsletter-test-status");
    const anyPreset = form.querySelectorAll("input[name=preset]:checked").length > 0;
    if (!anyPreset && !form.emails.value.trim()) {
      event.preventDefault();
      submittingNatively = false;
      status.textContent = "Choose or enter at least one address.";
      return;
    }
    const button = document.getElementById("newsletter-test-button");
    setTimeout(() => { button.disabled = true; button.textContent = "Sending…"; }, 0);
    status.textContent = "Sending the test email. This can take up to 45 seconds.";
    setTimeout(() => {
      button.disabled = false;
      button.textContent = "Send [TEST] email";
      status.textContent = "No answer from the server after 60 seconds. Check \"Recent tests\" below before sending again.";
    }, 60000);
  });
})();

// Preview width toggle (desktop / mobile) for the editor's live preview.
(() => {
  const group = document.querySelector(".nl-preview [role=radiogroup]");
  if (!group) return;
  group.addEventListener("click", event => {
    const button = event.target.closest("[data-nl-width]");
    if (!button) return;
    const frame = document.getElementById("nl-preview-frame");
    if (frame) frame.style.width = button.dataset.nlWidth;
    group.querySelectorAll("[data-nl-width]").forEach(item => {
      item.setAttribute("aria-checked", String(item === button));
      item.tabIndex = item === button ? 0 : -1;
    });
  });
})();
