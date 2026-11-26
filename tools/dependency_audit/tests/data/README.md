# Measured package listings

Each listing was written by `dotnet package list --vulnerable
--include-transitive --format json --output-version 1` for a solution of two
`netstandard2.0` projects, `Direct` and `Consumer`, where `Consumer`
references `Direct`. Paths are replaced by `/work`; nothing else is changed.

| File | SDK | Packages |
| --- | --- | --- |
| `package-list-clean-sdk10.0.404.json` | 10.0.404 | none |
| `package-list-clean-sdk11.0.100.json` | 11.0.100 | none |
| `package-list-vulnerable-sdk10.0.404.json` | 10.0.404 | `Direct` references System.Text.Json 8.0.0 |
| `package-list-vulnerable-sdk11.0.100.json` | 11.0.100 | `Direct` references System.Text.Json 8.0.0 |

System.Text.Json 8.0.0 is referenced only because it has known high-severity
advisories (GHSA-hh2w-p6rv-4g7w, GHSA-8g4q-xg66-9fp4), so the listing reports
it as a top-level package of `Direct` and a transitive package of `Consumer`.
