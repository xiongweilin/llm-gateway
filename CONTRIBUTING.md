# Contributing

Thanks for your interest!

## Scope
This repository is maintained primarily by @xiongweilin. Issues and pull requests are welcome.

## Workflow
1. Open an issue to discuss major changes.
2. Fork or create a branch from \main\.
3. Run local checks before submitting:
   - Follow existing style and \README\ instructions.
   - Run tests/lints if present (\make\, \uff\, \pytest\, \
pm run build\ as per repo).
4. Submit a PR targeting \main\. Fill the PR template.
5. CI must pass; maintainers will review.

## Commit Style
- Small, focused commits; short imperative subject (e.g., "Add health check").
- Do not commit secrets, \.env\ values, or PII. Use placeholders.

## Language Policy
- Public documents are paired: English canonical `*.md` and Chinese `*.zh-CN.md`. `LICENSE`, third-party content, and generated content are exempt.
- First-party full-line code comments use Chinese; inline comments, docstrings, and API documentation stay in their existing language.
- Run `python scripts/check_doc_pairs.py` before opening a PR; CI runs the same check.

## Local Validation
If \.github/scripts/validate_structure.py\ exists, run it locally.

## Code of Conduct
Participation is governed by \CODE_OF_CONDUCT.md\.

## License
By contributing, you agree your contributions are licensed under the repository's \LICENSE\.
