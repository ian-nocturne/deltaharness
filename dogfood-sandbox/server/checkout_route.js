import crypto from "node:crypto";
import { createRequire } from "node:module";

try {
  const req = createRequire(import.meta.url);
  req("server-only");
} catch {
  if (typeof window !== "undefined") {
    throw new Error("Server-only checkout_route.js cannot be imported in a browser context");
  }
}

export async function createCheckoutSession(orderId, amountCents, options = {}) {
  const stripeSecretKey = process.env.STRIPE_SECRET_KEY;
  const stripeWebhookSecret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!stripeSecretKey || !stripeWebhookSecret) {
    throw new Error("Missing server-only Stripe configuration");
  }
  if (!Number.isInteger(amountCents) || amountCents <= 0) {
    throw new Error("amountCents must be a positive integer");
  }

  const form = new URLSearchParams({
    mode: "payment",
    "payment_method_types[0]": "card",
    "line_items[0][price_data][currency]": options.currency || "usd",
    "line_items[0][price_data][unit_amount]": String(amountCents),
    "line_items[0][price_data][product_data][name]": options.productName || `Order ${orderId}`,
    "line_items[0][quantity]": "1",
    "metadata[order_id]": String(orderId),
    success_url: options.successUrl || "https://example.com/checkout/success",
    cancel_url: options.cancelUrl || "https://example.com/checkout/cancel",
  });

  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const apiUrl = options.apiUrl || "https://api.stripe.com/v1/checkout/sessions";
  const res = await fetchImpl(apiUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${stripeSecretKey}`,
      "Content-Type": "application/x-www-form-urlencoded",
      "Idempotency-Key": `checkout_${orderId}`,
    },
    body: form.toString(),
  });

  if (!res.ok) {
    const errBody = await res.text();
    throw new Error(`Stripe checkout session creation failed (${res.status}): ${errBody}`);
  }

  const session = await res.json();
  return {
    ok: true,
    orderId,
    amountCents,
    provider: "stripe",
    sessionId: session.id,
    checkoutUrl: session.url,
  };
}

export async function POST(req, options = {}) {
  try {
    const body = typeof req.json === "function" ? await req.json() : JSON.parse(await req.text());
    const orderId = String(body?.orderId || "").trim();
    const amountCents = Number(body?.amountCents || 0);
    if (!orderId || !Number.isInteger(amountCents) || amountCents <= 0) {
      return new Response(
        JSON.stringify({ ok: false, error: "Valid orderId and positive integer amountCents are required" }),
        { status: 400, headers: { "content-type": "application/json" } }
      );
    }
    const result = await createCheckoutSession(orderId, amountCents, options);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }),
      { status: 500, headers: { "content-type": "application/json" } }
    );
  }
}

// Stripe's documented default tolerance for webhook timestamps (replay protection).
export const WEBHOOK_TOLERANCE_SECONDS = 300;

export function verifyCheckoutWebhook(rawBody, stripeSignatureHeader, options = {}) {
  const stripeWebhookSecret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!stripeWebhookSecret || !stripeSignatureHeader) {
    return false;
  }
  const entries = String(stripeSignatureHeader)
    .split(",")
    .map((kv) => kv.trim())
    .map((kv) => [kv.slice(0, kv.indexOf("=")), kv.slice(kv.indexOf("=") + 1)])
    .filter(([k, v]) => k && v);
  const timestampRaw = entries.find(([k]) => k === "t")?.[1];
  const signatures = entries.filter(([k]) => k === "v1").map(([, v]) => v);
  if (!timestampRaw || !/^\d+$/.test(timestampRaw) || signatures.length === 0) return false;

  // Reject old or future-dated signatures so a captured request cannot be replayed later.
  const nowSeconds = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  const toleranceSeconds = options.toleranceSeconds ?? WEBHOOK_TOLERANCE_SECONDS;
  if (Math.abs(nowSeconds - Number(timestampRaw)) > toleranceSeconds) return false;

  const expected = Buffer.from(
    crypto.createHmac("sha256", stripeWebhookSecret).update(`${timestampRaw}.${rawBody}`, "utf8").digest("hex"),
    "hex"
  );
  return signatures.some((sig) => {
    if (!/^[0-9a-f]+$/i.test(sig)) return false;
    const candidate = Buffer.from(sig, "hex");
    return candidate.length === expected.length && crypto.timingSafeEqual(candidate, expected);
  });
}
