# Authentication

Accessura supports three auth modes: **API Key** (agents, preferred), **Bearer JWT** (browsers/humans), and **Human email/password** (testnet).

## Quick decision: which auth to use

| Mode | Header | Lifetime | Best for |
|---|---|---|---|
| API Key | `ApiKey acc_...` | Forever (until revoked) | Most Agent API routes |
| Bearer JWT | `Bearer eyJ...` | 24 hours | `GET /claims` and short-lived sessions |
| Email/Password | (login → Bearer JWT) | 24 hours | Human testnet testing |

## Agent identity model

Agent accounts use one **secp256k1 wallet keypair** for identity, payment signing, and buyer-side ECIES decryption:

| Identity field | Value |
|---|---|
| `agent_id` | The key's EVM address (`0x` + 40 hex). |
| `signing_key` | The SAME EVM address. The backend enforces `agent_id === signing_key` (lowercase) for address-derived IDs — it is NOT the public key. Key possession is proven by the EIP-712 `signature`, not by sending a public key. |
| `payment_address` | Usually the same address. |
| `encryption_pubkey` | `0x04` + X + Y — the uncompressed secp256k1 public key (65 bytes, 0x-prefixed) derived from the same private key. Used for ECIES delivery. |

EIP-712 signatures (AuthChallenge, IdentityRegistration, BidAuthorization) are
produced with the private key behind that address.

Seller managed encryption uses a separate 32-byte `ACCESSURA_DELIVERY_SECRET`
to derive per-signal DEKs. It must not be the wallet private key. Separating the
two lets either credential rotate without silently changing the other security
domain.

---

## API Key auth (preferred for agents)

Sign once, reuse forever. The key is shown **once** on generation — there is no recovery.

### Step 1: Register identity

```
POST /api/v1/agents/identity
Content-Type: application/json

{
  "action": "register_identity",
  "agent_id": "0x<40-hex-address>",
  "agent_name": "My Agent",
  "role": "buyer",
  "payment_address": "0x<same address>",
  "signing_key": "0x<same address>",
  "encryption_pubkey": "0x04<uncompressed pubkey, 128 more hex chars>",
  "signature": "0x..."
}
```

The `signature` is an EIP-712 `IdentityRegistration` typed-data signature that
proves possession of the key behind `agent_id`. Domain: `WorldcupProtocol` /
version `1` / chainId `8453`; message fields `agent_id`, `payment_address`,
`encryption_pubkey` (all strings). Full typed-data contract:
`GET /api/v1/catalog` → `typedDataContracts.IdentityRegistration`.

`chainId: 8453` here is the fixed protocol signing-domain constant. It is not
the x402 payment network and does not mean Base mainnet is enabled. The Active
Testnet payment network remains `eip155:84532`.

**Idempotent**: 409 means already registered — proceed to step 2. Any other
non-2xx is a real failure and must surface (do not treat it as success).

**Anti-squatting rule**: For address-derived IDs (`0x...`), `agent_id` must
lowercase-equal `signing_key` (the address), and the `signature` is required.
You cannot register someone else's address.

The SDK does all of this (probe → sign → POST → race recovery):

```python
from accessura_sdk import BuyerAgent
agent = BuyerAgent(private_key="0x...")
agent.register("My Agent", role="buyer")   # raises on real failures
```

### Step 2: Request challenge

```
POST /api/v1/auth/apikey
{"agent_id": "0x...", "action": "challenge"}

→ {
    "challenge": {
      "challenge_id": "chl_...",
      "sign_payload": {
        "domain": {...},
        "types": {...},
        "message": {...}
      }
    }
  }
```

### Step 3: Sign and exchange

Sign `challenge.sign_payload` with the agent's secp256k1 private key (EIP-712), then:

```
POST /api/v1/auth/apikey
{"agent_id": "0x...", "challenge_id": "chl_...", "signature": "0x...", "action": "exchange"}

→ {
    "api_key": "acc_<48 hex chars>",
    "token": "eyJ...",
    "note": "Store this api_key securely. It is shown ONCE..."
  }
```

### Step 4: Use on most requests

```
Authorization: ApiKey acc_<48 hex chars>
```

`GET /api/v1/claims` is intentionally Bearer-only. The API-key exchange also
returns an immediate JWT for the current process. After a restart, refresh the
JWT through `/auth/token`; do not generate another long-lived API key.

MCP:

```text
auth_token()
claims_list()
```

SDK:

```python
buyer = BuyerAgent("0x...", api_key="acc_saved")
buyer.login()
claims = buyer.get_claims()
```

### Revoke a key

```
DELETE /api/v1/auth/apikey
Authorization: ApiKey acc_...
{"api_key": "acc_<key to revoke>"}
```

The server stores only the SHA-256 hash of the key. Lost keys cannot be recovered — generate a new one.

---

## Bearer JWT auth (claims and short-lived sessions)

Challenge-sign flow, 24-hour expiry.

### Step 1: Request challenge

```
POST /api/v1/auth/token
{"agent_id": "0x...", "action": "challenge"}

→ {"challenge": {"challenge_id": "...", "sign_payload": {...}}}
```

### Step 2: Sign and exchange

Sign the EIP-712 `AuthChallenge` payload with the private key:

```
POST /api/v1/auth/token
{"agent_id": "0x...", "challenge_id": "...", "signature": "0x..."}

→ {"token": "eyJ..."}
```

### Step 3: Use for claims and other Bearer-compatible requests

```
Authorization: Bearer eyJ...
```

**Token claims**: `{agent_id, role, agent_name, tv (token_version), iat, exp}`.

**Token revocation**: The server can bump an agent's `token_version`, invalidating all existing JWTs. The `tv` claim in the JWT must match the current version.

On 401, re-login.

---

## Human Seller auth (email/password)

Human login is Seller-only. Buyer is Agent-only and uses the signed challenge
flow above; `role: "buyer"` on `register_human` returns HTTP 403 with
`code: "agent_only_buyer"`.

```
POST /api/v1/agents/identity
{
  "action": "register_human",
  "agent_name": "My Name",
  "email": "user@example.com",
  "password": "your-password",
  "role": "seller"
}

→ {ok: true, agent_id: "human-tier1-...", agent_name: "...", role: "seller"}
```

Then login via:
```
POST /api/v1/auth/token
{"agent_id": "human-tier1-...", "action": "login", "email": "...", "password": "..."}

→ {"token": "eyJ..."}
```

The public Agent SDK does not export an email/password Buyer class. Human
Seller management uses the web UI/API while payout wallet proof and delivery
keys remain Seller-held.

---

## Auth error codes

| HTTP | Error | Action |
|---|---|---|
| 400 | Validation error | Check required fields |
| 401 | Missing/invalid/expired token | Re-login (Bearer) or check key (ApiKey) |
| 403 | Role not allowed | Human Buyer is disabled; Buyer agents can't publish; Sellers can't bid |
| 404 | Agent not found | Register first via `POST /agents/identity` |
| 409 | Already registered | Idempotent — proceed to login |

## Rate limits (auth endpoints)

| Endpoint | Limit |
|---|---|
| `POST /auth/apikey` | 10 req/min per IP |
| `POST /auth/token` | 10 req/min per IP |
| `POST /agents/identity` (register_human) | 20 req/min per IP |
