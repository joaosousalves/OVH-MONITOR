# OVHcloud KS-LE-6 availability monitor

Standalone Python 3 service for Linux. It checks the official, public OVHcloud stock API every 30 seconds and sends an SMTP email when **any** KS-LE-6 storage configuration becomes available at **any** datacenter. Your preferred option is highlighted first:

- **Preferred:** 2 × 512 GB NVMe + 2 × 4 TB HDD (`hybridsoftraid-2x4000sa-2x500nvme`)
- Also alerts, in this priority order: 4 × 960 GB SATA SSD; 3 × 1 TB NVMe; 2 × 1.92 TB NVMe.
- All reported datacenters are checked, including Beauharnois (BHS), Canada.

**Plan code:** `27skle06-v1`  
**Public API:** `https://api.eu.ovhcloud.com/v1/dedicated/server/datacenter/availabilities?planCode=27skle06-v1`  
**Product page:** https://eco.ovhcloud.com/pt/kimsufi/ks-le-6/

No external Python packages, database, Docker, or OVHcloud API credentials are required. Uses Python's standard library and systemd.

## Behavior

1. Query OVHcloud and read every matching plan/storage/datacenter combination.
2. Treat orderable provisioning codes such as `1H-high`, `1H-low`, and `72H` as available. Do **not** alert on `unavailable`, `unknown`, or `comingSoon`.
3. Group all newly available combinations into one email, sorted with preferred disks first.
4. Record each alerted configuration/datacenter in `/var/lib/ovh-ksle-monitor/state.json` (managed by systemd).
5. Do not send more emails for the same combination until an API check has explicitly reported it unavailable, then later available again.
6. If OVHcloud temporarily omits a configuration, retain alert history rather than resetting it. If SMTP fails, retry alerts next run without marking them as delivered.

The first live check emails any currently available options. An empty or malformed API response causes a visible error; it cannot erase alert history. It only notifies: it never orders or reserves a server. Fast restocks may still be missed between polls. The API status does not guarantee a successful checkout.

## OVH Mail SMTP configuration (Basketly sender)

This application uses `SMTP_*` systemd environment variables (not Django's `EMAIL_HOST` settings). OVH's port **587** uses **STARTTLS**, so set `SMTP_SECURITY=starttls` (not `ssl`). The SMTP login is `info@basketly.eu`, while the visible sender is `Basketly <donotreply@basketly.eu>`. Verify OVHcloud allows `info@basketly.eu` to send using the `donotreply@basketly.eu` address. Set `SMTP_TO` to **your actual recipient address**.

Create a **local, root-only** environment file named **`/etc/ovh-ksle-monitor.env`**, *outside the Git repository*. Example:

```ini
SMTP_HOST=smtp.mail.ovh.net
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=info@basketly.eu
SMTP_PASSWORD=REPLACE_WITH_YOUR_SMTP_PASSWORD
SMTP_FROM="Basketly <donotreply@basketly.eu>"
SMTP_TO=your-personal-email@example.com
SMTP_TIMEOUT=15
HTTP_TIMEOUT=15
```

Run this from your checked-out project after `git pull origin main`:

```bash
sudo install -m 0600 config.example /etc/ovh-ksle-monitor.env
sudo nano /etc/ovh-ksle-monitor.env
sudo chown root:root /etc/ovh-ksle-monitor.env
sudo chmod 600 /etc/ovh-ksle-monitor.env
sudo install -m 0644 ksle_monitor.py /opt/ovh-ksle-monitor/ksle_monitor.py
```

If the monitor is already installed, **do not overwrite your existing credential file** with `install config.example`; just edit the existing `/etc/ovh-ksle-monitor.env` instead.

The service already loads the file through `EnvironmentFile=/etc/ovh-ksle-monitor.env`. Test email delivery without any stock/state changes:

```bash
sudo systemd-run --wait --pipe --collect \\
  --property=EnvironmentFile=/etc/ovh-ksle-monitor.env \\
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --send-test-email
```

Use `sudo journalctl -u ovh-ksle-monitor.service -n 40 --no-pager` for subsequent stock-check logs. Do not paste SMTP secrets into chat or check them into version control.

## Troubleshooting missing catalog prices

The monthly price parser now reads the recurring one-month entries (`phase=1`, `interval=1`) and ignores annual or 24-month commitments. It tolerates identical repeated monthly entries from the bandwidth addon. The corrected cache format invalidates pre-fix pricing entries automatically after an update.

If `--list-prices` still returns zero verified layouts, run:

```bash
cd /opt/ovh-ksle-monitor
python3 ksle_monitor.py --diagnose-prices
```

This requests a fresh public Eco catalog and prints a restricted, non-secret summary: currency, KS-LE-6 monthly pricing fields and the mandatory addon family's first candidate. Share that output to identify schema differences. It never creates a cart or order, and does not alter the monitor's 30-second stock timer. The pricing cache now also correctly refreshes on first use in a new state directory.

## Prices in stock alerts (Portugal)

On a **new stock alert only**, the monitor consults the publicly available OVHcloud Eco catalog for subsidiary `PT`, and caches monthly pricing for **6 hours** in `/var/lib/ovh-ksle-monitor/pricing-cache.json`. For each storage option the monitor tries to calculate the monthly plan **plus all mandatory default addons** using the current Eco catalog recurring monthly pricing (`phase=1`, `interval=1`; older schemas may expose `P1M`). The stock endpoint itself does not return prices. No API token or cart operation is needed for the public catalog.

Prices, when available and unambiguous, are labeled **"EUR/mês sem IVA (estimativa de catálogo)"** in both HTML and plain-text emails. The monitor also appends an **illustrative 23% Portuguese mainland IVA calculation**; neither is a checkout quote. They can omit location-specific fees, setup fees, promotional adjustments or tax rules specific to the account. If any required price is missing, ambiguous or the catalog cannot be reached, the email says **"Preço indisponível — confirmar no checkout"**. Price lookup failure does not stop stock alerts; failed queries are not retried more than once per hour. Existing alert history and stock backoff are preserved. No changes to `/etc/ovh-ksle-monitor.env` are required.

To inspect available catalog prices **without sending email**, run `python3 ksle_monitor.py --list-prices` from the project directory. This fetches the public catalog only if the cache is due.

Install the new `pricing.py` module alongside `ksle_monitor.py` (or pull the repository if it is already cloned in `/opt/ovh-ksle-monitor`):

```bash
cd /opt/ovh-ksle-monitor
sudo git pull origin main
python3 -m unittest discover -s tests -v
```

## HTML email notifications

Stock alerts are sent as multipart/alternative email: accessible plain text plus responsive HTML featuring a dark OVH-MONITOR header, per-configuration cards, preferred-storage badge, datacenter, availability, timestamp, and a clickable full-area button plus a visible fallback URL to the official OVHcloud product page. A single email can list multiple available configurations. All values originating in API responses are HTML-escaped. No images or external email template assets are required.

`--send-test-email` sends a **sample HTML stock alert** with a prominent **TEST / simulated availability** banner, without checking stock or changing alert history. It does **not** place an order. Existing SMTP configuration in `/etc/ovh-ksle-monitor.env` is unchanged.

## Requirements

- Ubuntu/Debian or another Linux with `systemd` and **Python 3.10 or later**.
- Outbound HTTPS to `api.eu.ovhcloud.com:443`.
- Outbound SMTP to your chosen mail server (`587` for STARTTLS or `465` for implicit TLS).
- Valid SMTP sender and recipient addresses. No SMTP credentials are bundled.

## Installation

From the extracted `ovh-ksle6-monitor` folder:

```bash
sudo install -d -m 0755 /opt/ovh-ksle-monitor
sudo install -m 0644 ksle_monitor.py /opt/ovh-ksle-monitor/ksle_monitor.py
sudo install -m 0644 order_probe.py /opt/ovh-ksle-monitor/order_probe.py
sudo install -m 0600 config.example /etc/ovh-ksle-monitor.env
sudo nano /etc/ovh-ksle-monitor.env
```

Update `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_TO` with your real details. Use a dedicated SMTP/app password. Set `SMTP_TO` to comma-separated addresses if you want several recipients. If your TLS-protected SMTP relay needs no authentication, leave **both** username and password empty. Keep the `/etc/ovh-ksle-monitor.env` file owner-root and permission `0600`.

For OVH SMTP submission, use the host and credentials from the **actual mail service you own**. Do not assume the hostname or port is the same for every OVH offer. The service requires verified TLS, including when using an unauthenticated relay.

**Check current stock manually first (no email, no state changes):**

```bash
python3 ksle_monitor.py --list
python3 ksle_monitor.py --check
```

**Install and activate the timer:**

```bash
sudo install -m 0644 systemd/ovh-ksle-monitor.service /etc/systemd/system/
sudo install -m 0644 systemd/ovh-ksle-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ovh-ksle-monitor.timer
sudo systemctl list-timers ovh-ksle-monitor.timer
```

The service executes as a temporary unprivileged user (systemd `DynamicUser`) and keeps its persistent history in a systemd-owned state directory.

**Test SMTP independently** (does not read stock or modify alert history):

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-monitor.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --send-test-email
```

**Run one real check now** (may send a genuine stock email):

```bash
sudo systemctl start ovh-ksle-monitor.service
sudo journalctl -u ovh-ksle-monitor.service -n 40 --no-pager
```

**Check logs and timer health:**

```bash
systemctl status ovh-ksle-monitor.timer
sudo journalctl -u ovh-ksle-monitor.service -f
```

## Stop, restart, and alert history

```bash
sudo systemctl disable --now ovh-ksle-monitor.timer
# Re-enable later:
sudo systemctl enable --now ovh-ksle-monitor.timer
```

Alert history survives restarts, reboots, and service upgrades. If you ever **intentionally** want to send alerts again for configurations still in stock, stop the timer first and use an administrator to move (rather than delete) `/var/lib/ovh-ksle-monitor/state.json` to a backup. On the next live run a new history file will be created. Don't reset history during normal updates.

## API cooldown and rate limiting

The default systemd timer now runs **30 seconds after the previous service execution completes**, plus an initial check 30 seconds after boot. It does not overlap checks.

If the stock API returns **HTTP 429**, **HTTP 5xx**, or a temporary connection error, the monitor saves its API cooldown separately as `/var/lib/ovh-ksle-monitor/api-backoff.json`. Delay increases exponentially (60 s, 120 s, 240 s, etc., up to 30 minutes), and respects any longer `Retry-After` value (up to 24 hours). During cooldown, timer executions exit without requesting the stock API. A successful response resets the backoff counter. Existing stock alert history remains unchanged. Manual `--check` and `--list` bypass cooldown for diagnostics, so don't run them repeatedly during rate limiting.

**Updating an existing installation:** `git pull origin main` alone does not update the active systemd timer. Reinstall both the application and timer, then reload and restart:

```bash
cd /opt/ovh-ksle-monitor
sudo git pull origin main
sudo install -m 0644 ksle_monitor.py /opt/ovh-ksle-monitor/ksle_monitor.py
sudo install -m 0644 systemd/ovh-ksle-monitor.timer /etc/systemd/system/ovh-ksle-monitor.timer
sudo systemctl daemon-reload
sudo systemctl restart ovh-ksle-monitor.timer
systemctl list-timers ovh-ksle-monitor.timer
sudo journalctl -u ovh-ksle-monitor.service -n 30 --no-pager
```

If you previously created a timer override via `sudo systemctl edit ovh-ksle-monitor.timer`, inspect it with `systemctl cat ovh-ksle-monitor.timer`; it may supersede the packaged schedule. Don't remove custom overrides without inspecting them.

## Change polling frequency

Edit the installed timer's `OnUnitInactiveSec` setting. For example, `OnUnitInactiveSec=60s` checks approximately 60 seconds after the previous run finishes. Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ovh-ksle-monitor.timer
```

Avoid overly aggressive polling; respect OVHcloud's service terms and any future rate limits. This package defaults to 30 seconds, with persistent API cooldown.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

All tests use mocked data and SMTP; they do not send email or call OVHcloud.

## Optional: test OVHcloud API ordering without purchasing

The manual `--test-order` command can test whether your OVHcloud Portugal account
can prepare and quote a **temporary Eco cart** for the KS-LE-6. It will:

1. Confirm the server and selected addons exist in the public PT catalog.
2. Create and assign a temporary cart to your account.
3. Look for the monthly `P1M`/`default` offer, add **one** server, its mandatory
   addons and its selected datacenter configurations.
4. Read the **GET checkout quotation** and show the total including VAT.
5. Delete the temporary cart on exit, including after errors when possible.

**It never calls POST checkout, never creates a paid order, and never charges a
payment method.** A successfully quoted cart does not reserve stock, and a
quote is not a guarantee that a subsequent purchase will succeed. With stock
unavailable, the API may not offer the plan at all, which is an expected
non-purchasing test result. When no exact `P1M/default` offer is found,
the program reports the number of Eco offers returned, matches for KS-LE-6,
and a short sample of `planCode/duration/pricingMode` fields. It does not
log the full cart response or any API credentials. **An empty offer list is
not an authentication error after cart creation/assignment succeeds.**
The temporary cart is still deleted on failure.

This feature is completely separate from the regular `systemd` monitor timer.
The timer does not read the token or attempt to order anything.

### Authentication (AK / AS / CK)

The temporary Eco cart probe supports the **three OVHcloud API credentials** created at [auth.eu.ovhcloud.com/api/createToken](https://auth.eu.ovhcloud.com/api/createToken): Application Key (AK), Application Secret (AS) and Consumer Key (CK). Requests are signed with `X-Ovh-Application`, `X-Ovh-Consumer`, `X-Ovh-Timestamp` and `X-Ovh-Signature`. To avoid clock-drift issues, it obtains the OVH server timestamp from `/1.0/auth/time`.

Grant only: `GET /order/catalog/public/eco`, `POST /order/cart`, `POST /order/cart/*/assign`, `GET /order/cart/*/eco`, `POST /order/cart/*/eco`, `POST /order/cart/*/eco/options`, `POST /order/cart/*/item/*/configuration`, `GET /order/cart/*/checkout`, `DELETE /order/cart/*`. **Do not grant POST checkout or payment permissions.**

Create a separate root-only file for the cart test (never overwrite an existing credential file without checking it first):

```bash
cd /opt/ovh-ksle-monitor
sudo install -m 0600 -o root -g root order-config.example /etc/ovh-ksle-order.env
sudo nano /etc/ovh-ksle-order.env
sudo chmod 600 /etc/ovh-ksle-order.env
```

Replace the three placeholders in `/etc/ovh-ksle-order.env` with your own AK/AS/CK values. Never put real secrets in the Git repository, logs, shell command line or chat.

**Run tests before the real API call:**

```bash
python3 -m unittest discover -s tests -v
```

These tests do not call the OVHcloud API or create a cart. A legacy `OVH_TOKEN` Bearer value is also supported if you already use OAuth2 access tokens, but do not set it when using AK/AS/CK.

To test the **preferred disks** in **Beauharnois (BHS)**:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --test-order --datacenter bhs
```

Replace `bhs` with `gra`, `rbx`, `fra`, `sbg`, `waw`, or `lon` for another
datacenter. The preferred storage is `2 × 512 GB NVMe + 2 × 4 TB HDD`. To test
another disk configuration, use `--storage` with the corresponding full catalog
addon plan code (which ends in `-27skle06-v1`).

The test's last step is a **GET** request to `/order/cart/{cartId}/checkout`.
No purchase endpoint is present anywhere in the test-order API client.

### Quote an exact available KS-LE-1 configuration (never purchase)

The default KS-LE-1 addon selection can be out of stock even while another
RAM/storage combination is available. For example, the public stock API showed
`27skle01-v1.ram-32g-ddr3-noecc-1333.softraid-3x2000sa` as `1H-high` in
BHS, but the default 16 GB RAM / two SSD configuration was unavailable.
Pass the **exact FQN** from `--list-test-stock` with `--test-fqn`:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --test-order --test-plan ks-le-1 --datacenter bhs \
  --test-fqn '27skle01-v1.ram-32g-ddr3-noecc-1333.softraid-3x2000sa'
```

Before creating any cart, this command re-queries the **public stock API**
and requires the exact FQN and datacenter to report an orderable status.
It then resolves both the memory and storage addon codes against the public
Eco catalog, instead of using the defaults. If stock changed, it refuses to
create a cart. A matching availability status is not a reservation and checkout
quotation can still fail. The test deletes the temporary cart when possible,
does **not** call POST checkout or payment endpoints and does not change the
monitor's existing stock alert history. No new API credentials are needed.

### Find KS-LE-1 stock before testing a cart (no credentials)

A checkout quotation can fail with HTTP 400 if the selected plan, memory,
storage and datacenter are out of stock. This is expected even when cart
creation and option selection worked. Query the public availability endpoint
for the test plan, **without creating a cart**:

```bash
cd /opt/ovh-ksle-monitor
python3 ksle_monitor.py --list-test-stock --test-plan ks-le-1
```

The command prints one full hardware FQN per datacenter along with the API
availability status. Compare it to the FQN in the checkout error. For example,
`27skle01-v1.ram-16g-ddr3-noecc-1333.softraid-2x480ssd` in `GRA` must
be available (not `unavailable`) before trying a GRA quote with the default
KS-LE-1 storage. A different storage FQN is **not** a match for the default
cart configuration; an explicit storage addon code would be required.

The lookup is manual, read-only, uses the public stock API, and does **not**
modify the KS-LE-6 timer's alert state, SMTP settings, or ordering credentials.

### Optional: safely test KS-LE-1 with real stock

The current Eco cart API returns contract terms in each product offer's
`prices` array. The cart probe selects only the nested `P1M` and `default`
terms and rejects ambiguous offers. This also fixes the KS-LE-6 quote test.

To test **KS-LE-1** (PT catalog plan `27skle01-v1`) independently of the
KS-LE-6 stock monitor, use the product's **default storage configuration**
from the public catalog:

```bash
cd /opt/ovh-ksle-monitor
sudo git pull origin main
python3 -m unittest discover -s tests -v

sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --test-order --test-plan ks-le-1 --storage default --datacenter bhs
```

If BHS does not accept the configuration, try another datacenter reported
available for KS-LE-1. The public catalog decides the mandatory addons and
valid datacenters. The command never calls `POST /order/cart/{cartId}/checkout`,
does not create a paid order and deletes the temporary cart when possible.
No new API permissions are needed. Without `--test-plan`, the command still
tests KS-LE-6 preferred disks; the 30-second systemd monitor **always** remains
on KS-LE-6.

## Phase B — disabled KS-LE-6 purchase preview (no real checkout)

**Status: purchase preparations only.** The current program **cannot submit
a real order**, even if somebody changes `OVH_AUTO_BUY_ENABLED=true`. The
OVHcart API client has an explicit endpoint allowlist that excludes
`POST /order/cart/{cartId}/checkout` and all payment endpoints. The enabled
setting deliberately fails closed instead of buying.

The separate manual `--purchase-preview` command:

1. Checks the public stock feed for the **four** owner-approved
   `27skle06-v1.ram-128g-ddr4-ecc-2400.<storage>` disk layouts, in
   priority order.
2. Restricts candidates to the explicitly allowed datacenters.
3. If no eligible configuration is available, exits with `WAITING`
   without creating an authenticated cart or touching purchase history.
4. If available, rechecks the exact hardware/datacenter stock, prepares a
   temporary monthly `P1M/default` OVHcloud cart and obtains a **GET
   checkout** quote without placing an order.
5. Checks the checkout's structured `prices.withTax.value` amount and
   requires `currencyCode=EUR` and a value not exceeding
   `OVH_AUTO_BUY_MAX_TOTAL_EUR`. This cap is for the *initial payment
   including VAT and setup fees*. The separate owner-approved
   `OVH_AUTO_BUY_MAX_MONTHLY_EUR` cap is mandatory, but **the recurring
   renewal cost is not independently verified by this quote**.
   Missing or ambiguous initial price data blocks the preview.
6. Deletes the temporary cart even after a price-policy failure and
   reports `PREVIEW ONLY` if all checks pass.

A future ordering implementation can call `OrderJournal.reserve_once()`
before any billable operation to guarantee an exclusive persistent
one-purchase reservation, refusing retries if an attempt was made or its
outcome is uncertain. The preview reads that journal but **never reserves
anything**, so dry runs cannot silently consume the purchase allowance.
**No live checkout, account-wide order reconciliation, or real order-result
email has been enabled.** A separate GET-only payment readiness command exists.

### Configure the preview

The current `/etc/ovh-ksle-order.env` already holds your API credentials.
**Do not overwrite it** with `order-config.example`. Instead append these
new settings to that protected, root-only file:

```ini
OVH_AUTO_BUY_ENABLED=false
OVH_AUTO_BUY_MAX_TOTAL_EUR=80.00
OVH_AUTO_BUY_MAX_MONTHLY_EUR=37.00
OVH_AUTO_BUY_ALLOWED_DATACENTERS=bhs,fra,gra,lon,rbx,sbg,waw
OVH_AUTO_BUY_JOURNAL=/var/lib/ovh-ksle-purchase/intent.json
```

`80.00` EUR initial (with VAT/setup) and `37.00` EUR monthly (with VAT)
are the owner's confirmed **per-server maximums**. These separate
caps are not permission to submit a purchase. The code refuses missing
or invalid limits and retains a 500 EUR upper-bound safety check.
The actual monthly renewal price remains unverified.
The datacenter ordering represents preference only, not guaranteed stock.

```bash
sudo nano /etc/ovh-ksle-order.env
sudo chown root:root /etc/ovh-ksle-order.env
sudo chmod 600 /etc/ovh-ksle-order.env

cd /opt/ovh-ksle-monitor
python3 -m unittest discover -s tests -v

sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --purchase-preview
```

When stock is unavailable, the preview outputs `WAITING`. When the
preferred option is in stock and the quote meets the price cap, the
output starts `PREVIEW ONLY`. No automated spending, emails, or new
persistent order attempts are performed by this command.

The existing `ovh-ksle-monitor.service` and timer still run the plain
`--once` monitor every 30 seconds with the separate
`/etc/ovh-ksle-monitor.env`. They do not load any ordering credentials
or invoke `--purchase-preview`.

## Phase C — payment readiness and offline checkout simulation

**Important: automatic purchasing is still disabled.** The code intentionally
has **no live POST checkout/payment implementation**, and neither
`OVH_AUTO_BUY_ENABLED=true` nor any CLI option can override that. The old
`systemd` timer still only calls `--once` to check **KS-LE-6 stock**.

Phase C adds:

- `--check-payment-readiness`: authenticated **GET-only** check that the
  account has exactly one default payment method marked `VALID`, with
  one-click support and no expired date. This **cannot** prove sufficient
  funds, 3-D Secure approval, or that an order will be paid automatically.
  It never prints a card number, payment identifier or account details.
- `--simulate-checkout --simulation-case accepted|timeout|rejected`: an
  **entirely offline** mock of OVHcloud checkout with a temporary, isolated
  order journal. It checks the configured example initial-charge cap, makes
  exactly one call to a local fake gateway, persists `submitted` or
  `unknown`, blocks a duplicate attempt and formats a mock notification
  without sending email. It does **not** access OVHcloud APIs, send email,
  reserve real stock, or buy a server.
- `--reconcile-order-status`: **GET-only** inspection of an order ID
  already persisted in the real purchase journal, if one ever exists.
  If the journal indicates an uncertain attempt with no known order ID,
  it requires manual OVHcloud account investigation. It **never deletes
  the journal, retries checkout, or declares payment complete** merely
  from a status response.

### Additional API access for a payment check

The existing cart-only key may return HTTP 403 for new account endpoints.
To use `--check-payment-readiness`, create a **new OVHcloud API credential**
or rotate the existing one with the prior cart permissions plus only:

```text
GET /me/payment/method
GET /me/payment/method/*
GET /me/order/*/status
```

The order-status route is only needed for future reconciliation, not
payment readiness. Follow least privilege; don't enable checkout or
payment rights, and don't share AK/AS/CK secrets. Change only the values
in the already-protected `/etc/ovh-ksle-order.env`. API validity/expiry
and source-IP restrictions still apply.

### Test the new code

After updating GitHub files on the VPS:

```bash
cd /opt/ovh-ksle-monitor
sudo git pull origin main
python3 -m unittest discover -s tests -v
```

The existing `/etc/ovh-ksle-order.env` should retain:

```ini
OVH_AUTO_BUY_ENABLED=false
OVH_AUTO_BUY_MAX_TOTAL_EUR=80.00
OVH_AUTO_BUY_MAX_MONTHLY_EUR=37.00
OVH_AUTO_BUY_ALLOWED_DATACENTERS=bhs,fra,gra,lon,rbx,sbg,waw
OVH_AUTO_BUY_JOURNAL=/var/lib/ovh-ksle-purchase/intent.json
```

`80.00` EUR is the owner-approved first-charge maximum (with VAT),
separate from the `37.00` EUR VAT-inclusive monthly cap. Neither
is consent to buy, and the offline simulator's `39.99` EUR example
charge **is not an OVHcloud price**. Do not overwrite the API
credentials file when changing these settings.

**No credentials or API calls needed for the offline simulator:**

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --simulate-checkout --simulation-case accepted

sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --simulate-checkout --simulation-case timeout
```

**Optional real account check (read-only, requires GET permissions):**

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --check-payment-readiness
```

Do not add this to the 30-second timer. New module simulations never use
the real `OVH_AUTO_BUY_JOURNAL` and cannot create a persistent false
purchase on your VPS.

### Still required before live automated purchasing

The real KS-LE-6 `P1M` checkout must be validated against both a
user-approved initial-charge cap **and an independently verified
monthly-recurring-with-tax cap**. The current Phase B price cap verifies
only the first payment. In addition, a real order-submission transport
would require a newly approved scope for `POST /order/cart/*/checkout`,
payment method approval, an operator-approved kill switch, order ID
reconciliation, real notifications, and an isolated systemd purchase
worker. **None of that live submission path is activated or granted by
Phase C.** Never treat an order ID as proof of payment or provisioning.

Official OVHcloud documentation:
- https://docs.us.ovhcloud.com/en/bare-metal-cloud/dedicated-servers/order-dedicated-server
- https://developers.eu.ovhcloud.com/reference/payment/
- https://developers.eu.ovhcloud.com/reference/offers-services/orders/

## Phase D — TWO KS-LE-6 128GB servers, prioritized disk choices

The requirement is **at most two KS-LE-6 servers total** (`27skle06-v1`),
**both with 128 GB RAM**, and either **the same disk configuration**
or **different configurations**. These are NOT two distinct models.
Both purchases must be in a **European or Canadian** datacenter.

Current disk order for purchase previews (highest preference first):

| Priority | Exact stock storage code | Disk configuration |
| --- | --- | --- |
| 1 | `hybridsoftraid-2x4000sa-2x500nvme` | 2 × 512 GB NVMe + 2 × 4 TB HDD |
| 2 | `softraid-4x960ssd` | 4 × 960 GB SATA SSD |
| 3 | `softraid-3x1000nvme` | 3 × 1 TB NVMe |
| 4 | `softraid-2x1920nvme` | 2 × 1.92 TB NVMe |

All four exact disk layouts have been **explicitly approved by the
owner** as future purchase preferences, in the order shown. The first
choice remains unchanged. The two-slot quota now accepts any of these
FQNs by default, including the same FQN twice. This hardware approval
does **not** activate billable checkout. Exact hardware is validated against
`27skle06-v1.ram-128g-ddr4-ecc-2400.<storage>`. A fake/mismatched RAM or
storage code is not eligible.

Accepted datacenter codes only: `bhs` (Canada); `fra` (Germany,
Limburg); `gra`, `rbx`, `sbg` (France); `lon` (UK); `waw` (Poland).
These are Europe or Canada only. Datacenter priority is evaluated after disk
priority. The user can revise the disk order before checkout goes live.
The owner revised the ranking on 20 November 2026: the 4 × 960 GB SATA SSD
configuration is now **second**, and 2 × 1.92 TB NVMe is now **fourth**.
The 128 GB RAM requirement, datacenters, two-server cap, 37 EUR monthly
and 80 EUR initial limits, and zero-commitment requirement are unchanged.

### Inspect KS-LE-6 configurations and location availability

```bash
cd /opt/ovh-ksle-monitor
sudo git pull origin main
python3 -m unittest discover -s tests -v
python3 ksle_monitor.py --list-ksle6-disks
```

The last command reads the public stock API **without credentials,
temporary carts or journal writes**. It prints each disk type's status
in the accepted Europe/Canada datacenters. The existing `--list`
command also continues showing every reported combination. A code like
`1H-high` indicates a provisioning lead-time estimate, not a count of
available servers or a reservation.

### Shared lifetime limit of two, including matching configurations

`purchase_quota.PurchaseQuota` stores **slot 1 and slot 2**
under a single atomic journal and local file lock. Each slot has a
unique numeric slot ID; the **same plan and exact FQN may occur twice**.
Two different disk FQNs may also fill these slots. After two recorded
attempts, **no third reservation** is allowed, including after
restarts or changing the preferred disks.

The second attempt is blocked while the first is in `reserved`,
`submitted`, or `unknown` state. Only once an externally verified
first attempt is marked `confirmed` can slot 2 be reserved. Timeout
or an uncertain result is deliberately **not retried automatically**.

The new journal uses **schema 2**, at
`/var/lib/ovh-ksle-purchase/purchases-v2.json`. A legacy
`intent.json` or `purchases-v1.json` journal will **block**
new reservations until a human reconciles it against OVHcloud.
Do not delete these records simply to bypass the limit.

Add or correct these entries in the existing, root-only
`/etc/ovh-ksle-order.env` (do not overwrite its actual credentials):

```ini
OVH_AUTO_BUY_ENABLED=false
OVH_AUTO_BUY_MAX_SERVERS=2
OVH_AUTO_BUY_MAX_TOTAL_EUR=80.00
OVH_AUTO_BUY_MAX_MONTHLY_EUR=37.00
OVH_AUTO_BUY_ALLOWED_DATACENTERS=bhs,fra,gra,lon,rbx,sbg,waw
OVH_AUTO_BUY_QUOTA_JOURNAL=/var/lib/ovh-ksle-purchase/purchases-v2.json
```

Check the local quota without changing it:

```bash
python3 ksle_monitor.py --purchase-quota-status
```

The status is **local ledger attempts**, not a verified count of all
servers already ordered or owned in your OVHcloud account. Any
existing KS-LE-6 orders in the account must be reconciled before
activating a future live purchaser.

### Activation remains blocked

`--purchase-preview` recognizes all four owner-approved disk
configurations, sorted by priority. No real purchase can occur because
live checkout remains disabled. It refuses a cart when both
reservation slots are used or an earlier order is unresolved.
The preview itself creates no reservation or billable order.

The email monitor checks only the four recognized KS-LE-6 disk layouts
with exactly 128 GB RAM in the approved Europe/Canada datacenters
every 30 seconds. Manual `--list` still displays the raw OVHcloud feed,
including non-eligible configurations and sites. The monitor does not
load purchase credentials.
**No live POST checkout, automated buying systemd worker, or payment
submission has been enabled.** Real-order safeguards still require
recurring monthly price verification, account-wide order reconciliation,
purchase-result notification delivery, and explicit user authorization.

## Phase E — manual purchase preflight (still NO checkout)

The optional `--purchase-preflight` command combines the existing
non-billable checks into **one manual, one-shot review**:

1. Check OVHcloud's live KS-LE-6 stock and select the highest-ranked
   128 GB disk configuration in Europe/Canada.
2. Reject legacy order journals, unresolved purchases, or a filled
   two-server quota before continuing.
3. Verify the same exact FQN is still available, prepare a **temporary**
   Eco cart, read `GET /order/cart/{cartId}/checkout`, validate the
   initial price (EUR including VAT), and delete the temporary cart.
4. Check the OVHcloud account's default payment method using **GET-only**
   APIs. No card details or payment identifiers are displayed.
5. Report the number of local slots remaining. All four disk options
   are approved preferences, but cannot be purchased yet.
6. Inspect the `RENEW` price labels already returned when adding the
   server and mandatory addons to the temporary cart. If **every** item
   has one unambiguous EUR monthly price label, display their sum as
   **cart renewal evidence**; otherwise report missing/incomplete evidence.
   This makes no extra API calls or changes to the API key permissions.
7. Display the monthly cap separately, with an explicit
   **MONTHLY PRICE UNVERIFIED** warning: `RENEW` labels do not attest
   to VAT, later discounts or the actual renewal bill, and the initial
   checkout amount is not a monthly price.

Run it manually using the existing root-only
`/etc/ovh-ksle-order.env` configuration (if created):

```bash
cd /opt/ovh-ksle-monitor
sudo systemd-run --wait --pipe --collect \
  --unit=ovh-ksle-purchase-preflight \
  -p EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --purchase-preflight
```

**This is not a second scheduled timer or an auto-buyer.** The
`--once` monitor keeps using only the public stock API and SMTP.
If stock is unavailable, preflight reports `WAITING` without
creating a cart or calling account/payment APIs. Quoting available
stock may create and then delete a temporary cart, but **never**
submits `POST /order/cart/{cartId}/checkout` or payment requests.
The order client still blocks those routes. The preflight creates
**no quota reservations** and never changes existing order records.

Example of a possible preflight message when all four cart components
contain EUR `RENEW` labels: `Cart RENEW component sum 32.00 EUR
(4 item(s)); VAT status, discounts, and final monthly renewal NOT
VERIFIED.` This is illustrative, **not a live OVHcloud price**. If a
component has no price label, the monitor intentionally omits any sum.
Preflight still cannot confirm whether the **37.00 EUR/month with VAT**
budget is respected, and cannot create a billable order.

`OVH_AUTO_BUY_ENABLED=true` continues to fail closed. The owner has
approved **80.00 EUR initial with VAT** and **37.00 EUR monthly with VAT**
per server, and all four prioritized disk choices. However, the
**actual recurring renewal cost remains UNVERIFIED**: the cap alone
is not proof the seller's price meets it. Before any live implementation,
we still need a reliable provider-sourced recurring price, OVHcloud
contract/payment review, reconciliation against orders already in the
account, and separate authorization to activate checkout.
**No live order will be made by this release.**

## Phase F — no fixed commitment + account service inventory (GET only)

**Owner's firm purchase requirement:** **KS-LE-6, 128 GB RAM, P1M
month-to-month WITHOUT a 12- or 24-month commitment.** This is separate
from paying one month at a time. The cart code already selects ONLY the
`P1M/default` offer, never `P12M` or `P24M`. Phase F also rejects
machine-readable positive commitment/engagement indicators in the offer,
added item or GET checkout quote. Unknown metadata does not count as
proof of zero commitment. Checkout remains disabled even if a zero-term
indicator is present; OVHcloud's complete contractual conditions must
still be independently reviewed before any future purchase implementation.

**Manual inventory of the authenticated OVHcloud account** uses ONLY:

- `GET /service` (list service IDs)
- `GET /service/{serviceId}` (read each service's exact `plan.code`)
- `GET /dedicated/server/{serviceName}/specifications/hardware` (**only** when
  a dedicated server's `plan.code` is null or blank)

The code first compares known plan codes to `27skle06-v1` / `27skle06`.
OVHcloud has also been observed returning a missing `plan.code` for every
service in an account. In that case, the tool excludes clearly unrelated
service routes (IP, domain, VPS, etc.) and **reads the official commercial
model** of each dedicated server. It recognizes a `KS-LE-6` model prefix,
while other models such as `KS-6` and `KS-LE-5` do not consume KS-LE-6
quota. This model-name fallback is not proof of an exact catalog plan code.

The service quantity is counted conservatively, **including expired
records**, to avoid exceeding the owner's lifetime limit of two servers.
Missing/inconsistent dedicated-server details, unrecognized routing,
ambiguous KS-LE-6 spellings or over 250 service IDs produce an error,
**not a false zero**. The summary reports how many dedicated hardware
lookups were performed and never includes service hostnames or identifiers.
The inventory excludes purchase orders that have not yet become services;
the local purchase journal still protects against duplicate attempts, and
additional order reconciliation is required for future live checkout.

For a new or rotated AK/AS/CK API key, retain the previous cart and
account permissions and grant these **GET** rights as well:

```text
GET /service
GET /service/*
GET /dedicated/server/*/specifications/hardware
```

Old keys may return `HTTP 403` until these are granted. Do **not** add
checkout, payment, PUT or DELETE service permissions.

To run an account-only inventory **even when there is no stock**, use
the existing root-only `/etc/ovh-ksle-order.env`:

```bash
sudo systemd-run --wait --pipe --collect \
  --unit=ovh-ksle-account-reconcile \
  -p EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --reconcile-account
```

This does not create a cart, send email, query payment methods, or
modify persistent purchase records. If stock becomes available during
manual `--purchase-preflight`, it checks the remote service count
**after** the validated temporary quote and **before** any payment-method
read. A verified account count of two or more blocks further work.
An incomplete account scan also blocks preflight. The existing 30-second
stock-alert timer is unchanged and needs no additional credentials.

**Still not solved within Phase F:** pending order IDs not yet provisioned, contractual
proof of zero minimum engagement when the API omits that metadata, and
the independently verified monthly renewal total with VAT. Therefore
all real purchase or payment operations remain impossible.

## Phase H — manual pending-order inventory (read-only; no checkout)

The separate `--reconcile-pending-orders` command now checks both existing
KS-LE-6 services (Phase F) and OVHcloud purchase orders that have **not**
reached `delivered` or `cancelled` status. It uses only these GET routes:

```text
GET /service
GET /service/*
GET /dedicated/server/*/specifications/hardware
GET /me/order
GET /me/order/*
GET /me/order/*/status
GET /me/order/*/details
GET /me/order/*/details/*
GET /me/order/*/details/*/extension
```

The order-detail **extension** supplies the commercial `order.plan.code`,
which must match `27skle06-v1` or `27skle06`. Distinct ordered items are
grouped by their cart item identifier or OVH billing-domain parent, so
multiple invoice lines for a single server are not automatically counted as
different servers. Two separate KS-LE-6 orders or a quantity of two use
both quota slots. `checking`, `delivering`, `documentsRequested` and
`cancelling` remain open until their status is resolved. For `notPaid` orders,
the scanner also fetches `GET /me/order/{id}` and reads `expirationDate`:
only those whose expiry date is **strictly earlier than the current UTC day**
are excluded as expired unpaid purchase orders. OVHcloud documents unpaid
purchase orders as no longer purchasable after their expiration date. An order
expiring **today** remains open. A verified expiry is reported separately;
this does **not** change any real OVHcloud order or cancel it remotely.

Missing/malformed expiry dates, timestamps without a timezone, order-ID
mismatches, `unknown` or unrecognized statuses, missing product codes/details
for still-open orders, ambiguous duplicates, API failures or oversized order
lists **fail closed**, never returning a false zero. Account identifiers and
purchase descriptions are omitted from CLI output and error messages. Order
scans are bounded to 250 orders with 100 details each, and may encounter
OVHcloud rate limits. The additional order-metadata GET route is already
supported by the client; ensure the API key grants `GET /me/order/*`.

Reference: [OVHcloud — Managing orders](https://docs.ovhcloud.com/en/guides/account-and-service-management/managing-billing-payments-and-services/ovh-orders).

Orders marked `delivered` are excluded from **open-order counts**, because
their provisioned servers should be represented in the service inventory.
Very recent deliveries may not yet appear in `/service` and therefore still
require manual verification. When both a matching service and an open order
exist, the API data used here cannot prove whether they represent the same
server; the program reports a possible count range and **refuses to claim a
verified combined total**. It never assumes that they are distinct and
never authorizes purchases. This is not a complete lifetime order audit.

**Run once manually on Ubuntu**, using the existing root-only credentials
file (the monitor timer does not run this command):

```bash
sudo systemd-run --wait --pipe --collect \
  --unit=ovh-ksle-pending-reconcile \
  -p EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --reconcile-pending-orders
```

If OVHcloud returns `403 NOT_GRANTED_CALL` for order details or extensions,
the read-only key needs the missing `GET` rights listed above. Preserve all
existing permissions when rotating it; **do not add** POST checkout or
payment rights. This command does not create carts, send emails, write
purchase journals, or attempt any purchase. The separate purchase preflight
remains explicitly **partial**, and checkout is still disabled.

## Phase I — commercial audit before considering checkout (no purchase)

Use `--commercial-audit` when an approved KS-LE-6 / 128 GB configuration
appears in the stock feed. It is a **manual, one-shot** command and is **not**
attached to the 30-second monitor timer. In order, it:

1. Requires `OVH_AUTO_BUY_ENABLED=false` and the existing 80 EUR initial,
   37 EUR/month VAT-inclusive ceilings, approved disk configurations and
   Europe/Canada datacenters from `/etc/ovh-ksle-order.env`.
2. Checks the private local purchase journals, then scans **existing services
   and still-open orders** using Phase H (including proven-expired `notPaid`
   handling). Missing records or unidentified overlaps prevent the cart step.
3. Reconfirms the exact live stock FQN/datacenter, prepares **one temporary
   cart** for the highest-priority currently available option, reads the
   `GET /order/cart/{cartId}/checkout` quotation and deletes the cart.
4. Requires a complete, matching initial EUR `withoutTax` + `tax` = `withTax`
   breakdown. The **entire cart's VAT-inclusive initial total**, including
   whatever setup charges OVHcloud has included, must be **at most 80 EUR**.
   The separate setup-fee amount is **not** independently certified.
5. Inspects all `RENEW` component labels and any explicitly VAT-inclusive
   `priceWithTax` fields. An ordinary `RENEW.price` is **not** enough to
   verify the 37 EUR/month VAT-inclusive ceiling. Even complete item-level
   gross labels are not a binding future renewal invoice.
6. Refuses explicitly nonzero commitment terms, and reports whether a
   structured zero-commitment indicator was seen. One-month order duration
   `P1M` by itself **does not prove** freedom from a 12/24-month obligation.
   Seller contract terms still require independent validation.

The result intentionally says `COMMERCIAL AUDIT PARTIAL` whenever an
initial quote passes. It must **never be treated as approval to place or pay
an order**. When the server is out of stock, the command prints `WAITING`
without calling the account or cart endpoints. The current SMTP stock
alerts remain unchanged; **no order, payment, reservation or email is sent**
by this audit.

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
sudo systemd-run --wait --pipe --collect \
  --unit=ovh-ksle-commercial-audit \
  -p EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --commercial-audit
```

No additional OVH API permissions are required beyond the temporary cart
test and Phase H account/order GET permissions already documented. Do not
grant `POST /order/cart/*/checkout`, `POST /me/order/*/pay`, or
`POST /me/order/*/payWithRegisteredPaymentMean`. Keep
`OVH_AUTO_BUY_ENABLED=false`. The temporary cart is not a reservation and
may become unavailable before a human can act. If the cart's DELETE cleanup
fails, inspect the warning and remove it manually if necessary.

## Phase J — automatic commercial audit on new stock (opt-in; NO purchase)

The existing **30-second stock/email timer is unchanged**. A second, separate
**5-minute** systemd timer can now invoke `--commercial-watch` after initial
setup. It has its own root-only state and API backoff, and loads both the
monitor's SMTP configuration and the ordering API credentials. The watcher
does **not** create an order, activate checkout, or pay anything.

- When no approved stock exists, the watcher reports `WAITING` and creates
  **no cart**. When a suitable configuration first appears (or reappears after
  an observed unavailable state), it runs Phase I once for the highest-ranked
  eligible disk/datacenter selection.
- A successfully completed **partial commercial audit** generates **one
  additional plain-text email** showing the real initial quotation and which
  recurring-price/contract proofs are missing. The original availability
  alert continues to be sent separately by the 30-second timer.
- A persisted private JSON journal prevents normal duplicate commercial
  emails across timer invocations/reboots. The report is saved **before**
  SMTP. If a process crashes during delivery, SMTP outcome is treated as
  uncertain: the program **will not resend** automatically, avoiding a
  duplicate. An operator must inspect email/logs before manually resolving
  an uncertain notification.
- Each failed audit backs off for at least **15 minutes**, increasing to
  60 minutes for repeated failures; a recent audit also throttles new
  stock events. The public stock requests have an independent persisted
  HTTP 429/5xx cooldown. No successful audit is repeated while the same
  configuration remains available.
- A `LIMIT REACHED`, inconclusive account inventory, unsafe quote or missing
  agreement indicator can never authorize a purchase. Incomplete audits are
  logged for a later retry, not emailed as accepted purchase quotations.

**Deploy on Ubuntu** (after pulling the latest GitHub commit):

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
sudo install -m 0644 \
  /opt/ovh-ksle-monitor/systemd/ovh-ksle-commercial-watch.service \
  /etc/systemd/system/ovh-ksle-commercial-watch.service
sudo install -m 0644 \
  /opt/ovh-ksle-monitor/systemd/ovh-ksle-commercial-watch.timer \
  /etc/systemd/system/ovh-ksle-commercial-watch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ovh-ksle-commercial-watch.timer
sudo systemctl status ovh-ksle-commercial-watch.timer --no-pager
```

The service expects existing `0600` root-owned files
`/etc/ovh-ksle-monitor.env` (SMTP) and `/etc/ovh-ksle-order.env` (OVH API,
`OVH_AUTO_BUY_MAX_TOTAL_EUR=80.00`, `OVH_AUTO_BUY_MAX_MONTHLY_EUR=37.00`,
`OVH_AUTO_BUY_ENABLED=false`). No API-key rotation or new permissions are
needed if Phase I already works. The root-only watch journal lives at
`/var/lib/ovh-ksle-purchase/commercial-watch.json` (mode `0600`, directory
`0700`). Do **not** edit/delete it to clear an uncertain SMTP outcome.

**Inspect status and recent logs**:

```bash
sudo /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py --commercial-watch-status
sudo journalctl -u ovh-ksle-commercial-watch.service -n 40 --no-pager
```

**Disable the optional watcher without stopping stock alerts**:

```bash
sudo systemctl disable --now ovh-ksle-commercial-watch.timer
```

This new workflow is the **commercial-notification phase**, not automatic
purchasing. OVHcloud supports an actual order-creation operation
`POST /order/cart/{cartId}/checkout`, but this project deliberately omits that
route. A paid order would additionally require payment authorization and
independent, reliable checks of the **monthly total including VAT**, setup
fees, legal **zero commitment**, and a durable global two-server quota.
`P1M` alone is not proof of no commitment. Those commercial assurances are
not yet available to this automation, so real checkout/payment remain
**blocked** regardless of timer settings or API permissions.

## Phase K — future auto-buy decision gate (simulation only)

The commercial audit now appends an `AUTO-BUY BLOCKED (NO PURCHASE)` section
to each completed quotation and to the corresponding **commercial watcher
email**. This is generated by the pure `auto_buy_guard.py` decision engine.
No new OVH endpoint, API permission, email address, systemd service, or
environment variable is needed. Update the repository and keep both existing
timers running as before.

The gate is deliberately stricter than the public catalog or the temporary
cart: it requires **independent proof** of each of these facts before it could
even consider a future purchase eligible for human authorization:

1. Exact KS-LE-6 with 128 GB RAM, one of the four approved disk layouts, and
   a datacenter in the explicitly approved Europe/Canada list;
2. an account inventory and pending-order scan with an unambiguous combined
   count **below two**, and no unreconciled local purchase attempts;
3. an initial **per-server** final charge **at most 80.00 EUR incl. VAT**;
4. a **verified** recurring monthly renewal **at most 37.00 EUR incl. VAT**;
5. **verified** contractual freedom from fixed 12/24-month commitment;
6. a payment method independently validated as usable for the order.

Even if synthetic test data satisfies all six criteria, the result is
`AUTO-BUY DRY RUN`, **never authorization to submit checkout**. The real
commercial audit currently has only an initial cart price and non-binding
renewal/commitment indicators, so its output correctly stays **BLOCKED**.
In particular, `P1M` is a billing duration, not proof of no commitment.

The offline tests cover **two sequential servers**, whether the two disk
layouts are identical or different, and ensure a **third server is blocked**.
This does not assert that two physical servers are actually in stock: each
real order would need its own fresh stock and account reconciliation before
the next attempt. No real submission or payment transport has been added.

Run the existing offline suite after updating:

```bash
cd /opt/ovh-ksle-monitor
python3 -m unittest discover -s tests -q
```

**Do not grant** `POST /order/cart/*/checkout` or payment API rights to the
monitor or watcher. If real automatic purchasing is later explicitly
authorized, that will require a separate implementation, transaction-safe
idempotency, proof of the conditions above, and its own end-to-end safeguards.

## Phase L — read-only payment and contract evidence in commercial emails

When live stock finally permits a temporary cart quote, Phase I / Phase J
now inspect additional **non-billable** evidence before sending the audit
report:

- Existing `GET /me/payment/method` and `GET /me/payment/method/{id}` calls
  check for **one default `VALID` one-click** method. Possible results are
  `READ-ONLY PAYMENT` (method exists), `PAYMENT NOT READY` (none/invalid), or
  `PAYMENT UNVERIFIED` (API failure or missing read rights). A failure to
  find a method does **not suppress** the commercial alert: it is included
  as a blocker, with no card data or payment-method identifiers printed.
- The existing `GET /order/cart/{cartId}/checkout` quotation may contain
  `contracts`. The report states only the **number** of contract references,
  never their contents, URLs or tokens. Merely seeing a contract reference
  or a `P1M` duration is **not proof of a commitment-free subscription**.

A default card's `VALID` and `oneclick` flags **do not prove** that the card
can pay a particular future KS-LE-6 order or that payment will succeed. OVH
only supports its order-specific payment means query once a real order ID
exists (`GET /me/order/{orderId}/availableRegisteredPaymentMean`). The
commercial watcher deliberately creates **no such order**, so the future
auto-buy gate still flags **payment eligibility unverified**, even if a saved
default card is present. Do not grant additional order/payment POST rights.

If desired, run the existing manual read-only payment diagnostic at any time
without stock or a cart:

```bash
sudo systemd-run --wait --pipe --collect \
  --unit=ovh-ksle-payment-readiness \
  -p EnvironmentFile=/etc/ovh-ksle-order.env \
  /usr/bin/python3 /opt/ovh-ksle-monitor/ksle_monitor.py \
  --check-payment-readiness
```

If no method is registered, this command reports an error safely; it does
not create one. To add a method, use the **OVHcloud Console** directly, not
this monitor. The extra evidence in commercial emails is included
automatically after `git pull` on the VPS; the systemd timer files and
environment files do not need changing.

## Phase M — fully offline restock rehearsal and independent health alerts

### One-command end-to-end simulation (NO OVH account/API, SMTP or order)

After updating the Python files, run:

```bash
python3 /opt/ovh-ksle-monitor/ksle_monitor.py --simulate-restock-flow
```

This uses an isolated temporary directory, synthetic OVH stock and cart
responses, and in-memory mock email delivery. It exercises the **actual**
monitoring, disk ranking, commercial audit, persistent deduplication and
pure auto-buy guard code, showing the following fake sequence:

1. No stock → zero notifications;
2. second-priority disks in FRA become available → one stock alert and one
   commercial audit notification;
3. repeated stock → no duplicated notifications;
4. preferred disks in BHS become available → two more fake notifications;
5. account already has 2/2 servers → audit stops before any cart;
6. monthly quote of 38 EUR, initial quote of 81 EUR, or unknown final
   monthly charge → purchasing decision blocked.

All illustrative prices are **fabricated test inputs**, not verified live
OVHcloud prices. Nothing is sent, purchased, or billed. The standalone command
never even loads live OVH credentials. Run the entire offline suite with
`python3 -m unittest discover -s tests -q`.

### Optional health watchdog for stopped/failing timers

The regular stock monitor and the commercial watcher now write separate
private timestamps **only after successful real polling runs**. Normal
`--check`, API 429 cooldown, failed SMTP, or incomplete commercial audit do
not falsify these heartbeats. A third, independent timer checks them every
five minutes and sends an **email on each change** to or from a degraded
condition (no repeated warning every five minutes):

- stock successful-poll heartbeat older than **30 minutes**;
- commercial-watcher heartbeat older than **40 minutes**;
- either missing beyond the **40-minute startup grace**;
- unreadable/malformed/insecure heartbeat, or implausible future timestamp.

This is intentionally tolerant of typical API backoff: a short OVH 429
pause does not immediately trigger a false failure alarm. Real stock alerts
still go out on the existing 30-second service. Health emails contain no
OVH account data or credentials and are **not** stock notifications.

Install on Ubuntu **after** `git pull` (do not replace your existing `.env`
files or change the original two timers):

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
sudo install -m 0644 /opt/ovh-ksle-monitor/systemd/ovh-ksle-health.service /etc/systemd/system/
sudo install -m 0644 /opt/ovh-ksle-monitor/systemd/ovh-ksle-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ovh-ksle-health.timer
sudo systemctl list-timers 'ovh-ksle-*'
sudo journalctl -u ovh-ksle-health.service -n 30 --no-pager
```

To run the first health check manually and inspect its local state:

```bash
sudo systemctl start ovh-ksle-health.service
sudo python3 /opt/ovh-ksle-monitor/ksle_monitor.py --health-status
```

The checker reads the SMTP values from your existing
`/etc/ovh-ksle-monitor.env` and stores its own notification journal in
`/var/lib/ovh-ksle-health/health-state.json`. No extra OVH API permission
is required. If you changed the stock/commercial journal directories,
explicitly set `OVH_STOCK_HEARTBEAT` and `OVH_COMMERCIAL_HEARTBEAT` in the
health service environment to point to the real heartbeat files.

For the stock timer's `DynamicUser=yes` service, systemd can present
`/var/lib/ovh-ksle-monitor` as a managed symbolic link to
`/var/lib/private/ovh-ksle-monitor`. The heartbeat writer explicitly allows
this **exact systemd-managed directory alias** while rejecting unrelated
symlinks. After upgrading a version that logged
`Cannot record stock health heartbeat: Health directory is not a directory`,
run `sudo systemctl start ovh-ksle-monitor.service` and check its journal;
no systemd restart or state-file deletion is required. Once the heartbeat is
current, the next scheduled health check sends one recovery notification.

For an SMTP error with an **uncertain delivery outcome**, the watchdog keeps
`sending` in its private journal and refuses to send another health email
until a human investigates; inspect with `--health-status` and the unit
logs. Do not delete or reset that file blindly: an SMTP timeout can mean
the recipient already received the message. This protects against duplicate
incident email, at the cost of requiring intervention after ambiguity.

**Safety remains unchanged:** `OVH_AUTO_BUY_ENABLED=false`; no checkout or
payment endpoint is implemented. The €80 initial and €37 monthly ceilings,
2-server quota and zero-commitment requirement are conservative blockers,
not a guarantee that an unknown future OVH price is within budget. When
monthly VAT-inclusive renewal or contract terms cannot be independently
verified, the guard **blocks** purchase; it never assumes an unknown price
is cheap enough.

## Phase N — checkout price detail and commitment evidence checklist

The existing **non-billable** `GET /order/cart/{cartId}/checkout` response is
now inspected further, without any new HTTP route, OVH permission, or
background timer. When an eligible KS-LE-6 configuration appears in the
approved datacenters, the commercial audit (manual or five-minute watcher)
will include:

- **Initial amount with VAT**: strict EUR gross/net/tax equality against the
  80.00 EUR ceiling. This reflects the complete temporary cart at that moment.
- **Installation/detail lines**: if checkout provides structured `details`,
  aggregate `INSTALLATION` entries and count `DURATION`, promotional, and
  unknown line types. The listed installation amount is **not** treated as
  VAT-inclusive unless separately proven. A missing or zero installation
  entry is **not proof** that setup will always be free.
- **Monthly renewal**: explicit VAT-inclusive item-level `RENEW` figures,
  when available, are compared with 37.00 EUR; they remain *indicative*
  rather than a certified final monthly invoice.
- **No fixed commitment**: a `P0M`/zero metadata indicator is reported as
  *indicative only*; `P1M` is a billing duration, **not** evidence that the
  customer can cancel without a 12- or 24-month commitment. Any explicit
  fixed-term indicator still blocks the audit.
- **Evidence checklist**: uses unambiguous markers such as
  `INITIAL_VAT=WITHIN_LIMIT`, `SETUP_DETAILS=PRESENT_TAX_UNVERIFIED`,
  `MONTHLY_VAT=UNVERIFIED`, `CONTRACT_TERMS=UNVERIFIED`, and
  `FINAL_DECISION=BLOCKED`.

No descriptions, personal identifiers, contract text, URLs, or tokens from
checkout detail lines are included in the audit email. Malformed or missing
details cause an **unverified** status, not a zero-fee assumption. The 2-server
quota and original purchase blockers are unchanged. Existing commercial
emails are **not resent** just because code was updated: a new stock event
or an explicitly invoked manual audit is required.

Update and test on Ubuntu:

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
cd /opt/ovh-ksle-monitor
python3 -m unittest discover -s tests -q
python3 ksle_monitor.py --simulate-restock-flow
```

If the public availability API still reports no suitable KS-LE-6 server,
`--commercial-audit` will correctly return `WAITING` without creating a
temporary cart: **no real prices or legal commitment terms can be confirmed
until a suitable offer is actually available to quote**. The commercial
watcher will use the richer evidence report automatically on the next
eligible stock edge. This phase deliberately does not enable order creation,
payment, or any fixed-term purchase.

## Phase P — Portugal 23% VAT estimates (catalog only; purchase still blocked)

The owner confirmed **23% mainland Portuguese VAT** as the assumption to
show alongside the public OVHcloud Eco catalog's monthly prices. `--list-prices`
and **future stock-alert emails** now show the original ex-VAT catalog value
plus a VAT-inclusive **estimate**, rounded to EUR cents using decimal
half-up rounding. The current approved four-layout snapshot is:

| Preference | Storage | Catalog without VAT | 23% VAT estimate | Margin under 37 EUR |
| --- | --- | ---: | ---: | ---: |
| 1 | 2 × 512 GB NVMe + 2 × 4 TB HDD | 26.99 EUR | 33.20 EUR | 3.80 EUR |
| 2 | 4 × 960 GB SATA SSD | 27.99 EUR | 34.43 EUR | 2.57 EUR |
| 3 | 3 × 1 TB NVMe | 28.99 EUR | 35.66 EUR | 1.34 EUR |
| 4 | 2 × 1.92 TB NVMe | 29.99 EUR | 36.89 EUR | 0.11 EUR |

The figures above were supplied by the operator's catalog output on
20 November 2026 and **can change**. The CLI always uses its current
validated local cache, refreshing on the existing 6-hour schedule.
Malformed, missing or non-EUR strings are never converted to a VAT estimate.
No additional OVH credentials, permissions, systemd timers, or configuration
files are needed. Keep in mind that the applicable VAT treatment still has to
match the final invoice for this exact account; 23% here is a **display
assumption**, not a claim from the OVH checkout.

These estimates do **not** prove the actual monthly renewal price, fixed-term
contract conditions, first charge including any installation fees, or
availability. The approved caps remain **37 EUR/month including VAT** and
**80 EUR initial including VAT per server**, with **zero fixed commitment**.
`OVH_AUTO_BUY_ENABLED=false`; order/payment endpoints remain unavailable.
The final commercial audit requires a live eligible offer and a temporary
OVH cart quote; unknown legal/payment conditions continue to block purchase.

Update and test:

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
cd /opt/ovh-ksle-monitor
python3 -m unittest discover -s tests -q
python3 ksle_monitor.py --list-prices
```

## Phase Q — indicative initial charge: one month plus 26.99 EUR setup

The owner confirmed choosing the **without-commitment** monthly option for
the KS-LE-6. The OVHcloud Eco PT public listing also advertises a **26.99 EUR
+ VAT base installation fee** for this model. The 12/24-month prepayment
promotions are NOT selected or used to calculate the reference price.

`--list-prices` and new stock-alert emails now display an **illustrative**
first payment: one month at the current monthly catalog price, plus the
published base setup fee, each converted to 23% VAT and rounded to EUR cents.
With the owner's 20 November 2026 catalog snapshot:

| Rank | Monthly incl. VAT (estimate) | Setup incl. VAT (base estimate) | First payment incl. VAT (estimate) | Margin under 80 EUR |
| --- | ---: | ---: | ---: | ---: |
| 1. 2 × 512 GB NVMe + 2 × 4 TB HDD | 33.20 EUR | 33.20 EUR | **66.40 EUR** | 13.60 EUR |
| 2. 4 × 960 GB SATA SSD | 34.43 EUR | 33.20 EUR | **67.63 EUR** | 12.37 EUR |
| 3. 3 × 1 TB NVMe | 35.66 EUR | 33.20 EUR | **68.86 EUR** | 11.14 EUR |
| 4. 2 × 1.92 TB NVMe | 36.89 EUR | 33.20 EUR | **70.09 EUR** | 9.91 EUR |

These values assume the same base installation fee applies to every disk
layout, that exactly one month is invoiced in advance and that no other
charges apply. **Neither assumption is guaranteed by the public catalog.**
If OVHcloud changes the offer, taxes, selected datacenter or setup fee, the
figures may differ. The 37 EUR recurring and 80 EUR initial ceilings are
still tested against actual eligible cart evidence, never these estimates.

The owner's observation that the monthly catalog offer is **sem compromisso**
is recorded as a preference; it is not automatically upgraded to verified
legal contract evidence. `P1M` means the billing duration, not necessarily
absence of a 12/24-month contractual obligation. When stock becomes
available, the commercial audit still needs an authoritative quote and
contract validation. The real **checkout and all payment actions remain
disabled** (`OVH_AUTO_BUY_ENABLED=false`).

No new API rights or systemd services are required. After updating, run
`python3 ksle_monitor.py --list-prices` to inspect the revised estimates.

## Phase R — owner-confirmed no-commitment price reference and drift detection

The owner personally checked the KS-LE-6 **without a 12- or 24-month fixed
commitment** prices on 20 November 2026. They confirmed 23% mainland Portugal
VAT, a 26.99 EUR ex-VAT setup charge and the four monthly ex-VAT prices of
26.99, 27.99, 28.99 and 29.99 EUR, respectively, in the established disk
priority order. The reference assumes **one month paid upfront plus setup**,
without additional charges. The owner-approved hard limits remain **37 EUR
monthly** and **80 EUR initial**, both including VAT, per server.

The new completely offline command prints these owner-confirmed reference
values even when every datacenter is out of stock:

```bash
cd /opt/ovh-ksle-monitor
python3 ksle_monitor.py --confirmed-terms
```

When stock returns, a manual `--commercial-audit` or the existing five-minute
commercial watcher will additionally compare the temporary cart's VAT-inclusive
initial total and *available, consistent* item-level RENEW data against the
owner's reference for the **exact storage configuration**. If a price has
increased, even while still below the absolute budget limit, the report shows
`ABOVE_CONFIRMED_REFERENCE` and flags the variation for owner review. If the
cart lacks usable VAT-inclusive renewal evidence, it says `MONTHLY=UNVERIFIED`.
Lower prices may reflect a promotion, **not** approval of any fixed commitment.
The comparison does not claim that item-level renewal prices are binding or
that `P1M` proves the right to cancel at any time.

Stock notifications and the commercial watcher keep their current timers and
duplicate prevention; no new API access is needed. Most importantly, no
checkout or payment route has been added, and the auto-buy switch remains
disabled. A live eligible configuration is still necessary before the audit
can retrieve an actual temporary cart quotation.

Update and verify:

```bash
sudo git -C /opt/ovh-ksle-monitor pull --ff-only
cd /opt/ovh-ksle-monitor
python3 -m unittest discover -s tests -q
python3 ksle_monitor.py --confirmed-terms
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `SMTP_HOST, SMTP_FROM and SMTP_TO must be configured` | Fill the `/etc/ovh-ksle-monitor.env` settings before starting the service. |
| Email fails to authenticate | Check user/app password and whether the SMTP provider permits client submission. |
| TLS failure | Check mail hostname, server certificate, and STARTTLS (587) vs implicit TLS (465). |
| No stock alert | Run `python3 ksle_monitor.py --list`; if values are `unavailable` or `unknown`, no alert is expected. |
| API request fails | Check DNS, outbound HTTPS, proxy/firewall and API status; run `--check`. |
| Same option triggers again later | This is intended if the API reported it unavailable in between; each restock is a new event. |
| No repeat after a restock | If the item went out of stock and returned between checks, no unavailable state was observed. Faster polling reduces but cannot eliminate this. |

## Notes

- Application code and configuration contain no SMTP credentials. Keep the populated environment file private and out of version control.
- Neither API availability nor email delivery implies an item is held for you.
- No auto-buy is performed; only manually invoked temporary-cart or payment-readiness checks use OVH account credentials.

The Eco no-offer diagnostic includes only matching-plan field names and nested structure; it never logs full response values.
