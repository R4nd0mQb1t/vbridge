using System;
using System.IO;
using VBridge.Core;
using Xunit;

namespace VBridge.Tests;

public class PairingTests
{
    [Fact]
    public void Pairing_ReadsTokenAndPort()
    {
        var dir = Path.Combine(Path.GetTempPath(), "vbridge-pairing-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "pairing.json"), "{\"token\": \"abc123\", \"port\": 5578}");
        Assert.True(Pairing.TryRead(dir, out var token, out var port));
        Assert.Equal("abc123", token);
        Assert.Equal(5578, port);
        Directory.Delete(dir, true);
    }

    [Fact]
    public void Pairing_MissingFileReturnsFalse()
    {
        var dir = Path.Combine(Path.GetTempPath(), "vbridge-pairing-" + Guid.NewGuid().ToString("N"));
        Assert.False(Pairing.TryRead(dir, out var token, out var port));
        Assert.Equal("", token);
        Assert.Equal(0, port);
    }

    [Fact]
    public void Pairing_BadJsonReturnsFalse()
    {
        var dir = Path.Combine(Path.GetTempPath(), "vbridge-pairing-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "pairing.json"), "{not json");
        Assert.False(Pairing.TryRead(dir, out _, out _));
        Directory.Delete(dir, true);
    }

    [Fact]
    public void Pairing_SearchDirs_TriesAtasRoamingFirstThenLocal()
    {
        var dirs = Pairing.SearchDirs;
        Assert.Equal(2, dirs.Length);
        Assert.EndsWith(Path.Combine("ATAS X", "VBridge"), dirs[0]);
        Assert.Equal(Pairing.DefaultDir, dirs[1]);
    }

    [Fact]
    public void Pairing_DefaultDirIsLocalAppDataVBridge()
    {
        var expected = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "VBridge");
        Assert.Equal(expected, Pairing.DefaultDir);
    }
}
