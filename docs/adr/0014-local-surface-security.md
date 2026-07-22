# ADR-0014 — Local-surface security

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The first-pass architecture binds the control + read API to loopback and adds no
authentication, on the reasoning that a localhost surface is safe. That reasoning
does not hold for a **browser-based** tool. A page open in another tab can POST to a
predictable `127.0.0.1:<port>` endpoint (`/cancel`, `/send_event`) with no
credentials, and a DNS-rebinding attack can bypass the same-origin policy to read
runs back. On a shared development machine, any local user can also drive the API.
The goal is a cheap, proportionate mitigation, not a full authentication system.

## Decision

`satay dev` binds the API to loopback and, in addition:

- listens on a **random (ephemeral) port** by default, so the endpoint is not
  predictable;
- generates a **per-session token** at startup, prints it, and hands it to Studio;
  every API request must present the token (via header), and requests without it are
  rejected;
- **allow-lists `Origin`/`Host`** on incoming requests, rejecting cross-origin
  requests and unexpected `Host` values, which defends against DNS rebinding.

This is **not** authentication or authorization for a networked deployment. A
network-exposed deployment still needs real auth added at the API layer, which
remains out of scope (ADR-0009). Read-time redaction of sensitive fields (N18) is
unchanged and complementary.

## Consequences

- Closes the CSRF and DNS-rebinding hole for the local debugger with a small amount
  of code and no login system.
- Studio must carry the session token; the control API rejects tokenless or
  cross-origin requests.
- Extends ADR-0009 and updates ARCHITECTURE §7.

## Refinement (H3 test audit, 2026-07-22)

- **The guard is enforced by the API server, and is tested from V5 (Q43).** The token
  check and `Origin`/`Host` allow-list are properties of the HTTP surface itself, so they
  exist and are covered by negative tests (missing/invalid token rejected, disallowed
  `Origin`/`Host` rejected, non-loopback bind refused) from **V5**, where the surface is
  born. `satay dev` in **V8** is what generates and hands over the per-session token; V8
  carries only a smoke test that a booted `satay dev` supplies a working token. This
  avoids a three-slice window (V5–V7) where the surface would otherwise run unguarded and
  untested.
