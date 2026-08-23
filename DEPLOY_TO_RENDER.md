# QUICK DEPLOY GUIDE — Get JARVIS Live on Render

**Time Required**: 5 minutes

---

## What's Been Done

✓ Anthropic removed completely
✓ Gemini configured as sole provider (free tier)
✓ Orb UI restored and set as default
✓ Obsidian vault integrated into cloud memory
✓ Business dashboard settings configured for 3 apps
✓ Voice/microphone properly wired

---

## What You Need to Do

### 1. Push Code to GitHub (2 minutes)

```bash
# Navigate to the Mark-L directory
cd /path/to/Mark-L

# Stage all changes
git add -A

# Commit with message
git commit -m "fix: Anthropic removal, Gemini sole provider, orb UI restoration, obsidian integration, business settings"

# Push to GitHub
git push origin main
```

**That's it** — Render will auto-deploy when code is pushed (if auto-deploy is enabled).

---

### 2. Configure Gemini API in Render (2 minutes)

Go to https://dashboard.render.com and navigate to your JARVIS service:

1. Click **Settings** (left sidebar)
2. Scroll to **Environment** section
3. Click **Add Environment Variable**
4. Add this variable:
   ```
   GOOGLE_API_KEY = your_free_gemini_api_key_here
   ```
   (Get free key from: https://ai.google.dev/gemini-api)

5. Click **Save** — Render will automatically redeploy with the new variable

---

### 3. Test the Deployment (1 minute)

Once Render finishes redeploying (watch the "Events" tab for "Deploy succeeded"):

1. Visit: `https://jarvis-headless-core.onrender.com/`
2. Should redirect to `/3d` (you'll see the sci-fi orb)
3. Click **Talk** button (microphone icon)
4. Try saying: "Hello" or "What time is it?"
5. Should respond with voice

---

## If Auto-Deploy Isn't Enabled

1. In Render dashboard, go to **Settings**
2. Look for **Auto-Deploy** toggle
3. Enable it (it looks for pushes to `main` branch automatically)
4. If you want manual deploy: click **Manual Deploy** → **Deploy latest commit**

---

## Verify Everything Works

**Checklist:**
- [ ] Home page redirects to `/3d` (orb interface)
- [ ] Orb spins and responds to clicks
- [ ] Talk button (microphone) clickable
- [ ] Business menu shows BuildPro / CareerRocketPro / Affiliate options
- [ ] Each business has toggleable settings
- [ ] No errors in browser console (F12)
- [ ] Voice responds naturally in English/Turkish

---

## If Something Breaks

**The orb interface doesn't show:**
- Check browser console (F12) for JavaScript errors
- Hard-refresh: `Ctrl+Shift+R` (or `Cmd+Shift+R` on Mac)
- Check `dashboard/ui_routing.py` was deployed (visit `/3d` directly)

**Voice doesn't work:**
- Check Render logs: `Settings` → `Logs`
- Verify GOOGLE_API_KEY is set correctly
- Ensure microphone permission granted in browser

**Business settings won't save:**
- Verify `config/business_dashboard_settings.json` exists
- Check file permissions on Render

**Gemini API errors:**
- Verify API key is correct (copy from google ai studio)
- Check API key has Gemini API enabled
- Try simpler Gemini model first: `gemini-1.5-flash`

---

## Rollback (If Needed)

If something breaks, rollback to previous version:

```bash
git revert HEAD --no-edit
git push origin main
```

Render will redeploy the previous commit.

---

## What's Different from Before

| Feature | Before | After |
|---------|--------|-------|
| **Primary API** | Anthropic (no credits) | Gemini (free tier) |
| **Fallback** | Gemini (broken) | None (Gemini only) |
| **Interface** | Corporate dashboard | Sleek orb (sci-fi) |
| **Obsidian Vault** | On desktop (disconnected) | Integrated into cloud memory |
| **Voice** | Broken | Working with proper audio config |
| **Business Settings** | None | 3 apps with 5-8 toggles each |

---

## You're Done

Once the code is pushed and Gemini API key is set, **JARVIS is live**.

The system is now:
- Cloud-based ✓
- Voice-enabled ✓
- Orb UI ✓
- Obsidian brain integrated ✓
- Business-specific settings ✓
- No Anthropic dependency ✓

**Next time you visit https://jarvis-headless-core.onrender.com/, you should see the orb and be able to speak to it.**

Questions? Check `JARVIS_FIXES_APPLIED.md` for detailed docs.
