// The UI is a plain client of GET /events — the same endpoint any external
// consumer would call. It holds no event data of its own and does no
// normalisation; everything rendered here arrives already normalised.

const form = document.getElementById("search");
const submit = document.getElementById("submit");
const statusEl = document.getElementById("status");
const results = document.getElementById("results");

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search();
});

async function search() {
  const location = document.getElementById("location").value.trim();
  const days = document.getElementById("days").value;
  if (!location) return;

  setBusy(true);
  setStatus(`Searching for events near ${location}…`);
  results.replaceChildren();

  try {
    const url = `/events?location=${encodeURIComponent(location)}&days=${encodeURIComponent(days)}`;
    const response = await fetch(url);
    const body = await response.json().catch(() => null);

    if (!response.ok) {
      // The API returns {"detail": "..."} for provider failures and validation
      // errors alike, so a failed upstream shows a readable sentence rather
      // than a blank page.
      throw new Error(readError(body, response.status));
    }

    render(body);
  } catch (err) {
    setStatus(err.message || "Something went wrong. Please try again.", true);
  } finally {
    setBusy(false);
  }
}

function readError(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  // FastAPI validation errors arrive as a list of objects.
  if (Array.isArray(detail) && detail.length && detail[0].msg) {
    return `That search isn't valid: ${detail[0].msg}.`;
  }
  return `The server returned an error (${status}). Please try again.`;
}

function render(data) {
  if (!data.events.length) {
    setStatus(`No events found near ${data.location} in the next ${data.days} day(s).`);
    return;
  }
  setStatus(`${data.count} event${data.count === 1 ? "" : "s"} near ${data.location}.`);
  results.append(...data.events.map(card));
}

function card(event) {
  const li = document.createElement("li");
  li.className = "event";
  const needsAttention = ["cancelled", "postponed", "rescheduled"].includes(event.status);
  if (needsAttention) li.classList.add("alert");
  if (event.status === "cancelled") li.classList.add("cancelled");

  li.append(el("div", "when", formatWhen(event)));

  const title = el("h2", "title");
  title.append(event.url ? link(event.url, event.name) : text(event.name));
  li.append(title);

  li.append(el("div", "where", formatVenue(event.venue)));

  // A moved show is the single most decision-relevant fact on the card, so the
  // original date is spelled out rather than implied by a badge alone.
  if (event.previous_date && event.status !== "cancelled") {
    li.append(el("span", "moved", `Moved from ${formatDate(event.previous_date)}`));
  }

  const meta = el("div", "meta");
  if (needsAttention) meta.append(el("span", "badge", event.status));
  if (event.headliner) meta.append(el("span", "tag", event.headliner));
  event.genres.slice(0, 3).forEach((g) => meta.append(el("span", "tag", g)));
  meta.append(priceEl(event.price_range));
  if (event.ticket_url && event.status !== "cancelled") {
    const a = link(event.ticket_url, "Tickets →");
    a.className = "tickets";
    meta.append(a);
  }
  li.append(meta);
  return li;
}

function priceEl(range) {
  // Absent pricing is stated plainly. JamBase publishes a price on only a
  // minority of ticket offers, and showing $0 or a guess would be worse than
  // showing nothing.
  if (!range) return el("span", "price none", "Price not published");
  const { min_price, max_price, currency } = range;
  const label =
    min_price === max_price
      ? money(min_price, currency)
      : `${money(min_price, currency)}–${money(max_price, currency)}`;
  return el("span", "price", label);
}

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 0,
    }).format(Number(amount));
  } catch {
    return `${amount} ${currency}`;
  }
}

function formatWhen(event) {
  const when = formatDate(event.event_date);
  if (!event.event_time) return `${when} · time TBA`;
  const [h, m] = event.event_time.split(":");
  const d = new Date(2000, 0, 1, Number(h), Number(m));
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${when} · ${time}`;
}

function formatDate(iso) {
  // Parsed as parts, not via Date(iso), which would treat a bare date as UTC
  // and can render the previous day west of Greenwich.
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function formatVenue(venue) {
  const place = [venue.city, venue.region].filter(Boolean).join(", ");
  return place ? `${venue.name} · ${place}` : venue.name;
}

// --- tiny DOM helpers; textContent everywhere, so provider strings are never
// --- interpreted as markup.
function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}
function text(value) {
  return document.createTextNode(value);
}
function link(href, label) {
  const a = document.createElement("a");
  a.href = href;
  a.textContent = label;
  a.rel = "noopener noreferrer";
  a.target = "_blank";
  return a;
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}
function setBusy(busy) {
  submit.disabled = busy;
  submit.textContent = busy ? "Searching…" : "Search";
}

search();
