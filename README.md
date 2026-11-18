# OVHcloud KS-LE-6 availability monitor

Standalone Python 3 service for Linux. It checks the official, public OVHcloud stock API every three minutes and sends an SMTP email when **any** KS-LE-6 storage configuration becomes available at **any** datacenter. Your preferred option is highlighted first:

- **Preferred:** 2 × 512 GB NVMe + 2 × 4 TB HDD (`hybridsoftraid-2x4000sa-2x500nvme`)
- Also alerts: 2 × 1.92 TB NVMe; 3 × 1 TB NVMe; 4 × 960 GB SATA SSD.
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

This application uses `SMTP_*` systemd environment variables (not Django's `EMAIL_HOST` settings). OVH's port **587** uses **STARTTLS**, so set `SMTP_SECURITY=starttls` (not `ssl`). The email sender is `Basketly <donotreply@basketly.eu>` and the envelope SMTP account can be `donotreply@basketly.eu` if it has sending permission. Set `SMTP_TO` to **your actual recipient address**.

Create a **local, root-only** environment file named **`/etc/ovh-ksle-monitor.env`**, *outside the Git repository*. Example:

```ini
SMTP_HOST=smtp.mail.ovh.net
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=donotreply@basketly.eu
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

## Change polling frequency

Edit the installed timer's `OnCalendar` expression. For example, `*:0/5` is every 5 minutes. Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ovh-ksle-monitor.timer
```

Avoid overly aggressive polling; respect OVHcloud's service terms and any future rate limits. This package defaults to three minutes.

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
unavailable, the API may reject the add-to-cart step, which is an expected
non-purchasing test result.

This feature is completely separate from the regular `systemd` monitor timer.
The timer does not read the token or attempt to order anything.

### Authentication

Create a narrowly scoped OVHcloud EU API Bearer token, using the official
[OVHcloud developer console](https://developers.eu.ovhcloud.com/).
Give it permissions only for temporary cart operations: `GET` the public Eco
catalog and cart Eco offers, `POST` cart creation/assignment, Eco item/options,
item configurations, `GET` cart checkout and `DELETE` the test cart. **Do not
grant POST checkout or payment permissions.**

Never put your token in GitHub, in `config.example`, or in the monitoring
service environment. Put it in a separate root-only local file:

```bash
sudo install -m 0600 order-config.example /etc/ovh-ksle-order.env
sudo nano /etc/ovh-ksle-order.env
sudo chown root:root /etc/ovh-ksle-order.env
sudo chmod 600 /etc/ovh-ksle-order.env
```

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
- No auto-buy or OVH account login is performed.
