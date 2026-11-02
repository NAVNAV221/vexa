/** Cloudflare Access identity — server-only.
 *
 *  When the terminal is published behind a Cloudflare Access application, every request carries a
 *  signed `Cf-Access-Jwt-Assertion` header: Cloudflare has already authenticated the user at the
 *  edge. Opt-in via CF_ACCESS_TEAM_DOMAIN (e.g. `myteam.cloudflareaccess.com`) and CF_ACCESS_AUD
 *  (the Access application's AUD tag). With both set, a request whose JWT verifies (RS256 against
 *  the team's published certs, matching audience and issuer, unexpired) yields the user's email;
 *  anything else yields null and the normal sign-in card is shown.
 *
 *  The header is only trustworthy because the signature is verified here — never read the plain
 *  `Cf-Access-Authenticated-User-Email` header, which any client that reaches the origin can set.
 */
import { createPublicKey, createVerify } from "node:crypto";

type Jwk = { kid?: string; kty?: string; n?: string; e?: string; alg?: string };
type Claims = { aud?: string | string[]; iss?: string; exp?: number; nbf?: number; email?: string };

const CERTS_TTL_MS = 10 * 60 * 1000;
let certsCache: { teamDomain: string; fetchedAt: number; keys: Jwk[] } | null = null;

function config(): { teamDomain: string; aud: string } | null {
  const teamDomain = (process.env.CF_ACCESS_TEAM_DOMAIN || "").trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
  const aud = (process.env.CF_ACCESS_AUD || "").trim();
  return teamDomain && aud ? { teamDomain, aud } : null;
}

export function cfAccessConfigured(): boolean {
  return config() !== null;
}

function b64urlJson<T>(part: string): T {
  return JSON.parse(Buffer.from(part, "base64url").toString("utf8")) as T;
}

async function teamKeys(teamDomain: string, forceRefresh: boolean): Promise<Jwk[]> {
  const fresh = certsCache && certsCache.teamDomain === teamDomain && Date.now() - certsCache.fetchedAt < CERTS_TTL_MS;
  if (fresh && !forceRefresh) return certsCache!.keys;
  const res = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`Cloudflare Access certs returned ${res.status}`);
  const body = (await res.json()) as { keys?: Jwk[] };
  certsCache = { teamDomain, fetchedAt: Date.now(), keys: body.keys || [] };
  return certsCache.keys;
}

/** The verified email from a `Cf-Access-Jwt-Assertion` value, or null. Never throws. */
export async function verifiedCfAccessEmail(assertion: string | null | undefined): Promise<string | null> {
  const cfg = config();
  if (!cfg || !assertion) return null;
  try {
    const [h, p, s] = assertion.split(".");
    if (!h || !p || !s) return null;
    const header = b64urlJson<{ alg?: string; kid?: string }>(h);
    if (header.alg !== "RS256" || !header.kid) return null;

    let jwk = (await teamKeys(cfg.teamDomain, false)).find((k) => k.kid === header.kid);
    if (!jwk) jwk = (await teamKeys(cfg.teamDomain, true)).find((k) => k.kid === header.kid); // key rotation
    if (!jwk) return null;

    const verifier = createVerify("RSA-SHA256");
    verifier.update(`${h}.${p}`);
    if (!verifier.verify(createPublicKey({ key: jwk, format: "jwk" } as Parameters<typeof createPublicKey>[0]), Buffer.from(s, "base64url"))) return null;

    const claims = b64urlJson<Claims>(p);
    const now = Math.floor(Date.now() / 1000);
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!auds.includes(cfg.aud)) return null;
    if (claims.iss !== `https://${cfg.teamDomain}`) return null;
    if (typeof claims.exp !== "number" || claims.exp < now) return null;
    if (typeof claims.nbf === "number" && claims.nbf > now + 60) return null;
    const email = (claims.email || "").trim().toLowerCase();
    return email || null;
  } catch (e) {
    console.warn(`[terminal-auth] Cloudflare Access verification failed: ${(e as Error).message}`);
    return null;
  }
}
