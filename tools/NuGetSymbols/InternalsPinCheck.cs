namespace NuGetSymbols;

/// <summary>
/// Requires an exact version pin wherever a package depends on another package of the same candidate whose
/// assemblies grant it their internals.
/// </summary>
/// <remarks>
/// WHY: Internals carry no compatibility promise, so a package that uses another package's internals works only
/// with the exact version it was built against; NuGet treats a plain version as a minimum and would let a
/// consumer combine it with a newer one. "[1.0]" is the exact-match range.
/// https://learn.microsoft.com/nuget/concepts/package-versioning#version-ranges
/// https://learn.microsoft.com/dotnet/api/system.runtime.compilerservices.internalsvisibletoattribute
/// </remarks>
internal static class InternalsPinCheck
{
    private const string InternalsVisibleTo = "System.Runtime.CompilerServices.InternalsVisibleToAttribute";

    /// <summary>
    /// Checks every dependency between the primary packages of the directory.
    /// </summary>
    /// <param name="packageDirectory">Directory with the .nupkg files of one candidate.</param>
    /// <returns>Number of dependencies that share internals and carry the exact pin.</returns>
    /// <exception cref="InvalidDataException">A dependency that shares internals is not pinned exactly.</exception>
    internal static int Verify(
        string packageDirectory
    )
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(packageDirectory);

        var packages = Directory
            .GetFiles(packageDirectory, "*.nupkg")
            .Order(StringComparer.Ordinal)
            .Select(Read)
            .ToDictionary(package => package.Id, StringComparer.OrdinalIgnoreCase);

        var pinned = 0;
        foreach (var dependent in packages.Values)
        {
            foreach (var (id, range) in dependent.Dependencies)
            {
                if (!packages.TryGetValue(id, out var dependency)
                    || !dependency.Grants.Overlaps(dependent.Assemblies))
                {
                    continue;
                }

                if (!StringComparer.OrdinalIgnoreCase.Equals(range, $"[{dependency.Version}]"))
                {
                    throw new InvalidDataException(
                        $"{dependent.Id} depends on {dependency.Id} with the range {range}, but {dependency.Id} grants "
                        + $"it its internals; the dependency needs the exact pin [{dependency.Version}].");
                }

                pinned++;
            }
        }

        return pinned;
    }

    private static Package Read(
        string path
    )
    {
        using var archive = ZipFile.OpenRead(path);
        var nuspecs = archive
            .Entries
            .Where(entry => !entry.FullName.Contains('/')
                && entry.FullName.EndsWith(".nuspec", StringComparison.Ordinal))
            .ToArray();

        if (nuspecs.Length != 1)
        {
            throw new InvalidDataException($"{Path.GetFileName(path)} must contain exactly one .nuspec.");
        }

        XDocument nuspec;
        using (var stream = nuspecs[0].Open())
        {
            nuspec = XDocument.Load(stream);
        }

        var metadata = nuspec.Root?.Elements().SingleOrDefault(element => element.Name.LocalName == "metadata")
            ?? throw new InvalidDataException($"{Path.GetFileName(path)} has no nuspec metadata.");

        var dependencies = metadata
            .Descendants()
            .Where(element => element.Name.LocalName == "dependency")
            .Select(element => (
                Id: (string?)element.Attribute("id") ?? "",
                Range: (string?)element.Attribute("version") ?? ""))
            .ToArray();

        var assemblies = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var grants = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in archive.Entries.Where(entry => entry.FullName.StartsWith("lib/", StringComparison.Ordinal)
                     && entry.FullName.EndsWith(".dll", StringComparison.Ordinal)))
        {
            using var memory = new MemoryStream();
            using (var stream = entry.Open())
            {
                stream.CopyTo(memory);
            }

            memory.Position = 0;
            using var reader = new PEReader(memory);
            var assembly = reader.GetMetadataReader();
            assemblies.Add(assembly.GetString(assembly.GetAssemblyDefinition().Name));
            grants.UnionWith(GrantedAssemblies(assembly));
        }

        return new Package(
            Text(metadata, "id", path),
            Text(metadata, "version", path),
            dependencies,
            assemblies,
            grants);
    }

    private static IEnumerable<string> GrantedAssemblies(
        MetadataReader assembly
    )
    {
        foreach (var handle in assembly.GetAssemblyDefinition().GetCustomAttributes())
        {
            var attribute = assembly.GetCustomAttribute(handle);
            if (AttributeType(assembly, attribute) != InternalsVisibleTo)
            {
                continue;
            }

            // WHY: The argument is "Name" or "Name, PublicKey=..."; only the simple name identifies the friend.
            // https://learn.microsoft.com/dotnet/api/system.runtime.compilerservices.internalsvisibletoattribute
            var value = attribute.DecodeValue(new StringOnlyDecoder()).FixedArguments.Single().Value as string ?? "";
            yield return value.Split(',')[0].Trim();
        }
    }

    private static string AttributeType(
        MetadataReader assembly,
        CustomAttribute attribute
    )
    {
        var type = attribute.Constructor.Kind switch
        {
            HandleKind.MemberReference
                => assembly.GetMemberReference((MemberReferenceHandle)attribute.Constructor).Parent,
            HandleKind.MethodDefinition
                => assembly.GetMethodDefinition((MethodDefinitionHandle)attribute.Constructor).GetDeclaringType(),
            _ => default,
        };

        return type.Kind switch
        {
            HandleKind.TypeReference when assembly.GetTypeReference((TypeReferenceHandle)type) is var reference
                => $"{assembly.GetString(reference.Namespace)}.{assembly.GetString(reference.Name)}",
            HandleKind.TypeDefinition when assembly.GetTypeDefinition((TypeDefinitionHandle)type) is var definition
                => $"{assembly.GetString(definition.Namespace)}.{assembly.GetString(definition.Name)}",
            _ => "",
        };
    }

    private static string Text(
        XElement metadata,
        string name,
        string path
    )
    {
        var value = metadata.Elements().SingleOrDefault(element => element.Name.LocalName == name)?.Value;
        return string.IsNullOrWhiteSpace(value)
            ? throw new InvalidDataException($"{Path.GetFileName(path)} has no nuspec {name}.")
            : value.Trim();
    }

    private sealed record Package(
        string Id,
        string Version,
        IReadOnlyList<(string Id, string Range)> Dependencies,
        HashSet<string> Assemblies,
        HashSet<string> Grants
    );

    /// <summary>
    /// Decodes the single string argument of InternalsVisibleToAttribute; any other argument type is invalid data.
    /// </summary>
    private sealed class StringOnlyDecoder : ICustomAttributeTypeProvider<object?>
    {
        public object? GetPrimitiveType(
            PrimitiveTypeCode typeCode
        ) => typeCode == PrimitiveTypeCode.String
            ? null
            : throw new InvalidDataException("InternalsVisibleToAttribute takes only a string.");

        public object? GetSystemType() => throw new InvalidDataException("Unexpected type argument.");

        public object? GetSZArrayType(
            object? elementType
        ) => throw new InvalidDataException("Unexpected array argument.");

        public object? GetTypeFromDefinition(
            MetadataReader reader,
            TypeDefinitionHandle handle,
            byte rawTypeKind
        ) => throw new InvalidDataException("Unexpected type argument.");

        public object? GetTypeFromReference(
            MetadataReader reader,
            TypeReferenceHandle handle,
            byte rawTypeKind
        ) => throw new InvalidDataException("Unexpected type argument.");

        public object? GetTypeFromSerializedName(
            string name
        ) => throw new InvalidDataException("Unexpected type argument.");

        public PrimitiveTypeCode GetUnderlyingEnumType(
            object? type
        ) => throw new InvalidDataException("Unexpected enum argument.");

        public bool IsSystemType(
            object? type
        ) => false;
    }
}
