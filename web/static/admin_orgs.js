(function () {
  "use strict";

  const picker = document.getElementById("manage-org");
  if (picker) picker.addEventListener("change", () => picker.form.submit());

  const filter = document.getElementById("user-filter");
  if (!filter) return;
  const rows = Array.from(document.querySelectorAll(".member-list > li"));
  const noMatch = document.getElementById("no-user-match");
  const carried = document.querySelectorAll(".carry-filter");

  // Case-insensitive match on user name. The filter text rides along with
  // every form so it survives the page reload after an action.
  function apply() {
    const query = filter.value.trim().toLocaleLowerCase();
    let shown = 0;
    rows.forEach((row) => {
      row.hidden = Boolean(query) && !row.dataset.name.includes(query);
      if (!row.hidden) shown += 1;
    });
    noMatch.hidden = shown > 0;
    carried.forEach((input) => (input.value = filter.value));
  }

  filter.addEventListener("input", apply);
  apply();
})();
