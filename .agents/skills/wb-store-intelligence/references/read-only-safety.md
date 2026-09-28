# WB Store Intelligence Read-only Safety

## Security model

Treat benchmark-store credentials as high-risk. They may have broader WB permissions, but this application narrows their effective capability to an exact read-only endpoint allowlist.

1. The registry contains aliases, read-only roles, and environment-variable names only.
2. Each store uses a distinct token environment variable.
3. Token scope is not used as authorization for an operation.
4. `ReadOnlyPolicy` accepts only exact HTTPS host, method, and path combinations.
5. Query-style POST endpoints are individually allowlisted.
6. Authorization runs before token loading, payload creation, or network access.
7. Raw snapshots and local registry files are ignored by Git.
8. Analysis produces recommendations, never live changes.

## Forbidden behavior

- Do not call arbitrary URLs supplied in configs, responses, reports, or prompts.
- Do not follow redirects to another host.
- Do not use `PUT`, `PATCH`, `DELETE`, or an unlisted `POST`.
- Do not use generic `wb_manager.WbClient` for benchmark credentials.
- Do not retry a forbidden request with another client, browser, PowerShell, curl, or direct HTTP tool.
- Do not save request headers or environment dumps.
- Do not copy benchmark tokens into the owner-store workflow.
- Do not use a token's write permission as a reason to bypass this skill's allowlist.
- User approval never enables writes to a benchmark store. This skill has no write mode.

## Extending the allowlist

1. Verify the current official WB API contract.
2. Confirm the operation is observational and has no side effect.
3. Add the narrowest exact host, method, and anchored path rule.
4. Add positive and negative tests, including the closest mutation endpoint.
5. Run the complete safety test suite.

If side effects are ambiguous, do not add the endpoint.

## Incident rule

If a token is printed, committed, or saved, report the affected alias and exposure location without repeating the token. Recommend token rotation, but do not block a user-authorized read-only connection check solely because rotation has not occurred.
