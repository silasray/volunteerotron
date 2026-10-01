(function () {
  "use strict";

  const data = JSON.parse(document.getElementById("form-data").textContent);
  const form = document.getElementById("volunteer-form");
  const offerUrl = form.dataset.offerUrl;
  const emailInput = document.getElementById("email");
  const nameInput = document.getElementById("name");
  const statusEl = document.getElementById("status");
  const buttons = [document.getElementById("load"), document.getElementById("submit")];

  // Times arrive in UTC; show and group them in the volunteer's own time zone.
  const dayFmt = new Intl.DateTimeFormat(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });

  function dayKey(date) {
    return [date.getFullYear(), date.getMonth(), date.getDate()].join("-");
  }

  function formatRange(startIso, endIso, withDate) {
    const start = new Date(startIso);
    const end = new Date(endIso);
    const startText = (withDate ? dateTimeFmt : timeFmt).format(start);
    const endText = (dayKey(start) === dayKey(end) ? timeFmt : dateTimeFmt).format(end);
    return startText + " – " + endText;
  }

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    (children || []).forEach((child) => node.append(child));
    return node;
  }

  function checkbox(name, value, text) {
    const input = el("input", { type: "checkbox", name: name, value: value });
    return el("label", { className: "choice" }, [input, " ", text]);
  }

  function renderWindows() {
    const container = document.getElementById("windows");
    if (data.windows.length === 0) {
      container.append(el("p", { textContent: "No volunteer windows are available." }));
      return;
    }
    let currentKey = null;
    let list = null;
    data.windows.forEach((w) => {
      const start = new Date(w.start);
      if (dayKey(start) !== currentKey) {
        currentKey = dayKey(start);
        list = el("div", { className: "choices" });
        container.append(el("h3", { textContent: dayFmt.format(start) }), list);
      }
      list.append(checkbox("window", w.id, formatRange(w.start, w.end, false)));
    });
  }

  function enrichmentLabel(enrichment) {
    const parts = enrichment.fragments.map((f) =>
      f.kind === "calendar" ? formatRange(f.start, f.end, true) : f.value
    );
    return parts.length ? parts.join(" · ") : "(no details)";
  }

  // Text a filter matches against: the option's visible text fragment values.
  function searchText(enrichment) {
    return enrichment.fragments
      .filter((f) => f.kind === "text")
      .map((f) => f.value)
      .join("\n")
      .toLocaleLowerCase();
  }

  // Filters only change what is shown; selections are never dropped. A selected
  // option that doesn't match stays visible but greyed out. Deselecting it
  // leaves it greyed until the filter is next re-applied.
  const filters = [];

  function addFilter(fieldset, type, items, select) {
    const input = el("input", { type: "search", placeholder: "Filter", className: "filter" });
    input.setAttribute("aria-label", "Filter " + type.name);
    const noMatch = el("p", { className: "hint", textContent: "No options match.", hidden: true });
    fieldset.insertBefore(input, fieldset.children[1]);
    fieldset.append(noMatch);

    // The closed dropdown shows its current choice, so grey it too when that choice is.
    const syncSelect = () => {
      if (select) {
        const chosen = select.options[select.selectedIndex];
        select.classList.toggle("filtered-out", chosen.classList.contains("filtered-out"));
      }
    };

    const refresh = () => {
      const query = input.value.trim().toLocaleLowerCase();
      let shown = 0;
      items.forEach(({ node, text, isSelected, isOption }) => {
        const matches = !query || text.includes(query);
        const greyed = !matches && isSelected();
        node.hidden = !matches && !greyed;
        node.classList.toggle("filtered-out", greyed);
        if (isOption) node.disabled = node.hidden;
        if (matches) shown += 1;
      });
      noMatch.hidden = shown > 0;
      syncSelect();
    };
    input.addEventListener("input", refresh);
    if (select) select.addEventListener("change", syncSelect);
    filters.push(refresh);
  }

  function renderEnrichments() {
    const container = document.getElementById("enrichments");
    data.enrichment_types.forEach((type) => {
      const fieldset = el("fieldset", {}, [el("legend", { textContent: type.name })]);
      const items = [];
      let select = null;
      if (type.enrichments.length === 0) {
        fieldset.append(el("p", { textContent: "No options available." }));
      } else if (type.volunteer_interaction === "select") {
        // A blank choice submits nothing for this type.
        select = el("select", { name: "enrichment-select" });
        select.setAttribute("aria-label", type.name);
        select.append(el("option", { value: "", textContent: "" }));
        type.enrichments.forEach((en) => {
          const option = el("option", { value: en.id, textContent: enrichmentLabel(en) });
          select.append(option);
          items.push({
            node: option, text: searchText(en), isOption: true, isSelected: () => option.selected,
          });
        });
        fieldset.append(select);
      } else {
        const choices = el("div", { className: "choices" });
        type.enrichments.forEach((en) => {
          const label = checkbox("enrichment", en.id, enrichmentLabel(en));
          const box = label.querySelector("input");
          choices.append(label);
          items.push({
            node: label, text: searchText(en), isOption: false, isSelected: () => box.checked,
          });
        });
        fieldset.append(choices);
      }
      const hasTextField = type.fragment_types.some((ft) => ft.content_type === "text");
      if (hasTextField && items.length > 0) addFilter(fieldset, type, items, select);
      container.append(fieldset);
    });
  }

  function applySelections(windowIds, enrichmentIds) {
    const windows = new Set(windowIds);
    const enrichments = new Set(enrichmentIds);
    form.querySelectorAll('input[name="window"]').forEach((box) => {
      box.checked = windows.has(box.value);
    });
    form.querySelectorAll('input[name="enrichment"]').forEach((box) => {
      box.checked = enrichments.has(box.value);
    });
    form.querySelectorAll('select[name="enrichment-select"]').forEach((select) => {
      const saved = Array.from(select.options).find((o) => enrichments.has(o.value));
      select.selectedIndex = saved ? saved.index : 0;
    });
    filters.forEach((refresh) => refresh());
  }

  function collectSelections() {
    const checked = (name) =>
      Array.from(form.querySelectorAll('input[name="' + name + '"]:checked'), (b) => b.value);
    const selects = Array.from(
      form.querySelectorAll('select[name="enrichment-select"]'), (s) => s.value
    ).filter(Boolean);
    return { window_ids: checked("window"), enrichment_ids: checked("enrichment").concat(selects) };
  }

  // Errors also open a dialog; nothing the volunteer entered is touched.
  const errorDialog = document.getElementById("form-error-dialog");
  let errorFocus = null;
  document.getElementById("form-error-ok").addEventListener("click", () => errorDialog.close());
  errorDialog.addEventListener("close", () => {
    if (errorFocus) errorFocus.focus();
    errorFocus = null;
  });

  function setStatus(message, isError, focusAfter) {
    statusEl.textContent = message;
    statusEl.classList.toggle("error", Boolean(isError));
    if (isError) {
      document.getElementById("form-error-message").textContent = message.charAt(0).toUpperCase() + message.slice(1);
      errorFocus = focusAfter || null;
      errorDialog.showModal();
    }
  }

  function requireEmail() {
    const email = emailInput.value.trim();
    if (!email || !emailInput.checkValidity()) {
      setStatus("Enter a valid email address.", true, emailInput);
      return null;
    }
    return email;
  }

  async function send(url, options) {
    buttons.forEach((b) => (b.disabled = true));
    try {
      const resp = await fetch(url, options);
      const body = await resp.json().catch(() => ({}));
      return { status: resp.status, body: body };
    } catch (err) {
      return { status: 0, body: { error: "Could not reach the server." } };
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  async function load() {
    const email = requireEmail();
    if (!email) return;
    setStatus("Loading…");
    const { status, body } = await send(offerUrl + "?" + new URLSearchParams({ email: email }));
    if (status === 200) {
      nameInput.value = body.name;
      applySelections(body.window_ids, body.enrichment_ids);
      setStatus("Loaded your existing sign-up.");
    } else if (status === 404) {
      // Reset like the selections, so a previously loaded name isn't carried over.
      nameInput.value = "";
      applySelections([], []);
      setStatus("No sign-up found for that email. Make your selections and submit to create one.");
    } else {
      setStatus(body.error || "Could not load your sign-up.", true);
    }
  }

  async function submit(event) {
    event.preventDefault();
    const email = requireEmail();
    if (!email) return;
    setStatus("Saving…");
    const payload = Object.assign(
      { email: email, name: nameInput.value.trim() }, collectSelections()
    );
    const { status, body } = await send(offerUrl, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (status === 200 || status === 201) {
      nameInput.value = body.name;
      applySelections(body.window_ids, body.enrichment_ids);
      setStatus(status === 201 ? "Thanks! Your sign-up was created." : "Your sign-up was updated.");
    } else {
      setStatus(body.error || "Could not save your sign-up.", true);
    }
  }

  renderWindows();
  renderEnrichments();
  document.getElementById("load").addEventListener("click", load);
  form.addEventListener("submit", submit);
})();
