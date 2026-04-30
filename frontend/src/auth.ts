// Auth + corpus-session helpers shared between App and ImportFlow.
//
// - corpusSession: the slug the browser is currently operating on.
//                  For the legacy admin path, this is the LEGACY_SESSION secret.
// - authToken:     proof of email ownership, set after the magic-link round trip.
//                  Sent on every request alongside corpusSession.

const SESSION_KEY = "corpusSession";
const AUTH_TOKEN_KEY = "authToken";

export function getSession(): string | null {
  return localStorage.getItem(SESSION_KEY);
}

export function setSession(slug: string): void {
  localStorage.setItem(SESSION_KEY, slug);
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}

export function getAuthToken(): string | null {
  return localStorage.getItem(AUTH_TOKEN_KEY);
}

export function setAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
}

export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const slug = getSession();
  if (slug) headers["X-Corpus-Session"] = slug;
  const token = getAuthToken();
  if (token) headers["X-Auth-Token"] = token;
  return headers;
}
