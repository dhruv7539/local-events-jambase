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
  const where = data.resolved_location || data.location;
  if (!data.events.length) {
    setStatus(`No events found near ${where} in the next ${data.days} day(s).`);
    return;
  }
  setStatus(completenessMessage(data, where));
  results.append(...data.events.map(card));
}

// The API reports how many events it returned and how many exist. Say which
// case this is, rather than presenting a truncated page as the whole answer.
function completenessMessage(data, where) {
  const n = data.returned_count;
  const total = data.total_available;
  const plural = n === 1 ? "" : "s";

  if (typeof total !== "number") {
    // The provider gave no trustworthy total, so we don't invent one.
    return `Showing ${n} event${plural} near ${where}. The provider did not report a total, so there may be more.`;
  }
  if (n < total) {
    return `Showing the first ${n} of ${total} upcoming events near ${where}.`;
  }
  return `${n} event${plural} near ${where}.`;
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

  li.append(el("div", "where", formatVenue(event)));

  // A moved show is the single most decision-relevant fact on the card, so the
  // original date is spelled out rather than implied by a badge alone.
  if (event.previous_date && event.status !== "cancelled") {
    li.append(el("span", "moved", `Moved from ${formatDate(event.previous_date)}`));
  }

  const meta = el("div", "meta");
  if (needsAttention) meta.append(el("span", "badge", event.status));
  // JamBase names events "{artist} at {venue}", so the headliner is usually
  // already in the title. Repeating it as a pill is noise, and a pill that
  // looks identical to a genre pill actively misleads. Show it only when the
  // title doesn't already say it.
  if (event.headliner && !titleMentions(event.name, event.headliner)) {
    meta.append(el("span", "tag artist", event.headliner));
  }
  event.genres.slice(0, 3).forEach((g) => meta.append(el("span", "tag", g)));
  // A price for a show that isn't happening is noise at best.
  if (event.status !== "cancelled") meta.append(priceEl(event.price_range));
  if (event.ticket_url && event.status !== "cancelled") {
    const a = link(event.ticket_url, "Tickets →");
    a.className = "tickets";
    meta.append(a);
  }
  li.append(meta);
  return li;
}

// JamBase is inconsistent about dashes and casing between an event's title and
// its performer name ("... - A Tribute to ..." vs "... - A Tribute To ..."),
// so a plain substring test misses real duplicates. Compare on letters and
// digits only.
function titleMentions(title, name) {
  const flatten = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return flatten(title).includes(flatten(name));
}

// Format from the browser's declared language, not the OS regional format.
// Passing `undefined` uses the system locale, which can disagree: on a Mac
// reporting navigator.language "en-US" but a UK regional format, every price
// rendered "US$15" and every showtime "19:00". Found by opening the page in a
// real browser — headless Chrome hid it behind its own default locale.
const LOCALE = navigator.language || "en-US";

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
    return new Intl.NumberFormat(LOCALE, {
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
  const time = d.toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit" });
  return `${when} · ${time}`;
}

function formatDate(iso) {
  // Parsed as parts, not via Date(iso), which would treat a bare date as UTC
  // and can render the previous day west of Greenwich.
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(LOCALE, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function formatVenue(event) {
  const venue = event.venue;
  const place = [venue.city, venue.region].filter(Boolean).join(", ");
  // The event title already ends with the venue name in JamBase's data, so
  // repeating it wastes the most scannable line on the card. Show the venue
  // name only when the title doesn't already carry it.
  const name = event.name.includes(venue.name) ? "" : venue.name;
  return [name, place].filter(Boolean).join(" · ") || venue.name;
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
