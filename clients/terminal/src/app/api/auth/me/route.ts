/** Who-am-I — reads the httpOnly `vexa-user-info` cookie (set at login). The login gate calls this to
 *  decide whether to show the email-entry form. No backend round-trip: presence of the auth + user-info
 *  cookies means authenticated.
 *
 *  Behind Cloudflare Access (CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD set), a request with no session but
 *  a verified `Cf-Access-Jwt-Assertion` signs in as the Access-authenticated email: the same
 *  find-or-create + token mint + cookies as the email login, so the edge sign-in is the only one. */
import { NextResponse } from "next/server";
import { cookies, headers } from "next/headers";
import { AUTH_COOKIE, USER_INFO_COOKIE, findOrCreateUserToken } from "../adminApi";
import { verifiedCfAccessEmail } from "../cfAccess";

export const dynamic = "force-dynamic";

const NO_STORE = { "Cache-Control": "no-store, no-cache, must-revalidate" } as const;

function isSecureRequest(): boolean {
  return (
    (process.env.TERMINAL_URL || "").startsWith("https://") ||
    (process.env.NEXTAUTH_URL || "").startsWith("https://") ||
    false
  );
}

export async function GET() {
  const cookieStore = await cookies();
  const token = cookieStore.get(AUTH_COOKIE)?.value;
  const info = cookieStore.get(USER_INFO_COOKIE)?.value;

  if (!token) {
    const email = await verifiedCfAccessEmail((await headers()).get("cf-access-jwt-assertion"));
    if (email) {
      const result = await findOrCreateUserToken(email);
      if (result.ok) {
        const opts = { httpOnly: true, secure: isSecureRequest(), sameSite: "lax" as const, maxAge: 60 * 60 * 24 * 30, path: "/" };
        const name = result.user.name || result.user.email.split("@")[0];
        cookieStore.set(AUTH_COOKIE, result.token, opts);
        cookieStore.set(USER_INFO_COOKIE, JSON.stringify({ email: result.user.email, name }), opts);
        return NextResponse.json(
          { authenticated: true, user: { email: result.user.email, name } },
          { headers: NO_STORE },
        );
      }
      console.warn(`[terminal-auth] Cloudflare Access sign-in for ${email} failed: ${result.error}`);
    }
    return NextResponse.json({ authenticated: false }, { status: 401, headers: NO_STORE });
  }

  let email: string | undefined;
  let name: string | undefined;
  if (info) {
    try {
      ({ email, name } = JSON.parse(info) as { email?: string; name?: string });
    } catch {
      /* malformed cookie — still authenticated by the token, just no email to show */
    }
  }

  return NextResponse.json(
    { authenticated: true, user: { email: email ?? null, name: name ?? null } },
    { headers: NO_STORE },
  );
}
