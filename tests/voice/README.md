# Browser voice acceptance tests

These drive the real `core/headless/ui_static/index.html` in jsdom with a
stubbed SpeechRecognition, and assert the three behaviors that were broken
before: a long directive surviving mid-sentence pauses, follow-up turns
that need no wake word, and barge-in that actually cuts JARVIS off.

```bash
npm install jsdom
node tests/voice/voice_conversation.test.js
```

Exit code 0 = all checks passed. There is no test framework on purpose —
one file, no dependencies beyond jsdom, runnable anywhere node is.
