# Secret handling

Rules (enforced from day one so we don't get burned later):

1. **Secrets live only in `engine/.env`**, which is `.gitignored` and never committed.
   `engine/.env.example` documents the key *names* (no values) and IS committed.
2. **Never** put a key in source code, a prompt, a log, a trace, or a commit. (Model calls,
   scraping, and telemetry must redact.)
3. **`.env` is loaded at the app/script entrypoint** (e.g. `load_dotenv()`), not inside library
   code, so library modules stay side-effect-free.
4. **If a key is exposed, rotate it.** Pasting a key into a chat/transcript counts as exposed.
   - The OpenAlex key is a free, low-value key (worst case: someone burns your free daily
     budget). Low risk, but rotate it if you want to be clean.
   - Higher-value keys (Featherless has real credit $, Anthropic bills your card): prefer NOT
     pasting them in chat. Best options: add them to `engine/.env` yourself in a terminal
     outside this session, or paste and rotate afterward. They can be regenerated anytime.
5. **Production** (later): move to a real secret manager (cloud KMS or self-hosted OpenBao, per
   the original spec), per-user/tenant data-encryption keys, and least-privilege scopes. Not
   needed for local dev, but the code should read keys from the environment so the swap is trivial.
6. **The iOS app never holds third-party API keys.** It talks to our backend; the backend holds
   the keys server-side. (A key shipped in an app binary is a key that gets extracted.)
