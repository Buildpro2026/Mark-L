# JARVIS phone line — Cartesia Line voice agent

This folder is a **separate deployable** from the Render service. It runs on
Cartesia's infrastructure and is the only part of JARVIS that lives there.

```
   Lee's phone  ──call──▶  Cartesia Line agent  ──HTTPS──▶  jarvis-headless-core
                           (voice_agent/main.py)            (Render — the brain)
        ▲                   ears, mouth, turn-taking         every tool, memory,
        └──── outbound ─────┘  no business logic             audit trail, agents
              (JARVIS calls Lee via actions/cartesia_calls.py)
```

**The split is the point.** Tools, prompt, memory, and provider fallback stay
defined once, in the Render service. Add a tool to
`core/headless/tool_registry.py` and it is reachable by phone immediately —
this agent never needs redeploying for that.

The voice layer runs a fast Groq model purely for conversational glue. Anything
real goes through the `ask_jarvis` tool to `POST /api/voice/ask`, which runs the
same `run_chat_turn()` the browser `/ui` runs.

---

## What each piece does

| Piece | Where it runs | Job |
|---|---|---|
| `voice_agent/main.py` | Cartesia | Phone conversation, calls `ask_jarvis` |
| `core/headless/voice_api.py` | Render | `/api/voice/context`, `/ask`, `/call-ended` |
| `actions/cartesia_tts.py` | Render | Browser `/ui` speaks in the same voice |
| `actions/cartesia_calls.py` | Render | JARVIS places outbound calls |
| `voice_call` tool | Render | Lets JARVIS decide to ring Lee |
| `actions/twilio_integration.py` | Render | **SMS stays here** — Cartesia does voice only |

---

## Setup, in order

### 1. Cartesia API key

Playground → **API Keys** → create one. Keep the tab open; it is needed twice.

### 2. Point the Render service at Cartesia

In the Render dashboard for `jarvis-headless-core` → **Environment**:

```
CARTESIA_API_KEY   = sk_car_...
CARTESIA_VOICE_ID  = <voice id from the playground's Voices page>
```

Save. That alone upgrades the browser `/ui` to the Sonic voice — verify at
`/health`, which now reports `cartesia_voice_configured: true`.

### 3. Deploy the phone agent

From this folder, on a machine with the Cartesia CLI:

```bash
curl -fsSL https://cartesia.sh | sh
cartesia auth login

cd voice_agent
uv sync                # or: uv add cartesia-line httpx
cartesia init          # creates the agent; note the agent_... id it prints
cartesia deploy
```

Then set its secrets (these live on Cartesia, not Render):

```bash
cartesia env set JARVIS_BASE_URL=https://jarvis-headless-core.onrender.com
cartesia env set JARVIS_API_TOKEN=<same token as Render's JARVIS_API_TOKEN>
cartesia env set GROQ_API_KEY=<same Groq key Render uses>
cartesia env set CARTESIA_VOICE_ID=<same voice id as step 2>
```

Test before any phone number exists:

```bash
cartesia chat        # talk to it in the terminal
```

Cartesia can also deploy from a **linked GitHub repo** instead of the CLI —
Agent → Deployments → link `Buildpro2026/Mark-L`, root `voice_agent/`,
branch `feature/jarvis-2`. Same result, and it redeploys on push.

### 4. Phone number

Playground → **Phone Numbers** → get a number (Cartesia sells US numbers
directly; a Twilio number or SIP trunk can be imported instead). Assign it:

```bash
cartesia phone-numbers assign <phone-number-id> --agent-id <agent-id>
```

Call it. That is inbound working.

### 5. Let JARVIS call Lee

Back in Render's environment, add:

```
CARTESIA_AGENT_ID        = agent_...
CARTESIA_PHONE_NUMBER_ID = ap_...
JARVIS_OWNER_PHONE       = +1XXXXXXXXXX
```

Now `voice_call` works, and "call me when the Henderson contract is signed" is a
thing JARVIS can actually do — the reason is spoken as the opening line rather
than a generic greeting.

---

## The one real gotcha: Render's free plan sleeps

A free instance spins down after inactivity and takes ~50 seconds to wake.
Cartesia rings for about five. So the first call after a quiet spell would find
the brain asleep.

Three defenses, in order of how much they actually fix it:

1. **Upgrade the Render service to Starter.** It never sleeps. This is the real fix.
2. **Ping `/health` every 10 minutes** from any uptime checker. Free, and enough
   in practice.
3. Already built in: the agent caches the context and warms the backend on
   startup, and says "let me check" before every lookup, so a slow answer sounds
   like thinking rather than a dropped call.

Without one of the first two, expect the first call of the morning to be rough.

---

## Verifying

```bash
# brain reachable and authorized
curl -H "Authorization: Bearer $JARVIS_API_TOKEN" \
     https://jarvis-headless-core.onrender.com/api/voice/context

# what's configured
curl https://jarvis-headless-core.onrender.com/health | grep cartesia
```

In the running JARVIS, "is voice calling configured?" runs `voice_call/status`
and names any missing variable specifically.

---

## Changing how JARVIS sounds or behaves on the phone

- **Personality / rules:** edit `core/prompt.txt` and push to Render. Next call
  uses it — the agent fetches the prompt per call, cached 5 minutes.
- **Voice-specific speaking rules:** `_VOICE_RULES` in `core/headless/voice_api.py`.
- **Voice:** change `CARTESIA_VOICE_ID` in both places, or set it in the agent's
  dashboard Voice & Language panel.
- **Conversational model:** `cartesia env set VOICE_MODEL=groq/...`.
