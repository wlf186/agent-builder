import { NextResponse } from 'next/server';

export async function GET() {
  const configuredPort = Number.parseInt(process.env.PHOENIX_PORT || '6006', 10);
  const port = Number.isInteger(configuredPort) && configuredPort > 0 && configuredPort <= 65535
    ? configuredPort
    : 6006;

  // Phoenix is intentionally loopback-only. Do not derive this hostname from
  // the untrusted Host header.
  return NextResponse.redirect(`http://127.0.0.1:${port}`);
}
