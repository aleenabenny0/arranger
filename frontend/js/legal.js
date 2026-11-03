// Fills the legal pages from /legal/config.
//
// The documents are static HTML so they can be read, reviewed and printed
// without running the app. Only facts that belong to whoever deploys the
// service (who they are, where data is stored, how long files are kept) are
// filled in here, and always with textContent. A detail the operator has not
// provided is shown as not provided; nothing is invented.

const meta = document.querySelector('meta[name="arranger-api"]');
const BASE = ((meta && meta.content) || "").replace(/\/+$/, "");

function lookup(config, path) {
  return path.split(".").reduce((value, key) => (value === null || value === undefined ? value : value[key]), config);
}

function days(n) {
  return n ? `${n} day${n === 1 ? "" : "s"}` : "until you delete them";
}

async function fill() {
  let config;
  try {
    const response = await fetch(`${BASE}/legal/config`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    config = await response.json();
  } catch {
    for (const el of document.querySelectorAll("[data-config]")) el.textContent = "[could not be loaded from the server]";
    return;
  }

  for (const el of document.querySelectorAll("[data-config]")) {
    const value = lookup(config, el.dataset.config);
    if (el.dataset.format === "days") { el.textContent = days(value); continue; }
    if (value === undefined || value === null || value === "") {
      el.textContent = el.dataset.missing || "[not provided by the operator of this service]";
      el.classList.add("missing");
    } else if (el.dataset.format === "mailto") {
      const link = document.createElement("a");
      link.href = `mailto:${encodeURIComponent(String(value)).replace(/%40/g, "@")}`;
      link.textContent = String(value);
      el.replaceChildren(link);
    } else {
      el.textContent = String(value);
    }
  }

  const processors = document.getElementById("processors");
  if (processors) {
    processors.replaceChildren(...config.processors.map((p) => {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = `${p.name}. `;
      item.append(name, document.createTextNode(`${p.purpose}${p.detail ? ` Location: ${p.detail}.` : ""}`));
      return item;
    }));
  }

  const draft = document.getElementById("draft-banner");
  if (draft) draft.hidden = Boolean(config.public_launch);
}

fill();
