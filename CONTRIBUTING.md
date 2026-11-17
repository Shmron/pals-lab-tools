# Contributing

Thank you for your interest in the PALs Lab tools. Contributions that make the tools clearer, more reliable and easier to reuse are welcome.

## Before you start

- Check the existing [issues](../../issues) and the tool's own README.
- For anything large, open an issue first so we can agree on the approach.

## Never commit

- Secrets of any kind (keys, tokens, passwords, service-account files), `.env` files
- Participant-level or survey microdata, databases, rasters or other data files
- Real personal email addresses or other personal details

If you are unsure whether something is safe to share, leave it out and ask. See [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md).

## Making a change

1. Fork the repository and create a branch from `main` (for example `fix/atlas-legend`).
2. Keep changes focused: one topic per pull request.
3. Run a syntax check on Python files you touched (`python -m compileall <folder>`) and test the tool locally.
4. Clear outputs from notebooks before committing.
5. Describe what changed and why in the pull request, and note anything you could not test.

## Commit messages

Use short, imperative subjects, ideally prefixed by the area, for example:

- `atlas: fix tercile legend for provinces`
- `docs: explain Earth Engine setup`

## Code style

- Python: follow PEP 8 where practical; prefer clear names and docstrings that explain *why*.
- State assumptions and limits in documentation, especially where results are estimates.

## Conduct

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
