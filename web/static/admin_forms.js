// Forms marked data-background-form are submitted without leaving the page.
// On success the page reloads to show the result (and its "done" message).
// On failure an error dialog explains what's wrong and everything typed stays
// in place; toggles marked data-revert-on-error flip back, since the change
// didn't happen. Without this script the forms work as ordinary posts.
(function () {
  "use strict";

  const dialog = document.getElementById("form-error-dialog");
  const message = document.getElementById("form-error-message");
  let returnFocus = null;
  document.getElementById("form-error-ok").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (returnFocus) returnFocus.focus();
    returnFocus = null;
  });

  function showError(text, form) {
    message.textContent = text;
    // Back to the first field the user can fix, or the button they pressed.
    returnFocus = form.querySelector("input:not([type=hidden]):not([disabled]), select, textarea")
      || document.activeElement;
    dialog.showModal();
  }

  function reloadTo(url) {
    const target = new URL(url, window.location.href);
    // Same page apart from the #anchor: assign() would only scroll.
    if (target.pathname + target.search === window.location.pathname + window.location.search) {
      // Update the address without a navigation, then reload once.
      window.history.replaceState(null, "", target);
      window.location.reload();
    } else {
      window.location.assign(target);
    }
  }

  // Listening on the document runs after each form's own submit handlers, so
  // page scripts can validate first (and cancel) before anything is sent.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-background-form")) return;
    if (event.defaultPrevented) return;
    event.preventDefault();

    const toggles = form.hasAttribute("data-revert-on-error")
      ? Array.from(form.querySelectorAll("input[type=checkbox]"), (box) => [box, !box.checked])
      : [];
    const buttons = form.querySelectorAll("button, input[type=submit]");
    buttons.forEach((b) => (b.disabled = true));
    let status = 0;
    let body = null;
    try {
      const data = new FormData(form);
      if (event.submitter && event.submitter.name) data.append(event.submitter.name, event.submitter.value);
      // getAttribute, not form.action: a field named "action" (e.g. the member
      // buttons on the organizations page) shadows the form's action property.
      const url = new URL(form.getAttribute("action") || window.location.href, window.location.href);
      const resp = await fetch(url, {
        method: "POST",
        body: data,
        headers: { Accept: "application/json" },
      });
      status = resp.status;
      body = await resp.json().catch(() => null);
    } catch (err) {
      body = null;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }

    if (body && body.ok) {
      reloadTo(body.reload);
      return;
    }
    toggles.forEach(([box, was]) => (box.checked = was));
    if (body && body.error) {
      showError(body.error, form);
    } else if (status === 404) {
      showError("That's no longer available. It may have been deleted, or you may not have access any more. Reload the page to see the current state.", form);
    } else {
      showError("The change couldn't be saved: the server didn't respond as expected. If you've been signed out, sign in again in another tab, then try again.", form);
    }
  });
})();
