namespace NuGetSymbols;

/// <summary>
/// Derives symbol-server probes from the identity and checksum that each packaged assembly carries for its
/// Portable PDB, after proving that the symbol package holds exactly that PDB.
/// </summary>
internal static class SymbolProbeBuilder
{
    // WHY: nuget.org serves symbols from this symbol server.
    // https://learn.microsoft.com/nuget/create-packages/symbol-packages-snupkg
    private const string SymbolServer = "https://symbols.nuget.org/download/symbols";

    /// <summary>
    /// Builds one probe for every assembly under lib/ of every primary package in the directory.
    /// </summary>
    /// <param name="packageDirectory">Directory with the .nupkg and matching .snupkg files.</param>
    /// <returns>Probes in package and entry order.</returns>
    /// <exception cref="InvalidDataException">A package, assembly or Portable PDB does not match.</exception>
    internal static IReadOnlyList<SymbolProbe> Build(
        string packageDirectory
    )
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(packageDirectory);

        var primaries = Directory
            .GetFiles(packageDirectory, "*.nupkg")
            .Order(StringComparer.Ordinal)
            .ToArray();

        if (primaries.Length == 0)
        {
            throw new InvalidDataException($"{packageDirectory} contains no .nupkg file.");
        }

        var probes = new List<SymbolProbe>();
        foreach (var primary in primaries)
        {
            var symbols = Path.ChangeExtension(primary, ".snupkg");
            if (!File.Exists(symbols))
            {
                throw new InvalidDataException($"{Path.GetFileName(primary)} has no matching .snupkg.");
            }

            probes.AddRange(BuildPackage(primary, symbols));
        }

        return probes;
    }

    private static IEnumerable<SymbolProbe> BuildPackage(
        string primary,
        string symbols
    )
    {
        using var primaryArchive = ZipFile.OpenRead(primary);
        using var symbolArchive = ZipFile.OpenRead(symbols);
        var assemblies = primaryArchive
            .Entries
            .Where(entry => entry.FullName.StartsWith("lib/", StringComparison.Ordinal)
                && entry.FullName.EndsWith(".dll", StringComparison.Ordinal))
            .OrderBy(entry => entry.FullName, StringComparer.Ordinal)
            .ToArray();

        if (assemblies.Length == 0)
        {
            throw new InvalidDataException($"{Path.GetFileName(primary)} contains no assembly under lib/.");
        }

        foreach (var assembly in assemblies)
        {
            var pdbEntry = Path.ChangeExtension(assembly.FullName, ".pdb");
            var pdbMatches = symbolArchive
                .Entries
                .Where(entry => StringComparer.Ordinal.Equals(entry.FullName, pdbEntry))
                .ToArray();

            if (pdbMatches.Length != 1)
            {
                throw new InvalidDataException($"{Path.GetFileName(symbols)} must contain exactly one {pdbEntry}.");
            }

            yield return BuildProbe(Path.GetFileName(primary), assembly.FullName, Read(assembly), pdbEntry,
                Read(pdbMatches[0]));
        }
    }

    private static SymbolProbe BuildProbe(
        string package,
        string assemblyEntry,
        byte[] assembly,
        string pdbEntry,
        byte[] pdb
    )
    {
        using var peReader = new PEReader(new MemoryStream(assembly, writable: false));
        var debugEntries = peReader.ReadDebugDirectory();
        var codeViews = debugEntries
            .Where(entry => entry is { Type: DebugDirectoryEntryType.CodeView, IsPortableCodeView: true })
            .ToArray();

        var checksums = debugEntries
            .Where(entry => entry.Type == DebugDirectoryEntryType.PdbChecksum)
            .ToArray();

        if (codeViews.Length != 1 || checksums.Length != 1)
        {
            throw new InvalidDataException($"{assemblyEntry} must carry one Portable PDB identity and one checksum.");
        }

        var codeView = peReader.ReadCodeViewDebugDirectoryData(codeViews[0]);
        var checksum = peReader.ReadPdbChecksumDebugDirectoryData(checksums[0]);
        var pdbName = Path.GetFileName(codeView.Path);
        if (!StringComparer.Ordinal.Equals(pdbName, Path.GetFileName(pdbEntry)))
        {
            throw new InvalidDataException($"{assemblyEntry} names symbols '{pdbName}', not {pdbEntry}.");
        }

        if (!StringComparer.OrdinalIgnoreCase.Equals(checksum.AlgorithmName, "SHA256"))
        {
            throw new InvalidDataException($"{assemblyEntry} does not carry a SHA-256 Portable PDB checksum.");
        }

        ValidatePortablePdb(pdb, codeView.Guid, checksum.Checksum.AsSpan(), pdbEntry);

        // WHY: Portable PDB symbol-store keys use the PDB GUID followed by FFFFFFFF in place of an age.
        // https://github.com/dotnet/symstore/blob/main/docs/specs/SSQP_Key_Conventions.md
        var key = $"{codeView.Guid:N}FFFFFFFF";
        var header = "SHA256:" + Convert.ToHexStringLower(checksum.Checksum.AsSpan());
        return new SymbolProbe(package, assemblyEntry, pdbEntry, $"{SymbolServer}/{pdbName}/{key}/{pdbName}",
            header, Convert.ToHexStringLower(SHA256.HashData(pdb)));
    }

    private static void ValidatePortablePdb(
        byte[] pdb,
        Guid expectedGuid,
        ReadOnlySpan<byte> expectedChecksum,
        string pdbEntry
    )
    {
        MetadataReaderProvider provider;
        try
        {
            provider = MetadataReaderProvider.FromPortablePdbImage(pdb.ToImmutableArray());
        }
        catch (BadImageFormatException exception)
        {
            throw new InvalidDataException($"{pdbEntry} is not a Portable PDB.", exception);
        }

        using (provider)
        {
            DebugMetadataHeader? header;
            try
            {
                header = provider
                    .GetMetadataReader()
                    .DebugMetadataHeader;
            }
            catch (BadImageFormatException exception)
            {
                throw new InvalidDataException($"{pdbEntry} is not a readable Portable PDB.", exception);
            }

            if (header is null || new BlobContentId(header.Id).Guid != expectedGuid)
            {
                throw new InvalidDataException($"{pdbEntry} does not match the identity in its assembly.");
            }

            // WHY: The Portable PDB checksum covers the file with its 20-byte PDB ID zeroed.
            // https://github.com/dotnet/runtime/blob/main/docs/design/specs/PE-COFF.md#pdb-checksum-debug-directory-entry-type-19
            if (header.Id.Length != 20 || header.IdStartOffset < 0 || header.IdStartOffset > pdb.Length - 20)
            {
                throw new InvalidDataException($"{pdbEntry} has an invalid PDB ID range.");
            }

            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            hash.AppendData(pdb.AsSpan(0, header.IdStartOffset));
            hash.AppendData(new byte[20]);
            hash.AppendData(pdb.AsSpan(header.IdStartOffset + 20));
            if (!hash.GetHashAndReset().AsSpan().SequenceEqual(expectedChecksum))
            {
                throw new InvalidDataException($"{pdbEntry} does not match the checksum in its assembly.");
            }
        }
    }

    private static byte[] Read(
        ZipArchiveEntry entry
    )
    {
        using var stream = entry.Open();
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }
}
