// Cognito login for the cloud deployment: OAuth 2 authorization code + PKCE, no SDK.
// config.js sets window.HD_AUTH = {domain, clientId, redirectUri} in the cloud, or null locally
// (then every function here is a no-op and the API needs no token).
"use strict";

const HDAuth = (() => {
  const cfg = window.HD_AUTH || null;
  const KEY = "hd.auth"; // {access, refresh, exp}
  const PENDING = "hd.auth.pending"; // {verifier, state} while away at the login page

  const read = (store, k) => { try { return JSON.parse(store.getItem(k)); } catch { return null; } };
  const write = (store, k, v) => {
    try { v == null ? store.removeItem(k) : store.setItem(k, JSON.stringify(v)); } catch { /* private mode */ }
  };
  // Tokens survive closing the tab (refresh token: 30 days) so you are not asked for MFA every time.
  let tokens = cfg ? read(localStorage, KEY) : null;
  let refreshing = null;

  const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const random = (n) => b64url(crypto.getRandomValues(new Uint8Array(n)));

  async function login() {
    const verifier = random(48);
    const state = random(16);
    write(sessionStorage, PENDING, { verifier, state });
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    const q = new URLSearchParams({
      response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.redirectUri, scope: "openid",
      state, code_challenge: challenge, code_challenge_method: "S256",
    });
    location.assign(`https://${cfg.domain}/oauth2/authorize?${q}`);
    return new Promise(() => {}); // the page is leaving
  }

  async function tokenRequest(params) {
    const res = await fetch(`https://${cfg.domain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: cfg.clientId, ...params }),
    });
    if (!res.ok) throw new Error(`token request failed (${res.status})`);
    const t = await res.json();
    tokens = {
      access: t.access_token,
      refresh: t.refresh_token || (tokens && tokens.refresh), // a refresh grant returns no new refresh token
      exp: Date.now() + (t.expires_in - 60) * 1000,
    };
    write(localStorage, KEY, tokens);
  }

  // Call once at startup: finishes a login redirect if there is one. Resolves to true when there
  // is a session (or no login is needed), false when the user still has to sign in. It never
  // leaves the page by itself: the home page shows a Sign in button that calls login().
  async function ready() {
    if (!cfg) return true;
    const q = new URLSearchParams(location.search);
    if (q.has("code") || q.has("error")) {
      const pending = read(sessionStorage, PENDING);
      write(sessionStorage, PENDING, null);
      history.replaceState(null, "", location.pathname); // drop ?code=… from the address bar
      if (q.has("error")) throw new Error(q.get("error_description") || q.get("error"));
      if (!pending || pending.state !== q.get("state")) return !!tokens; // stale or forged redirect
      await tokenRequest({
        grant_type: "authorization_code", code: q.get("code"),
        redirect_uri: cfg.redirectUri, code_verifier: pending.verifier,
      });
    }
    return !!tokens;
  }

  async function header() {
    if (!cfg) return {};
    if (!tokens) return login();
    if (Date.now() > tokens.exp) {
      // Several API calls can find the token expired at once; refresh it only once.
      refreshing = refreshing || tokenRequest({ grant_type: "refresh_token", refresh_token: tokens.refresh })
        .finally(() => { refreshing = null; });
      try {
        await refreshing;
      } catch {
        return expired();
      }
    }
    return { Authorization: `Bearer ${tokens.access}` };
  }

  function expired() {
    tokens = null;
    write(localStorage, KEY, null);
    return login();
  }

  function logout() {
    write(localStorage, KEY, null);
    const q = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri });
    location.assign(`https://${cfg.domain}/logout?${q}`);
  }

  return { enabled: !!cfg, ready, login, header, expired, logout };
})();
