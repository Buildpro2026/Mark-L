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
let audioPlayCalls = 0;   // times a <audio> element's play() actually fired

function makeDom(opts) {
  opts = opts || {};
  sent.length = 0;
  spokenCancelled = 0;
  audioPlayCalls = 0;

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
      window.fetch = async (url, fetchOpts) => {
        const u = String(url);
        if (u.includes("/tts/speak") && opts.ttsMode === "delayed-audio") {
          // Simulates a slow /ui/api/tts/speak round-trip (real neural TTS
          // configured) that resolves AFTER an interrupt has already fired
          // client-side — the race this test exists to catch.
          await new Promise((r) => setTimeout(r, opts.ttsDelayMs || 300));
          if (fetchOpts && fetchOpts.signal && fetchOpts.signal.aborted) {
            const err = new Error("aborted");
            err.name = "AbortError";
            throw err;
          }
          const body = { configured: true, ok: true, audio_base64: "AAAA", mime_type: "audio/mpeg" };
          return {
            ok: true, status: 200,
            json: async () => body,
            text: async () => JSON.stringify(body),
            headers: { get: () => "application/json" },
          };
        }
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
      // Only count <audio> playback (the TTS reply) — the avatar's idle
      // <video> autoplays independently and must not pollute this count.
      window.HTMLMediaElement.prototype.play = function () {
        if (this.tagName === "AUDIO") audioPlayCalls++;
        return Promise.resolve();
      };
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

  // ═══ TEST E: interrupting during an in-flight neural-TTS fetch must not
  // let the stale response resume speech afterward (the "keeps talking
  // after interruption, only a refresh stops it" bug) ═══
  {
    const w = makeDom({ ttsMode: "delayed-audio", ttsDelayMs: 300 });
    await sleep(200);

    // Kick off a spoken reply — this starts the (slow) /ui/api/tts/speak
    // round-trip and does not resolve for 300ms.
    w.speakReply("Here is a fairly long update about your pipeline.");
    await sleep(20);   // request is in flight, nothing has played yet
    check("E1 no audio has played yet while the request is in flight",
      audioPlayCalls === 0, `audioPlayCalls=${audioPlayCalls}`);

    // Interrupt arrives well before the network response does.
    w.jarvisStopSpeaking();

    // Let the delayed fetch resolve.
    await sleep(400);
    check("E2 the stale response never starts audio after interruption",
      audioPlayCalls === 0, `audioPlayCalls=${audioPlayCalls}`);
    w.close();
  }

  // ═══ TEST F: a second speakReply() call supersedes a still-pending
  // first one, rather than both eventually racing to play ═══
  {
    const w = makeDom({ ttsMode: "delayed-audio", ttsDelayMs: 300 });
    await sleep(200);

    w.speakReply("First reply, about to be superseded.");
    await sleep(20);
    w.speakReply("Second, newer reply.");
    await sleep(500);   // both delayed fetches have now resolved
    check("F1 exactly one audio element ever plays when a newer reply supersedes an older one",
      audioPlayCalls === 1, `audioPlayCalls=${audioPlayCalls}`);
    w.close();
  }

  // ═══ TEST G: whatever actually reaches the speech engine never contains
  // code/JSON/timestamps — cleanForSpeech runs before both TTS paths ═══
  {
    const w = makeDom();   // default ttsMode -> /tts/speak reports {configured:false} -> falls to browser voice
    await sleep(200);

    await w.speakReply("Here's the fix:\n```js\nfunction foo() { return 1; }\n```\nDone.");
    await sleep(20);
    check("G1 fenced code block never reaches the speech engine",
      w.__utt && !w.__utt.text.includes("function foo"), JSON.stringify(w.__utt && w.__utt.text));

    await w.speakReply('One moment. {"tool":"web_search","arguments":{"query":"x"}} Found it.');
    await sleep(20);
    check("G2 raw JSON tool-call blob never reaches the speech engine",
      w.__utt && !w.__utt.text.includes('"tool"'), JSON.stringify(w.__utt && w.__utt.text));

    await w.speakReply("It happened at 2026-09-01T05:13:00-05:00 today.");
    await sleep(20);
    check("G3 ISO timestamp never reaches the speech engine",
      w.__utt && !w.__utt.text.includes("2026-09-01"), JSON.stringify(w.__utt && w.__utt.text));

    await w.speakReply("I found several products that match what you're looking for.");
    await sleep(20);
    check("G4 ordinary sentence reaches the speech engine unchanged in substance",
      w.__utt && w.__utt.text === "I found several products that match what you're looking for.",
      JSON.stringify(w.__utt && w.__utt.text));
    w.close();
  }

  const failed = results.filter(r => !r.pass);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

run().catch(e => { console.error("HARNESS ERROR", e); process.exit(2); });
