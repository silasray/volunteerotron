(function () {
  "use strict";

  // Switching organization reloads the current page for that organization.
  const picker = document.querySelector(".org-picker select");
  if (picker) picker.addEventListener("change", () => picker.form.submit());

  const dialog = document.getElementById("new-event-dialog");
  if (!dialog) return;
  document.getElementById("new-event-open").addEventListener("click", () => {
    dialog.showModal();
    document.getElementById("new-event-name").focus();
  });
  document.getElementById("new-event-cancel").addEventListener("click", () => dialog.close());
  // Reopened after a failed create so the error and the typed values are shown.
  if (dialog.hasAttribute("data-open-on-load")) dialog.showModal();
})();
