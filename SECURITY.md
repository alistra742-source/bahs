# Accounts, access and payments

## Operator setup (Railway)

Use Railway's private Variables interface, not chat or Git, for credentials.

- `DATABASE_URL`: PostgreSQL connection URL; require transport TLS for external databases. Production refuses SQLite fallback.
- `APP_ENV=production`, `APP_ORIGIN=https://your-exact-domain`: fixed canonical origin, not derived from Host headers.
- `AUTH_SECRET`: randomly generated, stable secret of at least 32 characters. Rotation invalidates sessions/device signatures and changes the trial signal hashes, so retain it and migrate hashes before rotation if trial continuity is required.
- `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`: create a Turnstile widget for the exact production hostname. Both signup/login verify success, action and hostname server-side. Missing credentials fail closed; no production bypass.
- `ADMIN_USERS`: comma-separated lower-case existing usernames. Only these accounts can modify the shared proxy pool; ordinary users never see proxy passwords. Listed usernames are reserved against public signup to prevent privilege squatting. Create the intended account before adding its name to `ADMIN_USERS`.
- `TRUSTED_PROXY_CIDRS`: only the Railway-confirmed ingress CIDRs. Uvicorn must run with `--no-proxy-headers`. Without verified ingress configuration users may share the proxy's apparent IP and therefore lose trial eligibility. Never trust arbitrary X-Forwarded-For or wildcard CIDRs.
- `AUTH_ALLOWED_ORIGINS`: optional exact additional origins. Leave empty in production unless genuinely needed.

Apply schema changes at startup with one application process initially. Take PostgreSQL backups and retain `trial_claims` and `issued_addresses`: deleting/resetting the DB invalidates both the one-trial and no-address-reuse policy. Migrations use CREATE IF NOT EXISTS; future incompatible changes require versioned migrations before deployment.

Passwords: Argon2id, minimum 12 characters. Sessions: random bearer cookie, HttpOnly/SameSite Strict/Secure on HTTPS, hashed server-side; seven-day expiry and explicit logout invalidation. Mutations require exact Origin and synchronizer CSRF token. Tokens never enter browser storage. Account data is private/no-store. Registration/login/API throttles are persisted, request bodies capped, concurrency/retries bounded. Source files, env files, schema docs and payment secrets have no public routes. CSP currently permits inline scripts/styles because legacy pages use them; moving these to separate scripts/nonces is needed for a stricter CSP. No claim of total security or AI-proof challenges.

## Trial and ownership

An eligible signup gets 60 seconds from creation, based on server time. Logging in never renews it. An already-seen keyed IP hash OR signed device cookie disqualifies a repeat trial. Both signals are remembered even for disqualified accounts, making IP-only or cookie-only changes insufficient. A new IP AND new device cannot be conclusively identified as the same person. Shared/NAT IPs can be unfairly disqualified. No invasive fingerprinting or fake VPN detection is claimed. Stronger identity/payment verification requires a separate product decision.

New check/generate requests receive 402 on expiry. Detached workers have an expiry guard that signals and cancels ongoing work; logout doesn't renew access. Runs and their streams are owner-scoped; hits and alert settings are per-account in PostgreSQL. The process's shared worker pool permits one active scan/snipe per kind; it is not a horizontally scalable job queue, and runtime runs do not survive process restart. Persistent hits/accounts/invoices do survive restart.

There is no password recovery, email verification, MFA, or user account-deletion UI yet. Do not sell this as an audited, feature-complete identity service. Priority support is an entitlement flag for Forever; no staffed support contact/ticket system has been configured.

## Crypto: deliberately disabled until setup is agreed

The requirement for free, unlimited/no-rate-limit public blockchain APIs is **not met**. Solana public RPC is explicitly rate-limited, Ethereum public providers impose quotas, and self-hosting Bitcoin/Litecoin/Ethereum/Solana nodes costs infrastructure. All chains still have transaction fees. This implementation does not read six wallet seed phrases or sweep private wallets. Do not add wallet seeds to Railway for this code.

An optional NOWPayments adapter implements processor-issued invoices and provider-managed payout to the specified public owner wallets. It has service/network fees and must be explicitly approved and configured before enabling it:

- `NOWPAYMENTS_ENABLED=1`, `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`.
- Configure/verify merchant settlement wallets in the provider dashboard. Per-invoice payout fields use the user-supplied public wallets; their acceptance and actual settlement must be tested live. No API credentials or merchant account are configured here.
- Register `https://your-domain/billing/webhook`. Callbacks require sorted canonical JSON HMAC-SHA512. Background polling reconciles lost callbacks after restarts.
- SOL/Solana, LTC/Litecoin, ETH/Ethereum, BTC/Bitcoin, USDT/Solana (`usdtsol`), USDC/Ethereum (`usdc`). Availability, minimums, and network codes are checked by the API but final live route compatibility remains unverified. Never silently substitute a network.
- Monthly $8.99 is **manual crypto renewal**, extending one calendar month. Forever $45.99 is one-time lifetime access to this service. There is no automatic crypto debit.
- Unique address ledger is chain-scoped, durable, permanent; ETH/USDC share Ethereum and SOL/USDT share Solana. Addresses are published only after an atomic unique insert. A reused provider address is withheld and the invoice is marked for review. The processor is not guaranteed to generate a different address and may reuse addresses elsewhere; the ledger guarantees only that this application won't publish the same address again. Restore backups and preserve the ledger forever.
- Checkout displays 60 minutes. Records continue polling for two more hours silently. Expiry doesn't delete or reuse addresses. Reopening after expiry can request a new invoice; it never auto-creates financial operations merely due to a timer. Provider expiry/quote validity is separate and may differ from the local display.
- Confirming/confirmed/partial payments do not grant access. Sufficient `actually_paid`, matching order ID/currency/network/address/USD price, and terminal `finished` are required. Grant happens once in a transaction, even for duplicate callbacks. Payments first observed outside the three-hour window are review-only; payments detected inside it continue polling while awaiting confirmations, even after the window ends. Live webhook canonicalization and processor finished/fees semantics require a sandbox/low-value production acceptance test before enabling real money.
- Forwarding is provider-managed, not web-server signing. Network fees/dust/minimums mean 'every cent' cannot be delivered net. No tested wallet sweep or withdrawal automation is claimed.

## Verification

`python -m unittest discover -s tests -v` runs account/trial/CSRF/ownership/body-size/rate-limit/payment/address-ledger regression tests. Set `TEST_DATABASE_URL` to a **dedicated empty test database** to run them on PostgreSQL; tests clear their own tables. Do not point it at production. CAPTCHA/provider results are simulated in tests; no test bypass is shipped to the server.

Local setup without external credentials displays explicit setup notices; signup/login and buy remain disabled. Interactive real Turnstile signup/login, live blockchain confirmation, fees/merchant forwarding, IPv4/IPv6 ingress attribution and priority support handling require operator setup/user acceptance tests.

Sources: [Turnstile Siteverify](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/), [OWASP authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html), [NOWPayments API](https://documenter.getpostman.com/view/7907941/S1a32n38), [Solana public RPC](https://solana.com/docs/references/clusters), [Ethereum node operation](https://ethereum.org/developers/docs/nodes-and-clients/run-a-node/).
