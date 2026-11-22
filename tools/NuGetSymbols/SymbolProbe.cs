namespace NuGetSymbols;

/// <summary>
/// One symbol-server request that must return the Portable PDB of a packaged assembly.
/// </summary>
/// <param name="Package">File name of the primary package.</param>
/// <param name="Assembly">Archive entry of the assembly in the primary package.</param>
/// <param name="Pdb">Archive entry of the Portable PDB in the symbol package.</param>
/// <param name="Url">Symbol-server URL of the Portable PDB.</param>
/// <param name="Checksum">Value of the SymbolChecksum request header.</param>
/// <param name="Sha256">SHA-256 of the Portable PDB bytes.</param>
internal sealed record SymbolProbe(
    string Package,
    string Assembly,
    string Pdb,
    string Url,
    string Checksum,
    string Sha256
);
