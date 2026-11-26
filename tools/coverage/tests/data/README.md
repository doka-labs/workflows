# Measured Cobertura reports

Each report was written by the coverage tool named below on .NET SDK 10.0.404
for the same probe library, `Probe.Lib`, with a branching method, a nested
class and an async method. Paths are replaced by `/work`; nothing else is
changed.

| File | Producer | Command | Used by |
| --- | --- | --- | --- |
| `coverlet-collector.cobertura.xml` | coverlet.collector 10.1.1 with xunit 2.9.3 on VSTest | `dotnet test --collect:"XPlat Code Coverage"` | MySql 10.x, Graph |
| `coverlet-mtp.cobertura.xml` | coverlet.MTP 10.1.1 with xunit.v3 4.0.3 on the Microsoft Testing Platform | `dotnet test --coverlet --coverlet-output-format cobertura` | Tabular, MySql 11.x |
| `microsoft-codecoverage.cobertura.xml` | Microsoft.CodeCoverage 18.12.0 with xunit 2.9.3 on VSTest | `dotnet test --collect "Code Coverage;Format=cobertura"` | SafeMigrations, NestedSet |

coverlet.MTP named its file `coverage.cobertura.261126103325198.xml`, and with
`--coverlet-file-prefix integration-mariadb` it named it
`integration-mariadb.coverage.cobertura.261126132205055.xml`; the file is stored
here as `coverlet-mtp.cobertura.xml`, and the tests read it under both measured
names.
