// The phone client. One conversation with the counsellor, and a clear state
// between sessions. It talks to the same API as the reference pages.
//
// Identity is the authentication stub the whole demo uses: a fixed bearer
// token, and the episode id kept on the phone so the app carries on with the
// same programme when it is opened again. A real deployment replaces both
// with the operator's sign in; nothing else here changes.

(() => {
  "use strict";

  const TOKEN = "demo-client:client";
  const ENROL_TOKEN = "ui:clinician"; // enrolment is a clinician's action; the demo does it for itself
  const STORE = "cadence.app";
  const DEMO = "cadence.app.demo";

  const $ = (id) => document.getElementById(id);
  const headers = (token) => ({ "Content-Type": "application/json", Authorization: "Bearer " + token });

  let saved = load();
  let awaiting = false;

  // -- storage --------------------------------------------------------------

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch { return {}; }
  }
  function save(patch) {
    saved = { ...saved, ...patch };
    try { localStorage.setItem(STORE, JSON.stringify(saved)); } catch { /* private mode: carry on in memory */ }
  }
  function clear() {
    saved = {};
    try { localStorage.removeItem(STORE); } catch { /* nothing to remove */ }
  }
  function forget() {
    clear();
    location.reload();
  }

  // -- api ------------------------------------------------------------------

  async function api(path, options = {}, token = TOKEN) {
    const response = await fetch(path, { headers: headers(token), ...options });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const error = new Error(detail.detail || `${response.status} from ${path}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  // -- rendering ------------------------------------------------------------

  const escapeHtml = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const paragraphs = (text) => text.split(/\n\s*\n/).map((p) => `<p>${escapeHtml(p.trim())}</p>`).join("");

  function turn(role, text) {
    const el = document.createElement("div");
    el.className = "turn " + role;
    el.innerHTML = `<div class="who">${role === "client" ? "You" : "Counsellor"}</div>` + paragraphs(text);
    $("thread").appendChild(el);
    return el;
  }

  function notice(title, text) {
    const el = document.createElement("div");
    el.className = "notice";
    el.innerHTML = `<div class="who">${escapeHtml(title)}</div>` + paragraphs(text);
    $("thread").appendChild(el);
    return el;
  }

  function engine(text) {
    const el = document.createElement("div");
    el.className = "engine";
    el.textContent = text;
    $("thread").appendChild(el);
  }

  function scrollToEnd() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  function showState(title, body, action) {
    $("state-title").textContent = title;
    $("state-body").textContent = body;
    const button = $("state-action");
    if (action) {
      button.textContent = action.label;
      button.onclick = action.run;
      button.hidden = false;
    } else {
      button.hidden = true;
    }
    $("state").hidden = false;
    $("composer").hidden = true;
    scrollToEnd();
  }

  function showComposer() {
    $("state").hidden = true;
    $("composer").hidden = false;
  }

  function setAwaiting(value) {
    awaiting = value;
    $("reply").disabled = !value;
    $("send").disabled = !value;
    if (value) $("reply").focus({ preventScroll: true });
  }

  // -- progress -------------------------------------------------------------

  const sessionNumber = (id, remaining, done) => {
    // "s02-triggers" is the second ordinary session. Inserted sessions have
    // no number, so they are named by what they are for.
    const m = /^s(\d\d)-/.exec(id || "");
    if (!m) return null;
    const n = Number(m[1]);
    return n >= 90 ? null : n;
  };

  const sessionLabel = (id) => {
    if (!id) return "";
    if (id.startsWith("s90")) return "After a lapse";
    if (id.startsWith("s91")) return "Building confidence";
    const n = sessionNumber(id);
    return n ? `Session ${n}` : id;
  };

  async function refreshProgress() {
    const p = await api(`/episodes/${saved.episode}/progress`);
    const done = p.sessions_completed.length;
    const total = done + p.sessions_remaining.length;
    $("progress-fill").style.width = p.completion_pct + "%";
    $("progress").setAttribute("aria-valuenow", String(p.completion_pct));
    $("progress-label").textContent = done === total
      ? "Programme complete"
      : `${done} of ${total} sessions done`;
    return p;
  }

  // -- the session ----------------------------------------------------------

  function render(result) {
    $("session-name").textContent = sessionLabel(result.session_id || saved.session);
    if (result.escalated) {
      (result.messages || [result.text]).filter(Boolean).forEach((m) => notice("Important", m));
    } else if (result.text) {
      turn("counsellor", result.text);
    }
    (result.notes || []).forEach((n) => engine(n));
    (result.rules_fired || []).forEach((r) => engine("rule fired: " + r));

    if (result.escalated) {
      save({ run: null, session: null });
      showState(
        "We have paused the programme",
        "What you told us matters more than the session plan. Your counsellor will be in touch. If you need help now, contact your doctor or a crisis line."
      );
      return;
    }
    if (result.finished) {
      save({ run: null, session: null });
      engine("session complete");
      refreshProgress().then((p) => {
        const next = p.queued[0] || p.sessions_remaining[0];
        showState(
          "That is the session done",
          next
            ? "Sessions are spaced out on purpose, usually a few days apart. Open the app again in a day or two and the next one will be waiting."
            : "That was the last session in your programme."
        );
      });
      return;
    }
    showComposer();
    setAwaiting(result.awaiting_client);
    scrollToEnd();
  }

  async function send() {
    const text = $("reply").value.trim();
    if (!text || !awaiting) return;
    turn("client", text);
    $("reply").value = "";
    $("reply").style.height = "";
    setAwaiting(false);
    scrollToEnd();
    try {
      render(await api(`/sessions/${saved.run}/turns`, { method: "POST", body: JSON.stringify({ text }) }));
    } catch (error) {
      failed(error);
    }
  }

  async function resume() {
    // The app was closed in the middle of a session. Put the conversation
    // back on the screen and carry on from the counsellor's last question.
    const transcript = await api(`/sessions/${saved.run}/transcript`);
    transcript.forEach((t) => {
      if (t.role === "client") turn("client", t.text);
      else if (t.role === "counsellor") turn("counsellor", t.text);
    });
    $("session-name").textContent = sessionLabel(saved.session);
    showComposer();
    setAwaiting(true);
    scrollToEnd();
  }

  async function openSession() {
    const p = await refreshProgress();
    if (saved.run) {
      await resume();
      return;
    }
    const next = await api(`/episodes/${saved.episode}/next`);
    if (!next.session_id) {
      const upcoming = p.queued[0] || p.sessions_remaining[0];
      showState(
        "Nothing to do today",
        upcoming
          ? `${sessionLabel(upcoming)} is not open yet. Sessions are spaced out on purpose, usually a few days apart. Come back in a day or two.`
          : "Your programme is complete."
      );
      return;
    }
    const started = await api(`/episodes/${saved.episode}/sessions`, { method: "POST", body: JSON.stringify({}) });
    save({ run: started.session_run_id, session: next.session_id });
    render({ ...started, session_id: next.session_id });
  }

  async function enrol() {
    const enrolment = await api(
      "/clients",
      { method: "POST", body: JSON.stringify({ identity: { name: "Phone demo client" }, locale: "en" }) },
      ENROL_TOKEN
    );
    save({ client: enrolment.client_id, episode: enrolment.episode_id, run: null, session: null });
  }

  function failed(error) {
    if (error.status === 404 && saved.episode) {
      // This phone remembers a record the server no longer has. On the demo
      // server that means the database was reset. Nothing here can be resumed.
      showState(
        "Your record is not on this server any more",
        "The programme was reset since you last opened the app. Start again as a new client.",
        { label: "Start again as a new client", run: forget }
      );
    } else if (error.status) {
      showState(
        "The programme hit a problem",
        `The server answered ${error.status}. Try again in a moment. Nothing you have said is lost.`,
        { label: "Try again", run: () => location.reload() }
      );
    } else {
      showState(
        "Cannot reach the programme",
        "Check your connection and try again. Nothing you have said is lost.",
        { label: "Try again", run: () => location.reload() }
      );
    }
    console.error(error);
  }

  // -- wiring ---------------------------------------------------------------

  $("send").addEventListener("click", send);
  $("reply").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("reply").addEventListener("input", (e) => {
    e.target.style.height = "";
    e.target.style.height = Math.min(e.target.scrollHeight, 132) + "px";
  });
  $("forget").addEventListener("click", forget);

  const demoToggle = $("demo-toggle");
  const setDemo = (on) => {
    document.body.classList.toggle("demo", on);
    demoToggle.setAttribute("aria-pressed", String(on));
    try { localStorage.setItem(DEMO, on ? "1" : ""); } catch { /* fine */ }
  };
  demoToggle.addEventListener("click", () => setDemo(!document.body.classList.contains("demo")));
  try { setDemo(localStorage.getItem(DEMO) === "1"); } catch { /* fine */ }

  async function open() {
    if (!saved.episode) await enrol();
    await openSession();
  }

  (async () => {
    setAwaiting(false);
    try {
      await open();
    } catch (error) {
      if (error.status === 404 && saved.episode) {
        // This phone remembers a record the server no longer has. On the demo
        // server that means the database was reset. There is nothing to
        // resume, so do what a first open does, and say so.
        try {
          clear();
          notice("Fresh start", "The programme was reset since you last opened the app, so this is a new record.");
          await open();
          return;
        } catch (again) {
          failed(again);
          return;
        }
      }
      failed(error);
    }
  })();
})();
