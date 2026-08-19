"""Public, unauthenticated representation-agreement signing page.

Deliberately outside the /ui session-cookie and /api bearer-token auth
layers — a candidate reaches this from a plain link in their welcome
email with no JARVIS account of their own. See actions/agreement_signing.py
for the storage/signing logic and its PLACEHOLDER TEXT warning (the
agreement text shown here is a draft, not reviewed legal language, until
Lee replaces it).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from actions import agreement_signing

router = APIRouter()


def _escape(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _shell(body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BuildPro Recruiters — Representation Agreement</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #1a1d24; }}
  pre {{ white-space: pre-wrap; background: #f5f6f8; padding: 20px; border-radius: 10px; border: 1px solid #e2e4e8; font-family: inherit; font-size: 14px; }}
  input {{ padding: 12px; width: 100%; font-size: 16px; margin: 10px 0; box-sizing: border-box; border: 1px solid #ccc; border-radius: 6px; }}
  button {{ padding: 12px 28px; font-size: 16px; background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; }}
  .error {{ color: #c0392b; }}
  .notice {{ background: #fff8e1; border: 1px solid #f0d878; padding: 12px 16px; border-radius: 8px; font-size: 13px; }}
</style>
</head>
<body>{body}</body>
</html>"""


@router.get("/agreement/{token}", response_class=HTMLResponse)
async def view_agreement(token: str):
    agreement = agreement_signing.get_agreement(token)
    if agreement is None:
        return HTMLResponse(_shell("<h1>Link not found</h1><p>This agreement link is invalid or has expired.</p>"), status_code=404)
    if agreement.get("signed_ts"):
        return HTMLResponse(_shell(
            f"<h1>Already signed</h1><p>This agreement was signed by {_escape(agreement['signed_name'])}. Thank you!</p>"
        ))
    body = f"""
    <h1>BuildPro Recruiters</h1>
    <h2>Representation Agreement</h2>
    <p>Hi {_escape(agreement['candidate_name'])}, please review the agreement below and sign to continue.</p>
    <pre>{_escape(agreement['agreement_text'])}</pre>
    <form method="post" action="/agreement/{token}/sign">
      <label for="signed_name">Type your full legal name to sign:</label>
      <input type="text" id="signed_name" name="signed_name" required autocomplete="name">
      <button type="submit">Sign Agreement</button>
    </form>
    """
    return HTMLResponse(_shell(body))


@router.post("/agreement/{token}/sign", response_class=HTMLResponse)
async def submit_signature(token: str, request: Request):
    form = await request.form()
    signed_name = str(form.get("signed_name") or "")
    signer_ip = request.client.host if request.client else "unknown"

    agreement = agreement_signing.get_agreement(token)
    if agreement is None:
        return HTMLResponse(_shell("<h1>Link not found</h1><p>This agreement link is invalid or has expired.</p>"), status_code=404)

    result = agreement_signing.sign_agreement(token, signed_name, signer_ip)
    if not result["ok"]:
        body = f"""
        <h1>BuildPro Recruiters</h1>
        <h2>Representation Agreement</h2>
        <p class="error">{_escape(result['detail'])}</p>
        <pre>{_escape(agreement['agreement_text'])}</pre>
        <form method="post" action="/agreement/{token}/sign">
          <label for="signed_name">Type your full legal name to sign:</label>
          <input type="text" id="signed_name" name="signed_name" required autocomplete="name">
          <button type="submit">Sign Agreement</button>
        </form>
        """
        return HTMLResponse(_shell(body), status_code=400)

    return HTMLResponse(_shell(
        f"<h1>Thank you, {_escape(result['signed_name'])}!</h1>"
        "<p>Your signature has been recorded. Welcome to BuildPro Recruiters — we'll be in touch soon.</p>"
    ))
