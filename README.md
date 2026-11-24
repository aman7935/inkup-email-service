# Inkup review emails

Sends a customer a "how was your order?" email **3 days after Shiprocket marks
the order Delivered**, with a "Write a review" link (Judge.me) for each product.

## How it works

One small Python app (FastAPI). No queues, no Redis.

1. **Shiprocket calls the webhook** `POST /webhook/tracking-update` on every
   tracking update. The app checks the `x-api-key` token. If the status is
   *Delivered*, it saves the order to a SQLite file with
   `due = delivery time + 3 days`. Shiprocket gets its `200` right away.
2. **Every 5 minutes** a background loop inside the same app looks for orders
   that are due and, for each one:
   - asks the **Shiprocket API** for the buyer's email, name and products,
   - asks **Shopify** for the exact product links (falls back to guessing the
     link from the product name if Shopify is unavailable),
   - sends the email through **Resend**.
3. Temporary failures (Resend down, wrong password) are retried with a growing
   delay. Orders without a buyer email are skipped. Each order is emailed at
   most once.

Code: `api/routes.py` (webhook), `reviews.py` (the flow), `storage.py`
(SQLite), `services/` (Shiprocket, Shopify, Resend), `templates/` (the email).

## Run it on your computer

```bash
uv sync --all-groups
cp .env.example .env     # fill in the values
make run                 # http://localhost:8000
make test
```

Quick test without waiting 3 days: set `REVIEW_DELAY_SECONDS=60` in `.env`.
Put it back to `259200` before going live.

## Deploy on Render

1. Push this project to GitHub.
2. Render → **New → Blueprint** → choose the repo. `render.yaml` creates one
   web service (Starter plan) with a 1 GB disk for the database.
3. Fill in the values Render asks for (same as `.env`).
4. After the deploy, the service URL is shown at the top of its page, e.g.
   `https://inkup-reviews.onrender.com`. Check `…/health/live` returns `ok`.

Keep it at **one instance**: the email sender runs inside the app.

## Connect Shiprocket

Shiprocket → **Settings → API → Webhooks**:

| Field | Value |
|---|---|
| URL | `https://<your-render-url>/webhook/tracking-update` |
| Auth token type | `x-api-key` |
| Token | the `WEBHOOK_TOKEN` value |

The URL must not contain `shiprocket`, `kartrocket`, `sr` or `kr`.

The Shiprocket **API user** (`SHIPROCKET_API_EMAIL` / `_PASSWORD`) is created
under **Settings → Additional Settings → API Users** with the Listings,
Shipments and Order modules.

## Logs to look for

| Log | Meaning |
|---|---|
| `webhook_token_rejected` | Token in Shiprocket ≠ `WEBHOOK_TOKEN` |
| `tracking_update_ignored_not_delivered` | Normal: an in-transit update |
| `review_request_scheduled` | Delivered order saved; email in 3 days |
| `review_email_sent` | Email sent |
| `review_skipped` | No buyer email / products for that order |
| `review_retry_later` | Temporary problem; will try again |
| `shopify_configuration_error` | Shopify login failed; links guessed from product names |
