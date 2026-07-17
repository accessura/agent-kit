// Accessura x402-direct quickstart — canonical reference example.
// Safe default: public discovery and self-custodied authentication only.
// ACCESSURA_EXECUTE_DIRECT_TRADE=1 permits a signed bid (still no money move).
// ACCESSURA_CONFIRM_REAL_PAYMENT=1 is additionally required before signing the
// irreversible x402 USDC payment to the seller. Never set it implicitly.

import { bytesToHex, hexToBytes } from "viem";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { secp256k1 } from "@noble/curves/secp256k1";
import { canonicalize } from "../../src/lib/crypto/canonical.ts";
import { buyerDecrypt } from "../../src/lib/crypto/ecies.ts";

const BASE = process.env.ACCESSURA_API_BASE ?? "https://worldcup-direct-testnet.accessuraportal.com/api/v1";
const SEED_PACK_ID = process.env.ACCESSURA_PACK_ID ?? "wc-2026-player-status";
const SIGNAL_ID_OVERRIDE = process.env.ACCESSURA_SIGNAL_ID;
const PRICE = Number(process.env.ACCESSURA_BID_PRICE ?? 2.1);
const RUN_ID = Math.random().toString(36).slice(2, 10);
const EXECUTE = process.env.ACCESSURA_EXECUTE_DIRECT_TRADE === "1";
const CONFIRM_REAL_PAYMENT = process.env.ACCESSURA_CONFIRM_REAL_PAYMENT === "1";

const DOMAIN = {
  name: "WorldcupProtocol",
  version: "1",
  chainId: 8453,
  verifyingContract: "0x0000000000000000000000000000000000000000",
};

const BID_TYPES = {
  BidAuthorization: [
    { name: "bid_id", type: "string" },
    { name: "pack_id", type: "string" },
    { name: "signal_id", type: "string" },
    { name: "signal_scope", type: "string" },
    { name: "price", type: "string" },
    { name: "buyer_payment_address", type: "address" },
    { name: "buyer_signing_key", type: "address" },
    { name: "buyer_encryption_pubkey", type: "string" },
    { name: "delegation_id", type: "string" },
    { name: "window_id", type: "string" },
    { name: "nonce", type: "string" },
    { name: "expiry", type: "string" },
  ],
};

const IDENTITY_TYPES = {
  IdentityRegistration: [
    { name: "agent_id", type: "string" },
    { name: "payment_address", type: "string" },
    { name: "encryption_pubkey", type: "string" },
  ],
};

const TRANSFER_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
};

const SUPPORTED_USDC = {
  "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
};

async function api(path, options = {}) {
  const target = /^https?:\/\//.test(path) ? path : BASE + path;
  const headers = { ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["content-type"] = "application/json";
  if (options.token) headers.authorization = "Bearer " + options.token;
  const response = await fetch(target, {
    method: options.method ?? "GET",
    headers,
    credentials: options.credentials ?? "omit",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const json = await response.json().catch(() => ({}));
  if (!response.ok && options.throwOnError !== false) {
    throw new Error(`${options.method ?? "GET"} ${target} -> ${response.status}: ${JSON.stringify(json)}`);
  }
  return { status: response.status, headers: response.headers, json };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const uncompressedSecp256k1Pubkey = (privateKey) =>
  bytesToHex(secp256k1.getPublicKey(hexToBytes(privateKey), false));

function makePaymentPayload(account, paymentRequired) {
  const accepted = paymentRequired.accepts?.[0];
  const resource = paymentRequired.resource;
  if (!accepted || !resource || accepted.scheme !== "exact") {
    throw new Error("PAYMENT-REQUIRED did not contain an exact x402 offer");
  }
  const expectedAsset = SUPPORTED_USDC[accepted.network];
  if (!expectedAsset || expectedAsset.toLowerCase() !== String(accepted.asset).toLowerCase()) {
    throw new Error("payment requirement is not pinned Base/Base-Sepolia USDC");
  }
  if (!/^\d+$/.test(String(accepted.amount)) || BigInt(accepted.amount) <= 0n) {
    throw new Error("x402 amount must be positive USDC base units");
  }
  const chainId = Number(String(accepted.network).split(":")[1]);
  const validBefore = BigInt(
    Math.floor(Date.now() / 1000) + Math.max(1, Math.min(Number(accepted.maxTimeoutSeconds ?? 60), 55)),
  );
  const authorization = {
    from: account.address,
    to: accepted.payTo,
    value: String(accepted.amount),
    validAfter: "0",
    validBefore: String(validBefore),
    nonce: generatePrivateKey(),
  };
  return {
    accepted,
    resource,
    authorization,
    typedData: {
      domain: {
        name: accepted.extra?.name ?? "USDC",
        version: accepted.extra?.version ?? "2",
        chainId,
        verifyingContract: accepted.asset,
      },
      types: TRANSFER_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        ...authorization,
        value: BigInt(authorization.value),
        validAfter: 0n,
        validBefore,
      },
    },
  };
}

// 1. Keep payment/signing authority and encryption material local.
const buyerPrivateKey = generatePrivateKey();
const encryptionPrivateKey = generatePrivateKey();
const buyer = privateKeyToAccount(buyerPrivateKey);
const encryptionPubkey = uncompressedSecp256k1Pubkey(encryptionPrivateKey);
const agentId = buyer.address;

// 2. Topic-first public discovery and Pack/Signal inspection.
const topics = await api("/worldcup/topics?limit=1");
const topicSlug = process.env.ACCESSURA_TOPIC_SLUG ?? topics.json.topics?.[0]?.slug;
if (!topicSlug) throw new Error("no concrete topic slug returned");
const packs = await api("/packs?topic_slug=" + encodeURIComponent(topicSlug));
const selectedPack = packs.json.packs?.find((pack) => pack.id === SEED_PACK_ID) ?? packs.json.packs?.[0];
if (!selectedPack) throw new Error("no Pack returned for the selected topic");
const packId = selectedPack.id;
const packDetailResponse = (await api("/packs/" + encodeURIComponent(packId))).json;
const detailedPack = packDetailResponse.pack ?? packDetailResponse;
const signals = [...(selectedPack.signals ?? []), ...(detailedPack.signals ?? [])];
const signalIds = signals.map((signal) => signal?.id).filter(Boolean);
const signalId = SIGNAL_ID_OVERRIDE ?? signalIds[0];
if (!signalId || (SIGNAL_ID_OVERRIDE && signalIds.length && !signalIds.includes(SIGNAL_ID_OVERRIDE))) {
  throw new Error("selected Pack did not expose the requested signal id");
}

// 3. Register the self-custodied identity and authenticate.
const identityMessage = {
  agent_id: agentId,
  payment_address: buyer.address,
  encryption_pubkey: encryptionPubkey,
};
const identitySignature = await buyer.signTypedData({
  domain: DOMAIN,
  types: IDENTITY_TYPES,
  primaryType: "IdentityRegistration",
  message: identityMessage,
});
await api("/agents/identity", {
  method: "POST",
  body: {
    action: "register_identity",
    agent_id: agentId,
    agent_name: "Reference Buyer Agent",
    role: "buyer",
    payment_address: buyer.address,
    signing_key: buyer.address,
    encryption_pubkey: encryptionPubkey,
    signature: identitySignature,
  },
});
const challenge = (await api("/auth/token", {
  method: "POST",
  body: { agent_id: agentId, action: "challenge" },
})).json.challenge;
const authSignature = await buyer.signTypedData(challenge.sign_payload);
const token = (await api("/auth/token", {
  method: "POST",
  body: { agent_id: agentId, challenge_id: challenge.challenge_id, signature: authSignature },
})).json.token;

console.log("inspect complete", { topic_slug: topicSlug, pack_id: packId, signal_id: signalId, execute: EXECUTE });
if (!EXECUTE) process.exit(0);

// 4. Read the authenticated current round and sign one bid. Bidding moves no funds.
const bidStatus = (await api(
  "/packs/" + encodeURIComponent(packId) + "/bid?signal_id=" + encodeURIComponent(signalId),
  { token },
)).json;
const round = bidStatus.round ?? bidStatus.window;
const roundId = round?.round_id ?? round?.window_id;
if (!roundId) throw new Error("current round id was not returned by bid status");
const bidId = "bid-" + RUN_ID;
const signalScope = { mode: "single_signal", signal_id: signalId };
const rawAuthorization = {
  bid_id: bidId,
  pack_id: packId,
  signal_id: signalId,
  signal_scope: signalScope,
  price: PRICE,
  buyer_payment_address: buyer.address,
  buyer_signing_key: buyer.address,
  buyer_encryption_pubkey: encryptionPubkey,
  delegation_id: "",
  window_id: roundId,
  nonce: "bid-nonce-" + RUN_ID,
  expiry: round.closes_at,
};
const bidSignature = await buyer.signTypedData({
  domain: DOMAIN,
  types: BID_TYPES,
  primaryType: "BidAuthorization",
  message: {
    ...rawAuthorization,
    signal_scope: canonicalize(signalScope),
    price: String(PRICE),
  },
});
await api("/packs/" + encodeURIComponent(packId) + "/bid", {
  method: "POST",
  token,
  body: {
    bid_price: PRICE,
    signal_id: signalId,
    authorization: { ...rawAuthorization, signature: bidSignature },
  },
});

// 5. Clearing creates an unpaid award. Poll the claim; never poll a HOLD/balance.
let claim = null;
for (let attempt = 0; attempt < 25; attempt += 1) {
  await api("/packs/" + encodeURIComponent(packId) + "/settle", {
    method: "POST",
    token,
    body: { signal_id: signalId },
  });
  const claims = (await api("/claims", { token })).json.claims ?? [];
  claim = claims.find((item) => item.bid_id === bidId) ?? null;
  if (claim) break;
  await sleep(1200);
}
if (!claim) throw new Error("no direct award was created for " + bidId);

// 6. Payment is unavailable until the Seller's wrapped envelope is durable.
let requirementResponse = null;
for (let attempt = 0; attempt < 50; attempt += 1) {
  const response = await api("/claims/" + encodeURIComponent(claim.claim_id) + "/pay", {
    token,
    throwOnError: false,
  });
  if (response.status === 402 || response.status === 200) {
    requirementResponse = response;
    break;
  }
  if (response.status !== 202) throw new Error("claim payment state failed: " + response.status);
  await sleep(1200);
}
if (!requirementResponse) throw new Error("seller delivery was not ready before the polling deadline");
if (requirementResponse.status === 200) {
  console.log("claim was already paid", { claim_id: claim.claim_id });
} else {
  const offer = requirementResponse.json.accepts?.[0];
  console.log("payment review required", {
    claim_id: claim.claim_id,
    network: offer?.network,
    asset: offer?.asset,
    amount_base_units: offer?.amount,
    pay_to_seller: offer?.payTo,
  });
  if (!CONFIRM_REAL_PAYMENT) {
    console.log("payment not authorized; set ACCESSURA_CONFIRM_REAL_PAYMENT=1 only after reviewing the offer");
    process.exit(0);
  }

  // 7. This is the only money-moving step: sign EIP-3009 locally and pay the Seller.
  const payment = makePaymentPayload(buyer, requirementResponse.json);
  const paymentSignature = await buyer.signTypedData(payment.typedData);
  const paymentPayload = {
    x402Version: 2,
    resource: payment.resource,
    accepted: payment.accepted,
    payload: {
      signature: paymentSignature,
      authorization: payment.authorization,
    },
  };
  requirementResponse = await api("/claims/" + encodeURIComponent(claim.claim_id) + "/pay", {
    method: "POST",
    token,
    headers: {
      "PAYMENT-SIGNATURE": Buffer.from(JSON.stringify(paymentPayload), "utf8").toString("base64"),
    },
    body: {},
  });
}

// 8. Retrieve paid ciphertext without sending Accessura credentials cross-origin.
const delivery = requirementResponse.json;
if (delivery.state !== "paid_delivered" || !delivery.ciphertext_url || !delivery.platform_broker) {
  throw new Error("paid response did not contain the durable encrypted delivery");
}
const ciphertextTarget = new URL(delivery.ciphertext_url, BASE);
const accessuraOrigin = new URL(BASE).origin;
const ciphertextResponse = await api(ciphertextTarget.toString(), {
  token: ciphertextTarget.origin === accessuraOrigin ? token : undefined,
});
const ciphertextB64 = ciphertextResponse.json.ciphertext_b64;
if (typeof ciphertextB64 !== "string") throw new Error("ciphertext response was malformed");
const plaintext = buyerDecrypt(delivery.platform_broker, ciphertextB64, encryptionPrivateKey);
console.log("paid delivery decrypted", {
  claim_id: claim.claim_id,
  payment_tx_hash: delivery.payment_tx_hash,
  plaintext_bytes: plaintext.byteLength,
});
