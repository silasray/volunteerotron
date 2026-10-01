(function () {
  "use strict";

  // Kept current: Reload Details swaps in a type's fresh data.
  const config = JSON.parse(document.getElementById("config-data").textContent);
  const typeById = (id) => config.enrichment_types.find((t) => t.id === id);

  // ---------------------------------------------------------------- time helpers
  // Times are shown and typed in the browser's time zone as "YYYY-MM-DD HH:MM"
  // and sent to the server as UTC ISO strings.
  const pad = (n) => String(n).padStart(2, "0");
  const toLocalText = (date) =>
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;

  function parseLocalText(text) {
    const m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*$/.exec(text);
    if (!m) return null;
    const [, y, mo, d, h, mi, s] = m.map(Number);
    const date = new Date(y, mo - 1, d, h, mi, s || 0);
    // Reject impossible dates like 2026-02-31 instead of rolling them over.
    if (date.getFullYear() !== y || date.getMonth() !== mo - 1 || date.getDate() !== d || h > 23) return null;
    return date;
  }

  const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateTimeFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

  function rangeText(start, end, withDate) {
    const s = (withDate ? dateTimeFmt : timeFmt).format(start);
    const e = (dayKey(start) === dayKey(end) ? timeFmt : dateTimeFmt).format(end);
    return `${s} – ${e}`;
  }

  function syncIso(input) {
    const hidden = document.getElementById(input.dataset.isoTarget);
    const text = input.value.trim();
    if (!text) {
      hidden.value = "";
      input.setCustomValidity("");
      return true;
    }
    const date = parseLocalText(text);
    if (!date) {
      input.setCustomValidity("Use YYYY-MM-DD HH:MM, e.g. 2026-10-04 09:00");
      return false;
    }
    input.setCustomValidity("");
    hidden.value = date.toISOString();
    return true;
  }

  // ---------------------------------------------------------------- message dialog
  // Used for failed saves and failed reloads; never navigates away.
  const msgDialog = document.getElementById("values-error-dialog");
  const msgTitle = document.getElementById("values-error-title");
  const msgSummary = document.getElementById("values-error-summary");
  const msgList = document.getElementById("values-error-list");
  const msgHint = document.getElementById("values-error-hint");
  document.getElementById("values-error-ok").addEventListener("click", () => msgDialog.close());

  function showMessage(title, summary, items, { hint = false, focus = null } = {}) {
    msgTitle.textContent = title;
    msgSummary.textContent = summary;
    msgList.textContent = "";
    items.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      msgList.append(li);
    });
    msgHint.hidden = !hint;
    msgDialog.showModal();
    msgDialog.addEventListener("close", () => focus && focus.focus(), { once: true });
  }

  // ---------------------------------------------------------------- calendar picker
  const whenDialog = document.getElementById("when-dialog");
  const calDays = document.getElementById("cal-days");
  const calMonth = document.getElementById("cal-month");
  const calTime = document.getElementById("cal-time");
  const monthFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
  let pickTarget = null;
  let shown = null; // first day of the month on screen
  let picked = null; // selected day

  const weekdayRow = document.getElementById("cal-weekdays");
  for (let i = 0; i < 7; i++) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = new Intl.DateTimeFormat(undefined, { weekday: "narrow" }).format(new Date(2026, 1, 1 + i)); // Feb 1 2026 is a Sunday
    weekdayRow.append(th);
  }

  function renderMonth() {
    calMonth.textContent = monthFmt.format(shown);
    calDays.textContent = "";
    const first = new Date(shown.getFullYear(), shown.getMonth(), 1);
    const daysInMonth = new Date(shown.getFullYear(), shown.getMonth() + 1, 0).getDate();
    let row = document.createElement("tr");
    for (let i = 0; i < first.getDay(); i++) row.append(document.createElement("td"));
    for (let d = 1; d <= daysInMonth; d++) {
      const date = new Date(shown.getFullYear(), shown.getMonth(), d);
      const td = document.createElement("td");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = d;
      b.className = "cal-day";
      b.setAttribute("aria-label", dayFmt.format(date));
      if (picked && dayKey(picked) === dayKey(date)) b.setAttribute("aria-pressed", "true");
      if (dayKey(new Date()) === dayKey(date)) b.classList.add("today");
      b.addEventListener("click", () => {
        picked = date;
        renderMonth();
      });
      td.append(b);
      row.append(td);
      if (date.getDay() === 6) {
        calDays.append(row);
        row = document.createElement("tr");
      }
    }
    if (row.children.length) calDays.append(row);
  }

  document.getElementById("cal-prev").addEventListener("click", () => {
    shown = new Date(shown.getFullYear(), shown.getMonth() - 1, 1);
    renderMonth();
  });
  document.getElementById("cal-next").addEventListener("click", () => {
    shown = new Date(shown.getFullYear(), shown.getMonth() + 1, 1);
    renderMonth();
  });
  document.getElementById("when-cancel").addEventListener("click", () => whenDialog.close());
  document.getElementById("when-ok").addEventListener("click", () => {
    const [h, m] = (calTime.value || "00:00").split(":").map(Number);
    const date = new Date(picked.getFullYear(), picked.getMonth(), picked.getDate(), h, m);
    pickTarget.value = toLocalText(date);
    syncIso(pickTarget);
    whenDialog.close();
    pickTarget.focus();
  });

  // Time text boxes and their 📅 buttons inside root.
  function bindTimeInputs(root) {
    root.querySelectorAll(".when-text").forEach((input) => {
      if (input.dataset.initialIso) input.value = toLocalText(new Date(input.dataset.initialIso));
      input.addEventListener("input", () => syncIso(input));
    });
    root.querySelectorAll(".pick-when").forEach((button) => {
      button.addEventListener("click", () => {
        pickTarget = document.getElementById(button.dataset.for);
        const current = parseLocalText(pickTarget.value);
        picked = current || new Date();
        shown = new Date(picked.getFullYear(), picked.getMonth(), 1);
        calTime.value = current ? `${pad(current.getHours())}:${pad(current.getMinutes())}` : "09:00";
        renderMonth();
        whenDialog.showModal();
      });
    });
  }

  // ---------------------------------------------------------------- delete dialog
  // Deleting something volunteers chose asks what to do with their choices:
  // move them to another item of the same kind, or delete them too.
  const delDialog = document.getElementById("delete-dialog");
  const delMessage = document.getElementById("delete-dialog-message");
  const delTitle = document.getElementById("delete-dialog-title");
  const delReassign = document.getElementById("delete-dialog-reassign");
  const delTarget = document.getElementById("delete-dialog-target");
  const delTargetLabel = document.getElementById("delete-dialog-target-label");
  const delMove = document.getElementById("delete-dialog-move");
  const delCascade = document.getElementById("delete-dialog-cascade");
  let pendingForm = null;

  function enrichmentLabel(type, enrichment) {
    const parts = type.fields
      .map((f) => {
        const v = enrichment.values[f.id];
        if (!v) return null;
        return v.value !== undefined ? v.value : rangeText(new Date(v.start), new Date(v.end), true);
      })
      .filter(Boolean);
    return parts.length ? parts.join(" · ") : "(empty row)";
  }

  function candidates(form) {
    const id = form.dataset.id;
    if (form.dataset.kind === "window") {
      return config.windows
        .filter((w) => w.id !== id)
        .map((w) => {
          const s = new Date(w.start);
          return [w.id, `${dayFmt.format(s)}, ${rangeText(s, new Date(w.end), false)}`];
        });
    }
    if (form.dataset.kind === "row") {
      const type = typeById(form.dataset.typeId);
      return type.enrichments.filter((e) => e.id !== id).map((e) => [e.id, enrichmentLabel(type, e)]);
    }
    return null; // whole enrichment types and columns: delete-with-everything only
  }

  const TEXT = {
    window: (n) => ({
      title: "Delete this window?",
      message: `${n} volunteer${n === 1 ? " has" : "s have"} already offered their time in this window.`,
      label: "Move their offers to",
      cascade: "Delete window and their offers",
    }),
    row: (n) => ({
      title: "Delete this row?",
      message: `${n} volunteer${n === 1 ? " has" : "s have"} already chosen this option.`,
      label: "Move their choices to",
      cascade: "Delete row and their choices",
    }),
    field: (n, label) => ({
      title: `Delete the ${label} column?`,
      message: `${n} row${n === 1 ? " has a value" : "s have values"} in this column. Deleting it removes those values; volunteers' choices aren't affected.`,
      label: "",
      cascade: "Delete column and its values",
    }),
    type: (n, label) => ({
      title: `Delete ${label}?`,
      message: `${n} volunteer choice${n === 1 ? " uses" : "s use"} this enrichment. Deleting it removes them, along with all its fields and rows.`,
      label: "",
      cascade: "Delete enrichment and their choices",
    }),
  };

  function bindDeleteForms(root) {
    root.querySelectorAll("form.needs-delete-dialog").forEach((form) => {
      form.addEventListener("submit", (event) => {
        const linked = Number(form.dataset.linked || 0);
        if (!linked || form.dataset.confirmed === "1") return; // nothing linked: delete directly
        event.preventDefault();
        pendingForm = form;
        const t = TEXT[form.dataset.kind](linked, form.dataset.label);
        delTitle.textContent = t.title;
        delMessage.textContent = t.message;
        delCascade.textContent = t.cascade;
        const options = candidates(form);
        delReassign.hidden = options === null;
        if (options !== null) {
          delTargetLabel.textContent = t.label;
          delTarget.textContent = "";
          options.forEach(([value, text]) => {
            const o = document.createElement("option");
            o.value = value;
            o.textContent = text;
            delTarget.append(o);
          });
          delTarget.disabled = delMove.disabled = options.length === 0;
          if (!options.length) {
            const o = document.createElement("option");
            o.textContent = "(nothing else to move them to)";
            delTarget.append(o);
          }
        }
        delDialog.showModal();
      });
    });
  }

  function submitPending(mode) {
    pendingForm.querySelector('input[name="mode"]').value = mode;
    const target = pendingForm.querySelector('input[name="reassign_to"]');
    if (target) target.value = mode === "reassign" ? delTarget.value : "";
    pendingForm.dataset.confirmed = "1";
    delDialog.close();
    pendingForm.requestSubmit();
  }

  delMove.addEventListener("click", () => submitPending("reassign"));
  delCascade.addEventListener("click", () => submitPending("cascade"));
  document.getElementById("delete-dialog-cancel").addEventListener("click", () => delDialog.close());

  // ---------------------------------------------------------------- add field dialog
  const fieldDialog = document.getElementById("add-field-dialog");
  const fieldForm = document.getElementById("add-field-form");
  document.getElementById("add-field-cancel").addEventListener("click", () => fieldDialog.close());

  function bindAddField(root) {
    root.querySelectorAll(".open-add-field").forEach((button) => {
      button.addEventListener("click", () => {
        fieldForm.action = fieldForm.dataset.actionTemplate.replace("__TYPE__", button.dataset.typeId);
        document.getElementById("add-field-title").textContent = `Add field to ${button.dataset.typeName}`;
        fieldForm.reset();
        fieldDialog.showModal();
        document.getElementById("add-field-name").focus();
      });
    });
  }

  // ---------------------------------------------------------------- update values
  // Saved in the background so a failure never reloads the page: problems are
  // listed in a dialog and highlighted, and everything typed stays in place.
  function cellInputs(rowId, fieldId) {
    const text = document.querySelector(`input[name="t|${rowId}|${fieldId}"]`);
    if (text) return [text];
    return ["start", "end"]
      .map((part) => document.getElementById(`c-${rowId}-${fieldId}-${part}-text`))
      .filter(Boolean);
  }

  function showProblems(typeId, summary, problems) {
    const type = typeById(typeId);
    const rowNumber = Object.fromEntries(type.enrichments.map((e, i) => [e.id, i + 1]));
    const fieldName = Object.fromEntries(type.fields.map((f) => [f.id, f.name]));
    let first = null;
    const items = problems.map((p) => {
      cellInputs(p.row, p.field).forEach((input) => {
        input.setAttribute("aria-invalid", "true");
        input.closest("td").classList.add("cell-error");
        first = first || input;
      });
      const where = [p.row in rowNumber ? `Row ${rowNumber[p.row]}` : null, fieldName[p.field] || null]
        .filter(Boolean).join(", ");
      return where ? `${where}: ${p.message}` : p.message;
    });
    showMessage("Couldn't save the values", summary, items, { hint: true, focus: first });
  }

  function bindValuesForm(etype) {
    const typeId = etype.dataset.typeId;
    const form = document.getElementById(`values-${typeId}`);
    if (!form) return;
    const body = etype.querySelector(".etype-body");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      body.querySelectorAll(".cell-error").forEach((td) => td.classList.remove("cell-error"));
      body.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));

      // Times typed in a form we can't read are reported the same way.
      const problems = [];
      body.querySelectorAll(".when-text").forEach((input) => {
        const hidden = document.getElementById(input.dataset.isoTarget);
        if (!hidden || hidden.getAttribute("form") !== form.id) return;
        if (!syncIso(input)) {
          const [, rowId, fieldId, part] = hidden.name.split("|");
          problems.push({ row: rowId, field: fieldId, message: `${part} isn't a date and time (use YYYY-MM-DD HH:MM)` });
        }
      });
      if (problems.length) {
        showProblems(typeId, "Some values need fixing before they can be saved.", problems);
        return;
      }

      const buttons = document.querySelectorAll(`button[form="${form.id}"]`);
      buttons.forEach((b) => (b.disabled = true));
      let result = null;
      try {
        const resp = await fetch(new URL(form.getAttribute("action"), window.location.href), {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json" },
        });
        result = await resp.json();
      } catch (err) {
        result = null;
      } finally {
        buttons.forEach((b) => (b.disabled = false));
      }
      if (result && result.ok) {
        // Reload for fresh data and the "Saved" message. If only the #anchor
        // differs, assign() would just scroll, so force a real reload.
        const target = new URL(result.reload, window.location.href);
        if (target.pathname + target.search === window.location.pathname + window.location.search) {
          // Update the address without a navigation, then reload once.
          window.history.replaceState(null, "", target);
          window.location.reload();
        } else {
          window.location.assign(target);
        }
        return;
      }
      if (!result) {
        showProblems(typeId, "The values couldn't be saved: the server didn't respond as expected. "
          + "If you've been signed out, sign in again in another tab, then retry here.", []);
        return;
      }
      showProblems(typeId, result.error, result.problems || []);
    });
  }

  // ---------------------------------------------------------------- expand / collapse
  const OPEN_KEY = "configure-open-types:" + config.event.id;
  let remembered = [];
  try {
    remembered = JSON.parse(sessionStorage.getItem(OPEN_KEY) || "[]");
  } catch (e) {
    remembered = [];
  }

  function setOpen(etype, open) {
    const twisty = etype.querySelector(".twisty");
    etype.querySelector(".etype-body").hidden = !open;
    etype.querySelector(".reload-type").hidden = !open; // only offered while expanded
    twisty.setAttribute("aria-expanded", String(open));
    twisty.textContent = open ? "▾" : "▸";
    etype.querySelectorAll(".keep-open").forEach((i) => (i.value = open ? etype.dataset.typeId : ""));
  }

  function saveOpen() {
    const ids = Array.from(document.querySelectorAll(".etype"))
      .filter((e) => !e.querySelector(".etype-body").hidden)
      .map((e) => e.dataset.typeId);
    try {
      sessionStorage.setItem(OPEN_KEY, JSON.stringify(ids));
    } catch (e) {
      /* storage unavailable: open state just isn't remembered */
    }
  }

  // ---------------------------------------------------------------- reload details
  // Replaces one type's block (header controls, fields and rows) with what the
  // server has now, discarding unsaved edits in that type only.
  async function reloadType(etype) {
    const typeId = etype.dataset.typeId;
    const button = etype.querySelector(".reload-type");
    button.disabled = true;
    let doc = null;
    try {
      const url = new URL(window.location.href);
      url.hash = "";
      url.searchParams.set("open", typeId);
      const resp = await fetch(url, { headers: { Accept: "text/html" } });
      if (resp.ok) doc = new DOMParser().parseFromString(await resp.text(), "text/html");
    } catch (err) {
      doc = null;
    }
    const fresh = doc && doc.getElementById(`type-${typeId}`);
    const freshConfig = doc && doc.getElementById("config-data");
    if (!doc || !freshConfig) {
      button.disabled = false;
      showMessage("Couldn't reload", "The latest details couldn't be loaded. If you've been signed out, "
        + "sign in again in another tab, then try again. Your edits are still on the page.", [], { focus: button });
      return;
    }
    const latest = JSON.parse(freshConfig.textContent);
    const index = config.enrichment_types.findIndex((t) => t.id === typeId);
    if (!fresh) {
      // Deleted elsewhere since this page loaded.
      config.enrichment_types.splice(index, 1);
      etype.remove();
      saveOpen();
      showMessage("Enrichment no longer exists", "It was deleted since this page was loaded.", []);
      return;
    }
    config.enrichment_types[index] = latest.enrichment_types.find((t) => t.id === typeId);
    const replacement = document.importNode(fresh, true);
    etype.replaceWith(replacement);
    initEtype(replacement, true);
    saveOpen();
    replacement.querySelector(".reload-type").focus();
  }

  function initEtype(etype, open) {
    setOpen(etype, open);
    const body = etype.querySelector(".etype-body");
    etype.querySelector(".twisty").addEventListener("click", () => {
      setOpen(etype, body.hidden);
      saveOpen();
    });
    etype.querySelector(".reload-type").addEventListener("click", () => reloadType(etype));
    bindTimeInputs(etype);
    bindDeleteForms(etype);
    bindAddField(etype);
    bindValuesForm(etype);
  }

  // ---------------------------------------------------------------- page setup
  // Windows: sorted by the server; add a day heading before the first window of each day.
  let lastDay = null;
  document.querySelectorAll(".window-row").forEach((row) => {
    const start = new Date(row.dataset.start);
    const end = new Date(row.dataset.end);
    row.querySelector(".window-time").textContent = rangeText(start, end, false);
    if (dayKey(start) !== lastDay) {
      lastDay = dayKey(start);
      const heading = document.createElement("li");
      heading.className = "day-heading";
      heading.textContent = dayFmt.format(start);
      row.parentNode.insertBefore(heading, row);
    }
  });

  const windowsSection = document.getElementById("windows");
  bindTimeInputs(windowsSection);
  bindDeleteForms(windowsSection);
  // Add Window: convert both times, refuse to submit if either is invalid.
  windowsSection.querySelectorAll("form.add-window").forEach((form) => {
    form.addEventListener("submit", (event) => {
      let ok = true;
      form.querySelectorAll(".when-text").forEach((input) => {
        if (!syncIso(input)) {
          ok = false;
          input.reportValidity();
        }
      });
      if (!ok) event.preventDefault();
    });
  });

  document.querySelectorAll(".etype").forEach((etype) => {
    const open = etype.querySelector(".etype-body").dataset.initiallyOpen === "true"
      || remembered.includes(etype.dataset.typeId);
    initEtype(etype, open);
  });
  saveOpen();
})();
