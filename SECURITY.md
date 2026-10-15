# Accounts, access and payments

## Operator setup (Railway)

Use Railway's private Variables interface, not chat or Git, for credentials.

- `DATABASE_URL`: PostgreSQL connection URL; require transport TLS for external databases. Production refuses SQLite fallback.
- `APP_ENV=production`, `APP_ORIGIN=https://your-exact-domain`: fixed canonical origin, not derived from Host headers.
- `AUTH_SECRET`: randomly generated, stable secret of at least 32 characters. Rotation invalidates sessions/device signatures and changes the trial signal hashes, so retain it and migrate hashes before rotation if trial continuity is required.
- Human check (no third-party keys): signup/login require a server-issued drag-to-target and distorted-word challenge. Each challenge is random, expires after 3 minutes, is bound to the requesting IP and action, and is consumed on the first attempt. Submit stays disabled until both steps are done. A missing or failed challenge fails closed.
- `ADMIN_USERS`: comma-separated lower-case existing usernames. Listed usernames are reserved against public signup to prevent privilege squatting, and the account is flagged `admin` in `/auth/status`. It no longer gates the proxy list: every account manages its own list, which is the only model this deployment has. Create the intended account before adding its name to `ADMIN_USERS`.
- `ADMIN_PROMO_CODE`: optional override for the access code that turns an account into an administrator. Blank uses the constant compiled into `accounts.py`. The code is compared with `hmac.compare_digest`, is throttled to five attempts per hour per account, exists only in a server module, and is never served to a browser, logged, or echoed in a response -- a test walks `static/` and the served pages to keep it that way. Rotating it leaves existing administrators in place.
- `TRUSTED_PROXY_CIDRS`: only the Railway-confirmed ingress CIDRs. Uvicorn must run with `--no-proxy-headers`. Without verified ingress configuration users may share the proxy's apparent IP and therefore lose trial eligibility. Never trust arbitrary X-Forwarded-For or wildcard CIDRs.
- `AUTH_ALLOWED_ORIGINS`: optional exact additional origins. Leave empty in production unless genuinely needed.

The proxy list lives in `proxy_store` in the accounts database, with `data/proxies.json` as a local cache: a container filesystem is rebuilt from the image on every deploy, and a list that disappears there reads as a broken run rather than a lost file. Whichever copy was written last wins at boot, the database is written first on every save, and a database that is unreachable logs a warning and leaves the file working. `STORE_PATH` therefore only decides where the cache sits.

Apply schema changes at startup with one application process initially. Take PostgreSQL backups and retain `trial_claims` and `issued_addresses`: deleting/resetting the DB invalidates both the one-trial and no-address-reuse policy. Migrations run at startup: `CREATE TABLE IF NOT EXISTS` for new tables, plus an additive column check (`accounts.MIGRATIONS`) that inspects the catalog and issues `ALTER TABLE ... ADD COLUMN` only for columns that are missing, on both PostgreSQL and SQLite. A genuinely failing ALTER still raises. Future incompatible changes (renames, drops, type changes) require versioned migrations before deployment.

Passwords: Argon2id, minimum 12 characters. Sessions: random bearer cookie, HttpOnly/SameSite Strict/Secure on HTTPS, hashed server-side; seven-day expiry and explicit logout invalidation. Mutations require exact Origin and synchronizer CSRF token. Tokens never enter browser storage. Account data is private/no-store. Registration/login/API throttles are persisted, request bodies capped, concurrency/retries bounded. Source files, env files, schema docs and payment secrets have no public routes. CSP currently permits inline scripts/styles because legacy pages use them; moving these to separate scripts/nonces is needed for a stricter CSP. No claim of total security or AI-proof challenges.

## Trial and ownership

An eligible signup holds 60 seconds of trial time that does not begin until the account's **first run** (`POST /snipe` or `POST /scan`) opens a worker — creating an account and walking away costs nothing. Until then the account reports `trial_pending: true`, `plan: "trial"` and the full 60 seconds remaining, and `/snipe`, `/scan` and `/generate` are allowed. The clock is armed once, per account, under a lock, at the moment the run is registered and before its worker is detached; a second run reuses the running clock and never extends it. Logging in never renews it. An already-seen keyed IP hash OR signed device cookie disqualifies a repeat trial (`trial_available = 0`, no access at all, exactly as before). Both signals are remembered even for disqualified accounts, making IP-only or cookie-only changes insufficient. A new IP AND new device cannot be conclusively identified as the same person. Shared/NAT IPs can be unfairly disqualified. No invasive fingerprinting or fake VPN detection is claimed. Stronger identity/payment verification requires a separate product decision.

New check/generate requests receive 402 on expiry. Detached workers have an expiry guard that signals and cancels ongoing work; logout doesn't renew access. Runs and their streams are owner-scoped; hits and alert settings are per-account in PostgreSQL. The process's shared worker pool permits one active scan/snipe per kind; it is not a horizontally scalable job queue, and runtime runs do not survive process restart. Persistent hits/accounts/invoices do survive restart.

There is no password recovery, email verification, MFA, or user account-deletion UI yet. Do not sell this as an audited, feature-complete identity service. Priority support is an entitlement flag for Forever; no staffed support contact/ticket system has been configured.

## Access, the paywall and grants

Access is one question: does this account hold a plan, or a trial that has not run out. An administrator holds access implicitly -- granting from a panel you cannot open would be a trap.

- **Pages**: `/console`, `/hits/`, `/platforms`, `/proxies/`, `/alerts` and `/settings/` redirect (302) to `/buy` for a session without access, and for no session at all. `/buy`, `/account`, `/docs`, `/` and the whole `/auth`, `/billing` and `/promo` surface stay open, because a paywall with no way through is a dead end. The sidebar mirrors the same rule, and re-renders when the session changes.
- **Endpoints**: `/runs`, `/hits`, `/proxies` (including `/proxies/check` and `/proxies/prune`), `/settings`, `/claim` and `/scan`, `/snipe`, `/generate` answer 402 without access. The rule lives in `AccountMiddleware`, next to login, CSRF and Origin, so hiding a tab is presentation and the server is the authority.
- **The administrator flag** comes from `ADMIN_USERS` or from redeeming `ADMIN_PROMO_CODE`. There is no self-service path to it beyond the code, which is why the code is treated as a credential.
- **Grants** (`POST /admin/grant`) are explicit, logged, and per-account: `monthly` extends from today or from the remaining time, whichever is later, so granting twice never shortens a subscription; `forever` sets lifetime; `trial` hands back an unstarted trial; `revoke` clears paid, lifetime and any unstarted trial together. Nothing runs on a timer and nothing infers a payment. Specifying `order_id` also marks that invoice released, and only when it belongs to the account being granted.
- **No horizontal scale assumption**: grants are transactional and advisory-locked per account, so two administrators acting at once cannot interleave.

## Crypto: operator wallets in the environment

The requirement for free, unlimited/no-rate-limit public blockchain APIs is **not met**. Solana public RPC is explicitly rate-limited, Ethereum public providers impose quotas, and self-hosting Bitcoin/Litecoin/Ethereum/Solana nodes costs infrastructure. All chains still have transaction fees. This code does not run a node and does not watch a chain itself; invoices and reconciliation are the processor's (below).

Where the money goes is the operator's own: one Railway variable per coin, `SOL`, `LTC`, `BTC`, `ETH`, `USDT`, `USDC`. Each holds **either** a deposit address, used verbatim, **or** a BIP-39 recovery phrase from which `wallets.py` derives the first standard account (`m/84'/0'/0'/0/0` BTC, `m/84'/2'/0'/0/0` LTC, `m/44'/60'/0'/0/0` ETH and USDC, `m/44'/501'/0'/0'` SOL and USDT). Only the public address leaves the process: a phrase is never logged, never returned to a client, and never written to the database. Nothing is signed and no private key is loaded, so a compromise of this server cannot move funds. A coin whose variable is empty or unreadable is refused at checkout by name rather than invoiced. Verify a derived address against your wallet once before taking money on it.

The derivation is pure standard library (secp256k1, Ed25519, BIP-32/39/44/84, SLIP-0010, base58, bech32, Keccak-256) and every layer is pinned to its published test vector in `tests/test_wallets.py`; no wallet package was added to `requirements.txt`.

An optional NOWPayments adapter implements processor-issued invoices and provider-managed payout to those addresses. It has service/network fees and must be explicitly approved and configured before enabling it:

- `NOWPAYMENTS_ENABLED=1`, `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`.
- Configure/verify merchant settlement wallets in the provider dashboard. Per-invoice `payout_address` comes from the coin's own environment variable; its acceptance and actual settlement must be tested live. No API credentials or merchant account are configured here.
- Register `https://your-domain/billing/webhook`. Callbacks require sorted canonical JSON HMAC-SHA512. Background polling reconciles lost callbacks after restarts.
- SOL/Solana, LTC/Litecoin, ETH/Ethereum, BTC/Bitcoin, USDT/Solana (`usdtsol`), USDC/Ethereum (`usdc`). Availability, minimums, and network codes are checked by the API but final live route compatibility remains unverified. Never silently substitute a network.
- Monthly $8.99 is **manual crypto renewal**, extending one calendar month. Forever $45.99 is one-time lifetime access to this service. There is no automatic crypto debit.
- Unique address ledger is chain-scoped, durable, permanent; ETH/USDC share Ethereum and SOL/USDT share Solana. Addresses are published only after an atomic unique insert. A reused provider address is withheld and the invoice is marked for review. The processor is not guaranteed to generate a different address and may reuse addresses elsewhere; the ledger guarantees only that this application won't publish the same address again. Restore backups and preserve the ledger forever.
- Checkout displays 60 minutes. Records continue polling for two more hours silently. Expiry doesn't delete or reuse addresses. Reopening after expiry can request a new invoice; it never auto-creates financial operations merely due to a timer. Provider expiry/quote validity is separate and may differ from the local display.
- Confirming/confirmed/partial payments do not grant access. Sufficient `actually_paid`, matching order ID/currency/network/address/USD price, and terminal `finished` are required. Grant happens once in a transaction, even for duplicate callbacks. Payments first observed outside the three-hour window are review-only; payments detected inside it continue polling while awaiting confirmations, even after the window ends. Live webhook canonicalization and processor finished/fees semantics require a sandbox/low-value production acceptance test before enabling real money.
- Forwarding is provider-managed, not web-server signing. Network fees/dust/minimums mean 'every cent' cannot be delivered net. No tested wallet sweep or withdrawal automation is claimed.

`POST /proxies/check` is the one endpoint that makes a request somewhere the user chose: it asks each stored proxy (or a paste that was never stored) to fetch `PROXY_CHECK_URL`, which defaults to `api.ipify.org` — an address that answers with the exit IP and sees nothing but that request. It is owner-scoped, capped by `PROXY_CHECK_MAX`, limited to one check per account at a time, and it reports an answer without changing the list. Nothing about a proxy is stored: results live in the page that asked for them. Do not point `PROXY_CHECK_URL` at an endpoint that treats repeated anonymous requests as abuse, and treat the target as public — it will see every exit in the list.

### Tor: the fallback for an account with no proxy list

When the stored list is empty, a run goes out over Tor instead of stopping at
`no proxies saved yet`. That is a whole category of traffic leaving this process
by a different route, so what it does and does not do is stated here rather than
left to be discovered.

- **A port is not a Tor.** Something listening on `127.0.0.1:9050` is read as a
  SOCKS5 proxy, not as evidence. An exit is labelled `Tor exit` only after Tor's
  own checker answers `IsTor: true` for a request made *through* that port;
  anything that will not confirm is reported as `SOCKS5, unconfirmed`, and one
  that confirms it is *not* Tor is reported as exactly that. No exit is ever
  called Tor on the strength of its port number.
- **Loopback only, client only, no control port.** Discovery looks at
  `TOR_HOST` (loopback by default). The instance this service starts writes its
  own torrc: one `SocksPort 127.0.0.1:<port>` per listener (loopback spelled out,
  never a wildcard), `ClientOnly 1` so it never relays, and `ControlPort 0` so
  there is no control socket and therefore no cookie or password to leak and
  nothing else on the host can signal it. A tor that was already running in the
  environment is used and **never stopped** by this service; only a process this
  service started is terminated on shutdown.
- **What the network sees.** Every request a fallback run makes goes to three
  volunteer relays and leaves from a shared exit address, so the *target* sees an
  address that thousands of other Tor users also use — that is the point of it,
  and it is also why platforms refuse Tor exits far more often. DNS for those
  requests is resolved at the exit (`socks5h`), so the names being checked are not
  handed to this host's resolver. Everything else about a run is unchanged: the
  same platforms, the same names, the same logs.
- **What it is not.** It is not anonymity for the account using it, not a proxy
  service for others, and not a substitute for a private list. Do not describe it
  as either. A pasted list always takes precedence, and `TOR_ENABLED=0` removes
  the fallback entirely. Running a Tor *client* is also a deployment choice worth
  reading your provider's terms over: Railway's AUP forbids operating
  proxy/anonymisation *services*, which this is not, but ask them before relying
  on that reading.
- **Credentials and files.** The torrc, the data directory and tor's own log live
  under `TOR_DATA_DIR` and are written by this service. No proxy credentials, no
  Tor control-password and no cookie are handled here, and the log tail exposed
  to the page is bounded and never contains a secret.

## Verification

`python -m unittest discover -s tests -v` runs account/trial/CSRF/ownership/body-size/rate-limit/payment/address-ledger, wallet-derivation and proxy-check regression tests. Set `TEST_DATABASE_URL` to a **dedicated empty test database** to run them on PostgreSQL; tests clear their own tables. Do not point it at production. CAPTCHA/provider results are simulated in tests; no test bypass is shipped to the server.

Local setup without external credentials displays explicit setup notices; signup/login and buy remain disabled. Interactive browser sign-off of the drag/word challenge on real phones and desktops, live blockchain confirmation, fees/merchant forwarding, IPv4/IPv6 ingress attribution and priority support handling require operator setup/user acceptance tests.

Sources: [OWASP authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html), [NOWPayments API](https://documenter.getpostman.com/view/7907941/S1a32n38), [Solana public RPC](https://solana.com/docs/references/clusters), [Ethereum node operation](https://ethereum.org/developers/docs/nodes-and-clients/run-a-node/).
