# Measured TRX files

Each file was written by the test runner named below on .NET SDK 10.0.404,
from a probe test class with a passing test and skipped tests. Paths, user and
host names are replaced by `/work` and `runner`; nothing else is changed.

| File | Producer | Command | Used by |
| --- | --- | --- | --- |
| `xunit-v3-vstest.trx` | xunit.v3 4.0.3, xunit.runner.visualstudio 4.0.1, Microsoft.NET.Test.Sdk 18.12.0 | `dotnet test --logger "trx;LogFileName=vstest.trx"` | MySql 10.x, NestedSet, Graph |
| `xunit-v3-mtp.trx` | xunit.v3 4.0.3 on the Microsoft Testing Platform | `dotnet test --report-xunit-trx --report-xunit-trx-filename mtp.trx` | Tabular, MySql 11.x |
| `xunit-v2-vstest.trx` | xunit 2.9.3, xunit.runner.visualstudio 4.0.1, Microsoft.NET.Test.Sdk 18.12.0 | `dotnet test --logger trx --collect:"XPlat Code Coverage"` | SafeMigrations |
