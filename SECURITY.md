# Security

## Model

SheetRender is built to render **untrusted templates over untrusted data**:

- Jinja2 executes in `SandboxedEnvironment` with autoescaping on.
- Template HTML passes through an nh3 allowlist sanitizer before reaching the
  browser.
- The Chromium context intercepts every network request and blocks all hosts
  outside `RenderConfig.allowed_egress_hosts` (default: Google Fonts only), so
  a template cannot exfiltrate row data.
- Chromium's own sandbox is left enabled — run the engine as an unprivileged
  user, not root.

Anything that escapes one of those layers is a vulnerability we want to know
about: sandbox escapes, sanitizer bypasses that reach script execution, egress
allowlist bypasses, or a template that can read another render's data.

## Reporting

Email **info@finaldynamics.com** with a proof-of-concept template/data pair.
Please don't open a public issue for suspected vulnerabilities. We'll respond
within a few days, and credit you in the changelog unless you'd rather not be
named.
