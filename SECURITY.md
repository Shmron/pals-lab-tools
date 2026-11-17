# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting: on the repository page choose **Security** then **Report a vulnerability**. Include what you found, where, and how to reproduce it. We aim to acknowledge reports within a few working days.

## If you find a secret

If you spot an API key, password, token or personal data anywhere in this repository or its history, report it privately in the same way. Do not post it publicly or use it.

## Scope

This repository contains source code. Live services at placealert.org and portal.placealert.org are operated separately; please report problems with them through the same private channel rather than testing them aggressively.

## Our practices

- Configuration and secrets come from environment variables, never from committed files.
- `.env` files and key files are ignored by git.
- Every push and pull request is scanned for secrets in CI.
- Participant-level data and survey microdata are never stored here.
