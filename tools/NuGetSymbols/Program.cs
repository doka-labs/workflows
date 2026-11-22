namespace NuGetSymbols;

/// <summary>
/// Writes the symbol-server probes of a package directory as JSON:
/// <c>NuGetSymbols --packages &lt;directory&gt; --output &lt;file&gt;</c>.
/// </summary>
internal static class Program
{
    private static readonly JsonSerializerOptions s_jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
    };

    internal static int Main(
        string[] args
    )
    {
        if (args is not ["--packages", var packages, "--output", var output])
        {
            Console.Error.WriteLine("Usage: NuGetSymbols --packages <directory> --output <file>");
            return 2;
        }

        try
        {
            var probes = SymbolProbeBuilder.Build(packages);
            var document = new { schemaVersion = 1, probes };
            File.WriteAllText(output, JsonSerializer.Serialize(document, s_jsonOptions) + Environment.NewLine);
            Console.WriteLine($"Verified {probes.Count} Portable PDBs.");
            return 0;
        }
        catch (Exception exception) when (exception is InvalidDataException or IOException or BadImageFormatException)
        {
            Console.Error.WriteLine(exception.Message);
            return 1;
        }
    }
}
