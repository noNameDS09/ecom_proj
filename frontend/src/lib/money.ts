/** Money helpers. Amounts travel as strings ("19.90"); display uses Intl. */

export function toCents(amount: string): number {
  const value = Number(amount);
  return Number.isFinite(value) ? Math.round(value * 100) : 0;
}

export function centsToAmount(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function formatMoney(amount: string, currency: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${amount} ${currency}`;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value);
  } catch {
    // Unknown currency code: fall back to the raw amount.
    return `${amount} ${currency}`;
  }
}
