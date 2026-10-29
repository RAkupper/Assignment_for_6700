"use strict";

const form = document.getElementById("question-form");
const question = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const status = document.getElementById("request-status");
const error = document.getElementById("error");
const readiness = document.getElementById("readiness");
const results = document.getElementById("results");
const sourcesPanel = document.getElementById("sources");

async function readResponse(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : `Request failed (HTTP ${response.status}).`;
    throw new Error(message);
  }
  return body;
}

async function checkReadiness() {
  readiness.disabled = true;
  try {
    await readResponse(await fetch("/ready", { signal: AbortSignal.timeout(20000) }));
    readiness.textContent = "● Service ready";
    readiness.dataset.ready = "true";
    readiness.title = "Model and retrieval dependencies passed readiness checks. Click to refresh.";
  } catch (failure) {
    readiness.textContent = "○ Check service";
    readiness.dataset.ready = "false";
    readiness.title = failure.message;
  } finally {
    readiness.disabled = false;
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderAnswer(text, sourceCount) {
  const target = document.getElementById("answer");
  target.replaceChildren();
  let start = 0;
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    target.append(document.createTextNode(text.slice(start, match.index)));
    const number = Number(match[1]);
    if (number >= 1 && number <= sourceCount) {
      const link = element("a", "citation", match[0]);
      link.href = `#source-${number}`;
      link.setAttribute("aria-label", `View source ${number}`);
      link.addEventListener("click", () => {
        const card = document.getElementById(`source-${number}`);
        card.querySelector("details").open = true;
        card.focus({ preventScroll: true });
      });
      target.append(link);
    } else target.append(document.createTextNode(match[0]));
    start = match.index + match[0].length;
  }
  target.append(document.createTextNode(text.slice(start)));
}

function renderSources(sources) {
  sourcesPanel.replaceChildren();
  document.getElementById("source-count").textContent = `${sources.length} ${sources.length === 1 ? "passage" : "passages"}`;
  if (!sources.length) sourcesPanel.append(element("p", "hint", "No matching evidence passed the search filters."));
  sources.forEach((source, index) => {
    const number = index + 1;
    const card = element("article", "source-card");
    card.id = `source-${number}`;
    card.tabIndex = -1;
    const title = element("h3", "source-title");
    const citation = source.url ? element("a", "", source.provision_id) : element("span", "", source.provision_id);
    if (source.url) Object.assign(citation, { href: source.url, target: "_blank", rel: "noopener" });
    title.append(element("span", "source-number", `[${number}]`), citation);
    card.append(title, element("p", "source-meta", `${source.source} · ${source.authority} · status now: ${source.status}`));
    card.append(element("p", "source-heading", source.heading_path.split(" > ").slice(-3).join(" › ")));
    const details = document.createElement("details");
    details.append(element("summary", "", "Read evidence"));
    if (source.parent_text) details.append(element("p", "parent-text", source.parent_text));
    details.append(element("p", "source-text", source.text));
    card.append(details);
    sourcesPanel.append(card);
  });
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!question.value.trim()) { question.focus(); return; }
  error.hidden = true;
  results.hidden = true;
  document.getElementById("empty-state").hidden = true;
  askButton.disabled = true;
  status.textContent = "Finding evidence and writing a cited answer. This may take a minute…";
  const payload = { question: question.value.trim() };
  const visa = document.getElementById("visa").value;
  const date = document.getElementById("as-of").value;
  if (visa) payload.visa_type = visa;
  if (date) payload.as_of = date;
  try {
    const body = await readResponse(await fetch("/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: AbortSignal.timeout(250000),
    }));
    renderSources(body.sources);
    renderAnswer(body.answer, body.sources.length);
    document.getElementById("answer-heading").textContent = body.status === "answered"
      ? "Here is what the sources say." : "Review the available evidence.";
    const hasReferences = document.querySelector("#answer .citation") !== null;
    document.getElementById("answer-note").textContent = body.status === "insufficient_evidence"
      ? "The index did not return enough evidence for this question."
      : hasReferences ? "Select a numbered citation to inspect its source passage."
      : "The model did not add inline citations. Retrieved evidence is listed alongside for review.";
    results.hidden = false;
    status.textContent = "";
  } catch (failure) {
    error.textContent = failure.name === "TimeoutError" ? "The answer took too long. Please try again." : failure.message;
    error.hidden = false;
    status.textContent = "";
  } finally {
    askButton.disabled = false;
  }
});

document.querySelectorAll("[data-question]").forEach(button => button.addEventListener("click", () => {
  question.value = button.dataset.question;
  question.focus();
}));
readiness.addEventListener("click", checkReadiness);
checkReadiness();
