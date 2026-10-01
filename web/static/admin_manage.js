// Manage page: ordering and filtering volunteers' sign-ups.
//
// Enrichment columns arrive as generic descriptors {key, label, kind}; this
// script knows how to filter, show and order each *kind* of value and nothing
// about how any column is computed. New columns need no changes here as long
// as their kind is one of KIND below.
//
// Ordering (the Sort panel, for all windows): every sort criterion can be set
// to None, To Top or To Bottom. Criteria are each boolean enrichment column,
// plus each possible response value, as listed by the API (a sign-up with no
// response matches none of them). A sign-up scores +1 for each To Top criterion
// that is true for it and -1 for each To Bottom one; higher scores come first,
// and ties keep the order the API returned.
(function () {
  "use strict";

  const columns = JSON.parse(document.getElementById("manage-columns").textContent);
  const responses = JSON.parse(document.getElementById("manage-responses").textContent);
  const eventId = JSON.parse(document.getElementById("manage-event").textContent).id;

  // ---------------------------------------------------------------- kinds
  const KIND = {
    boolean: {
      orderable: true, // can be moved to top / bottom when true
      filter: { type: "select", options: [["", "Any"], ["true", "Yes"], ["false", "No"]],
                test: (v, f) => String(Boolean(v)) === f },
    },
    text: {
      filter: { type: "text", test: (v, f) => String(v ?? "").toLocaleLowerCase().includes(f.toLocaleLowerCase()) },
    },
    number: { filter: null },
  };
  // "pending" is the absence of a response; the rest come from the API.
  const STATUS = [["", "All"], ["pending", "Pending"], ...responses.map((r) => [r.value, r.label])];
  const PLACEMENTS = [["none", "None"], ["top", "To Top"], ["bottom", "To Bottom"]];
  const WEIGHT = { none: 0, top: 1, bottom: -1 };

  // ---------------------------------------------------------------- headings
  const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  document.querySelectorAll(".window-heading, .email-window").forEach((h) => {
    const start = new Date(h.dataset.start);
    const end = new Date(h.dataset.end);
    const sameDay = start.toDateString() === end.toDateString();
    h.textContent = `${dayFmt.format(start)} · ${timeFmt.format(start)} – ${sameDay ? timeFmt.format(end) : `${dayFmt.format(end)} ${timeFmt.format(end)}`}`;
  });

  // ---------------------------------------------------------------- remembered view
  // Kept for this browser session, so the reload after accepting or denying
  // keeps the admin's current view.
  function load(key) {
    try {
      return JSON.parse(sessionStorage.getItem(key) || "{}");
    } catch (e) {
      return {};
    }
  }
  function save(key, value) {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      /* storage unavailable: the view just isn't remembered */
    }
  }
  const SORT_KEY = `manage-sort:${eventId}`;
  const filterKey = (windowId) => `manage-filters:${eventId}:${windowId}`;

  // ---------------------------------------------------------------- helpers
  function el(tag, props, children) {
    const node = Object.assign(document.createElement(tag), props || {});
    (children || []).forEach((c) => node.append(c));
    return node;
  }
  let uid = 0;
  const nextId = () => `ctl-${++uid}`;

  function labelled(text, control) {
    control.id = nextId();
    return el("span", { className: "control" }, [el("label", { htmlFor: control.id, textContent: text }), control]);
  }

  function select(options, value) {
    const s = el("select");
    options.forEach(([v, t]) => s.append(el("option", { value: v, textContent: t })));
    s.value = value ?? "";
    if (s.value !== (value ?? "")) s.value = options[0][0]; // stale saved value
    return s;
  }

  // ---------------------------------------------------------------- windows
  const windows = Array.from(document.querySelectorAll(".manage-window"))
    .filter((section) => section.querySelector(".signup-list"))
    .map((section) => ({
      section,
      list: section.querySelector(".signup-list"),
      items: Array.from(section.querySelector(".signup-list").children).map((li) => ({
        li,
        order: Number(li.dataset.order), // position in the API's response
        response: li.dataset.response,
        values: JSON.parse(li.querySelector(".signup-values").textContent),
      })),
    }));

  // ---------------------------------------------------------------- sort panel
  const placement = load(SORT_KEY); // {criterion key: "none" | "top" | "bottom"}
  // Each criterion says whether it's true for a sign-up.
  const criteria = [
    ...columns
      .filter((c) => KIND[c.kind] && KIND[c.kind].orderable)
      .map((c) => ({ key: c.key, label: c.label, test: (item) => Boolean(item.values[c.key]) })),
    // From the sign-up's response, not an enrichment; no response matches none.
    ...responses.map((r) => ({ key: `response:${r.value}`, label: r.label, test: (item) => item.response === r.value })),
  ];
  const sortPanel = document.getElementById("manage-sort-controls");
  document.getElementById("manage-sort-empty").hidden = criteria.length > 0;

  criteria.forEach((c) => {
    const current = WEIGHT[placement[c.key]] !== undefined ? placement[c.key] : "none";
    const name = nextId();
    const fieldset = el("fieldset", { className: "sort-choice" }, [el("legend", { textContent: c.label })]);
    PLACEMENTS.forEach(([value, text]) => {
      const radio = el("input", { type: "radio", name, value, checked: value === current, id: nextId() });
      radio.addEventListener("change", () => {
        placement[c.key] = value;
        save(SORT_KEY, placement);
        applyAll();
      });
      fieldset.append(el("label", { className: "choice", htmlFor: radio.id }, [radio, " ", text]));
    });
    sortPanel.append(fieldset);
  });

  function score(item) {
    return criteria.reduce((sum, c) => sum + (c.test(item) ? WEIGHT[placement[c.key]] || 0 : 0), 0);
  }

  // ---------------------------------------------------------------- per-window filters
  windows.forEach((w) => {
    const saved = load(filterKey(w.section.dataset.windowId));
    w.status = select(STATUS, saved.status);
    w.filters = [];
    columns.forEach((c) => {
      const spec = KIND[c.kind] && KIND[c.kind].filter;
      if (!spec) return;
      const value = (saved.filters || {})[c.key] ?? "";
      const control = spec.type === "select"
        ? select(spec.options, value)
        : el("input", { type: "search", value, placeholder: "contains…" });
      w.filters.push([c, spec, control]);
    });
    w.count = el("span", { className: "hint shown-count" });
    const controls = w.section.querySelector(".window-controls");
    controls.append(labelled("Status", w.status));
    w.filters.forEach(([c, , control]) => controls.append(labelled(c.label, control)));
    controls.append(w.count);
    [w.status, ...w.filters.map(([, , control]) => control)].forEach((control) =>
      control.addEventListener(control.tagName === "INPUT" ? "input" : "change", () => apply(w)));
  });

  function apply(w) {
    const sorted = w.items.slice().sort((a, b) => score(b) - score(a) || a.order - b.order);
    let shown = 0;
    sorted.forEach((item) => {
      w.list.append(item.li); // re-append in sorted order
      let visible = !w.status.value || item.response === w.status.value;
      w.filters.forEach(([c, spec, control]) => {
        if (visible && control.value) visible = spec.test(item.values[c.key], control.value);
      });
      item.li.hidden = !visible;
      if (visible) shown += 1;
    });
    w.count.textContent = `Showing ${shown} of ${w.items.length}`;
    w.section.querySelector(".no-match").hidden = shown > 0;
    save(filterKey(w.section.dataset.windowId), {
      status: w.status.value,
      filters: Object.fromEntries(w.filters.map(([c, , control]) => [c.key, control.value])),
    });
  }

  function applyAll() {
    windows.forEach(apply);
  }

  applyAll();

  // ---------------------------------------------------------------- approved email lists
  // Toggle any number of windows; show the approved emails of all selected
  // windows, comma separated, without duplicates (in window order).
  const EMAILS_KEY = `manage-emails:${eventId}`;
  const emailButtons = Array.from(document.querySelectorAll(".email-window"));
  const emailOutput = document.getElementById("email-list-output");
  const selectedWindows = new Set(load(EMAILS_KEY).selected || []);

  function showEmails() {
    const emails = new Set();
    emailButtons.forEach((b) => {
      const on = selectedWindows.has(b.dataset.windowId);
      b.setAttribute("aria-pressed", String(on));
      if (on) JSON.parse(b.dataset.emails).forEach((e) => emails.add(e));
    });
    emailOutput.textContent = emails.size ? Array.from(emails).join(", ")
      : selectedWindows.size ? "No approved volunteers in the selected windows." : "Select one or more windows.";
    emailOutput.classList.toggle("hint", !emails.size);
    save(EMAILS_KEY, { selected: Array.from(selectedWindows) });
  }
  emailButtons.forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.windowId;
    if (selectedWindows.has(id)) selectedWindows.delete(id); else selectedWindows.add(id);
    showEmails();
  }));
  if (emailOutput) showEmails();
})();
