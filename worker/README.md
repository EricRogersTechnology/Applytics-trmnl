# Applytics Worker (Cloudflare)

The free serverless backend that makes Applytics a **public** TRMNL plugin: each
user enters their own App Store Connect credentials in the plugin's form, TRMNL
polls this Worker with them, and the Worker signs the JWT, pulls the Sales
reports, and returns the numbers. **Stateless — nothing is stored or logged.**

## Deploy (no tools needed)
1. Sign in at https://dash.cloudflare.com (free).
2. **Workers & Pages → Create → Create Worker** → name `applytics` → **Deploy**.
3. **Edit code** → delete the starter → paste all of `applytics-worker.js` → **Deploy**.
4. Copy your URL: `https://applytics.<your-subdomain>.workers.dev`.

CLI alternative: `npx wrangler deploy` from this folder.

## Wire it to TRMNL (Polling + Form Fields)
- **Strategy:** Polling   **Verb:** POST   **URL:** your Worker URL
- **Headers:** `Content-Type: application/json`
- **Body:**
  `{"key_id":"{{ asc_key_id }}","issuer_id":"{{ asc_issuer_id }}","private_key":"{{ asc_private_key }}","vendor_number":"{{ asc_vendor_number }}","app_store_url":"{{ app_store_url }}"}`
- **Form Fields:** `asc_key_id`, `asc_issuer_id`, `asc_private_key` (text), `asc_vendor_number`, `app_store_url` (url, optional)
- Paste the four `src/*.liquid` files as the markup.

## Security
- Stateless; credentials pass through in memory per request, never stored/logged.
- Use a **Sales-only** key (least privilege).
- Don't want a shared Worker? Deploy **your own** (above) and point the plugin at your URL — your key only touches infrastructure you control.
