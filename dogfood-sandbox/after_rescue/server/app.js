// Minimal route wiring for the repaired dogfood app (B16).
// Mounts the real handlers from ./checkout_route.js on a plain Node HTTP server:
//   POST /api/checkout          -> POST()              (creates a Stripe Checkout Session)
//   POST /api/webhooks/stripe   -> verifyCheckoutWebhook (signature + 300 s replay window)
// Run: STRIPE_SECRET_KEY=... STRIPE_WEBHOOK_SECRET=... node server/app.js
// Tests call createAppServer({ fetchImpl }) with an injected Stripe transport and listen on port 0.
import http from "node:http";
import { pathToFileURL } from "node:url";
import { POST, verifyCheckoutWebhook } from "./checkout_route.js";

const MAX_BODY_BYTES = 1024 * 1024;

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("payload_too_large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

export function createAppServer(options = {}) {
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", "http://localhost");
      if (req.method !== "POST") {
        sendJson(res, 405, { ok: false, error: "method_not_allowed" });
        return;
      }
      const raw = await readBody(req);
      if (url.pathname === "/api/checkout") {
        const response = await POST(new Request("http://localhost/api/checkout", { method: "POST", body: raw }), options);
        sendJson(res, response.status, await response.json());
        return;
      }
      if (url.pathname === "/api/webhooks/stripe") {
        const valid = verifyCheckoutWebhook(raw, req.headers["stripe-signature"] || "", options);
        if (!valid) {
          sendJson(res, 400, { ok: false, error: "invalid_or_expired_signature" });
          return;
        }
        // Signature verified on the raw body before parsing (never JSON.parse first).
        const event = JSON.parse(raw);
        sendJson(res, 200, { ok: true, received: event.type || null });
        return;
      }
      sendJson(res, 404, { ok: false, error: "not_found" });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      sendJson(res, message === "payload_too_large" ? 413 : 500, { ok: false, error: message === "payload_too_large" ? message : "internal_error" });
    }
  });
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const port = Number(process.env.PORT || 3000);
  createAppServer().listen(port, "127.0.0.1", () => {
    console.log(`dogfood app listening on http://127.0.0.1:${port}`);
  });
}
