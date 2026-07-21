#!/usr/bin/env node

// No-money Seller release preflight. Plaintext, DEK, and Buyer private key stay
// in this process. The script proves the canonical helper produces an envelope
// that the intended Buyer can decrypt, including the easily-confused AAD split.

import crypto from "node:crypto";
import { register } from "node:module";
import { pathToFileURL } from "node:url";
import { bytesToHex } from "viem";

const projectRoot = process.cwd();
register("./scripts/lib/ts-alias-hooks.mjs", pathToFileURL(`${projectRoot}/`));
const { buyerDecrypt, sellerWrapPreEncryptedDek } = await import("@/lib/crypto/ecies");

function check(condition, message) {
  if (!condition) throw new Error(message);
  console.log(`PASS  ${message}`);
}

const claimId = "claim_dry_run";
const buyerAgentId = "buyer_dry_run";
const binding = `${claimId}:${buyerAgentId}`;
const buyer = crypto.createECDH("secp256k1");
buyer.generateKeys();
const buyerPrivateKey = bytesToHex(buyer.getPrivateKey());
const buyerPublicKey = bytesToHex(buyer.getPublicKey(undefined, "uncompressed"));
const dek = crypto.randomBytes(32);
const plaintext = Buffer.from(JSON.stringify({ dry_run: true, secret: "seller-local-only" }), "utf8");
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv("aes-256-gcm", dek, iv);
const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
const framed = Buffer.concat([iv, ciphertext, cipher.getAuthTag()]).toString("base64");

const envelope = sellerWrapPreEncryptedDek(bytesToHex(dek), buyerPublicKey, {
  ciphertextB64: framed,
  claimId,
  buyerAgentId,
});
check(envelope.platform_broker.aad === null, "content aad is null for pre-encrypted content");
check(envelope.platform_broker.wrap_aad === binding, "wrap_aad is bound to claim_id:buyer_id");
const opened = buyerDecrypt(envelope.platform_broker, framed, buyerPrivateKey);
check(opened.equals(plaintext), "intended Buyer opens the exact Seller-local plaintext");
check(!JSON.stringify(envelope.platform_broker).includes("seller-local-only"), "broker contains no plaintext or DEK");

const apiBase = (process.env.ACCESSURA_API_BASE ?? "https://testnet.accessura.io/api/v1").replace(/\/$/, "");
const credential = process.env.ACCESSURA_SELLER_API_KEY
  ? `ApiKey ${process.env.ACCESSURA_SELLER_API_KEY}`
  : process.env.ACCESSURA_SELLER_TOKEN ? `Bearer ${process.env.ACCESSURA_SELLER_TOKEN}` : null;
if (process.env.ACCESSURA_VERIFY_REMOTE === "1" || credential) {
  const release = await fetch(apiBase).then((response) => response.json()).catch(() => null);
  check(typeof release?.release_sha === "string", "target API exposes a release_sha field");
}

if (credential) {
  const response = await fetch(`${apiBase}/sellers/readiness`, { headers: { Authorization: credential } });
  const payload = await response.json().catch(() => ({}));
  check(response.ok, `authenticated Seller readiness endpoint responds (${response.status})`);
  check(typeof payload?.readiness?.seller_id === "string", "credential belongs to a Seller");
  console.log("Seller readiness", payload.readiness);
} else {
  console.log("SKIP  remote Seller readiness (set ACCESSURA_SELLER_API_KEY or ACCESSURA_SELLER_TOKEN)");
}

console.log("\nSeller dry-run completed without bidding, signing payment, or moving USDC.");
