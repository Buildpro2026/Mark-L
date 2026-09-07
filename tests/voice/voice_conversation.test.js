/**
 * Acceptance tests for the JARVIS browser voice engine.
 * Loads the real index.html in jsdom, stubs SpeechRecognition + fetch,
 * and drives the state machine the way a person actually talks.
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const HTML = require("path").resolve(__dirname, "../../core/headless/ui_static/index.html");

const sent = [];          // everything that reached window.sendMessage
let spokenCancelled = 0;  // times JARVIS's audio was hard-stopped

function makeDom() {
  sent.length = 0;
  spokenCancelled = 0;

  const dom = new JSDOM(fs.readFileSync(HTML, "utf8"), {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://jarvis-headless-core.onrender.com/ui",
    beforeParse(window) {
      // ── stub SpeechRecognition ──────────────────────────────────────
      class FakeRecognition {
        constructor() { this.running = false; window.__rec = this; }
        start() {
          if (this.running) throw new Error("already started");
          this.running = true;
          setTimeout(() => this.onstart && this.onstart(), 0);
        }
        stop() {
          if (!this.running) return;
          this.running = false;
          setTimeout(() => this.onend && this.onend(), 0);
        }
        abort() { this.stop(); }
        // helper: emit a result the way Chrome does
        emit(text, isFinal) {
          const res = [{ 0: { transcript: text }, isFinal, length: 1 }];
          res.resultIndex = 0;
          this.onresult && this.onresult({ resultIndex: 0, results: res });
        }
      }
      window.SpeechRecognition = FakeRecognition;

      // ── stub speech output ──────────────────────────────────────────
      window.speechSynthesis = {
        speak(u) { setTimeout(() => u.onstart && u.onstart(), 0); window.__utt = u; },
        cancel() { spokenCancelled++; },
        resume() {},
        getVoices() { return [{ name: "Test", lang: "en-US" }]; },
      };
      window.SpeechSynthesisUtterance = function (t) { this.text = t; };

      // ── stub network ────────────────────────────────────────────────
      window.fetch = async (url, opts) => {
        const u = String(url);
        let body = {};
        if (u.includes("/tts/speak")) body = { configured: false };
        else if (u.includes("/ui/session")) body = { authenticated: true };
        else if (u.includes("/chat")) body = { reply: "Acknowledged.", tool_calls: [] };
        return {
          ok: true, status: 200,
          json: async () => body,
          text: async () => JSON.stringify(body),
          headers: { get: () => "application/json" },
        };
      };
      window.EventSource = function () { this.close = () => {}; };
      window.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
      window.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
      window.HTMLMediaElement.prototype.pause = function () {};
    },
  });

  const w = dom.window;
  // sendMessage is defined by the page; wrap it so tests see submissions
  // without depending on the page's own network path.
  w.eval(`
    window.sendMessage = async function (text) {
      window.__sent = window.__sent || [];
      window.__sent.push(text);
      if (window.speakReply) window.speakReply("Acknowledged.");
    };
  `);
  return w;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function check(name, cond, detail) {
  results.push({ name, pass: !!cond, detail: detail || "" });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function run() {
  // ═══ TEST B: long multi-clause directive arrives intact ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 300;
    w.jarvisVoiceConfig.continuationGraceMs = 500;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    const rec = w.__rec;

    // Spoken in clauses with real thinking pauses between them.
    rec.emit("review the recruiting pipeline", true);
    await sleep(200);                                  // pause mid-thought
    rec.emit(" find the highest priority candidates and", true);
    await sleep(400);   // trailing "and" -> continuation grace, must NOT submit
    check("B1 long directive not submitted during pauses",
      (w.__sent || []).length === 0,
      `sent=${JSON.stringify(w.__sent || [])}`);
    rec.emit(" give me the five strongest matches", true);
    await sleep(600);                                  // now, real silence

    const got = (w.__sent || [])[0] || "";
    check("B2 whole directive submitted as one message", (w.__sent || []).length === 1,
      `count=${(w.__sent || []).length}`);
    check("B3 directive intact",
      got.includes("review the recruiting pipeline") &&
      got.includes("highest priority candidates") &&
      got.includes("five strongest matches"),
      JSON.stringify(got));
    w.close();
  }

  // ═══ TEST A: continuous conversation, no wake word after the first ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.jarvisVoiceConfig.conversationTimeoutMs = 5000;

    // Arm wake mode, then say the wake word once.
    w.document.getElementById("orb-wake-toggle").click();
    await sleep(60);
    let rec = w.__rec;
    rec.emit("jarvis check my recruiting pipeline", true);
    await sleep(400);
    check("A1 wake word starts the turn and keeps the rest of the sentence",
      (w.__sent || []).length === 1 && /check my recruiting pipeline/.test(w.__sent[0]),
      JSON.stringify(w.__sent));

    // Follow-up with NO wake word.
    await sleep(150);
    rec = w.__rec;
    rec.emit("now look at the candidates we haven't contacted", true);
    await sleep(400);
    check("A2 follow-up needs no wake word",
      (w.__sent || []).length === 2 && /candidates we haven't contacted/.test(w.__sent[1] || ""),
      JSON.stringify(w.__sent));

    // Third turn, still no wake word.
    await sleep(150);
    rec = w.__rec;
    rec.emit("prioritize anyone above ninety percent", true);
    await sleep(400);
    check("A3 third turn still needs no wake word",
      (w.__sent || []).length === 3 && /ninety percent/.test(w.__sent[2] || ""),
      JSON.stringify(w.__sent));
    check("A4 conversation state exposed",
      typeof w.jarvisVoiceState === "string", w.jarvisVoiceState);
    w.close();
  }

  // ═══ TEST C: barge-in while JARVIS is speaking ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    let rec = w.__rec;
    rec.emit("what is on my calendar", true);
    await sleep(400);

    // JARVIS is now speaking (stubbed synthesis fired onstart).
    const speakingState = w.jarvisVoiceState;
    const before = spokenCancelled;
    rec = w.__rec;
    rec.emit("actually cancel that and call marcus instead", false); // interim!
    await sleep(60);
    check("C1 state was SPEAKING before the interruption",
      speakingState === "SPEAKING", speakingState);
    check("C2 interruption hard-stops JARVIS's audio",
      spokenCancelled > before, `cancels ${before} -> ${spokenCancelled}`);
    check("C3 mic switched back to listening",
      w.jarvisVoiceState === "LISTENING", w.jarvisVoiceState);

    rec.emit("actually cancel that and call marcus instead", true);
    await sleep(500);
    const last = (w.__sent || [])[(w.__sent || []).length - 1] || "";
    check("C4 the interrupting instruction reached JARVIS",
      /call marcus/.test(last), JSON.stringify(last));
    check("C5 no duplicate submission from interim + final",
      (w.__sent || []).length === 2, `count=${(w.__sent || []).length}`);
    w.close();
  }

  // ═══ TEST D: echo suppression (JARVIS must not interrupt himself) ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    let rec = w.__rec;
    rec.emit("say something", true);
    await sleep(400);
    const before = spokenCancelled;
    // The mic hears JARVIS's own reply coming back through the speakers.
    w.__rec.emit("acknowledged", false);
    await sleep(60);
    check("D1 JARVIS's own voice does not trigger barge-in",
      spokenCancelled === before, `cancels ${before} -> ${spokenCancelled}`);
    w.close();
  }

  
  // ═══ TEST E: self-listening — JARVIS's own audio must never become a turn ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.jarvisVoiceConfig.continuationGraceMs = 300;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    const rec = w.__rec;

    const reply = "I reviewed the recruiting pipeline this morning and there are four candidates above the ninety percent threshold worth your attention";
    w.speakReply(reply);
    await sleep(80);

    // The mic hears JARVIS, in fragments, exactly as Chrome transcribes
    // speaker bleed. None of it may reach sendMessage.
    rec.emit("i reviewed the recruiting pipeline this morning", true);
    await sleep(100);
    rec.emit(" and there are four candidates above the ninety percent threshold", true);
    await sleep(500);

    check("E1 JARVIS's own speech creates no user turn",
      (w.__sent || []).length === 0,
      `sent=${JSON.stringify(w.__sent || [])}`);
    check("E2 JARVIS was not interrupted by himself",
      spokenCancelled <= 1, `cancels=${spokenCancelled}`);
  }

  // ═══ TEST F: trailing echo AFTER playback ends is still not a turn ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    const rec = w.__rec;

    w.speakReply("the strongest candidate is available starting monday");
    await sleep(60);
    if (w.__utt && w.__utt.onend) w.__utt.onend();   // playback finished
    await sleep(30);
    // Recognition finalises the tail only now — still JARVIS's words.
    rec.emit("is available starting monday", true);
    await sleep(500);

    check("F1 late-finalising echo after playback creates no turn",
      (w.__sent || []).length === 0,
      `sent=${JSON.stringify(w.__sent || [])}`);
  }

  // ═══ TEST G: real barge-in still wins while JARVIS speaks ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    const rec = w.__rec;

    w.speakReply("here is the full recruiting summary for this morning");
    await sleep(80);
    const before = spokenCancelled;
    rec.emit("stop", true);                     // one word, deliberate
    await sleep(60);
    check("G1 a one-word interruption still stops JARVIS",
      spokenCancelled > before, `cancels ${before} -> ${spokenCancelled}`);

    rec.emit("call marcus about the superintendent role instead", true);
    await sleep(500);
    const sent = w.__sent || [];
    check("G2 the interrupting request is what gets sent",
      sent.length === 1 && sent[0].includes("marcus"),
      JSON.stringify(sent));
  }

  // ═══ TEST H: cancel, then immediately a new request ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    const rec = w.__rec;

    w.speakReply("reviewing the pipeline now");
    await sleep(60);
    rec.emit("wait", true);                      // barge-in
    await sleep(40);
    rec.emit("show me the client list instead", true);
    await sleep(500);

    const sent = w.__sent || [];
    check("H1 exactly one request after cancel-then-ask",
      sent.length === 1, `sent=${JSON.stringify(sent)}`);
    // The invariant is that the CANCELLED reply cannot come back — not that
    // nothing is speaking, since answering the new request is correct.
    check("H2 the cancelled response does not resume behind the new one",
      !(w.__jarvisSpokenText || "").includes("reviewing the pipeline"),
      `spoken="${w.__jarvisSpokenText}"`);
  }

  // ═══ TEST I: a background/autonomous event during speech is not a turn ═══
  {
    const w = makeDom();
    await sleep(200);
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);

    w.speakReply("three drafts are ready for your review");
    await sleep(60);
    // An autonomous notification speaks; it must not become a user turn.
    w.speakReply("a new candidate inquiry just arrived");
    await sleep(500);

    check("I1 autonomous speech creates no user turn",
      (w.__sent || []).length === 0,
      `sent=${JSON.stringify(w.__sent || [])}`);
  }

  // ═══ TEST J: recogniser restart (session recovery) keeps the guard ═══
  {
    const w = makeDom();
    await sleep(200);
    w.jarvisVoiceConfig.endpointSilenceMs = 200;
    w.document.getElementById("orb-mic-btn").click();
    await sleep(60);
    let rec = w.__rec;

    w.speakReply("the pipeline review is complete for today");
    await sleep(60);
    rec.stop();                     // Chrome ends the session mid-reply
    await sleep(400);               // page restarts it
    rec = w.__rec;
    rec.emit("the pipeline review is complete for today", true);
    await sleep(500);

    check("J1 echo after a recogniser restart still creates no turn",
      (w.__sent || []).length === 0,
      `sent=${JSON.stringify(w.__sent || [])}`);
  }

const failed = results.filter(r => !r.pass);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

run().catch(e => { console.error("HARNESS ERROR", e); process.exit(2); });
