"use client";

export const publicSupabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;

export async function requestOrderCheckout(orderId, amountCents) {
  const response = await fetch("/api/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orderId, amountCents }),
  });
  return response.json();
}
