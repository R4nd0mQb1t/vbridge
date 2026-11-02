using System;
using VBridge.Core;
using Xunit;

namespace VBridge.Tests;

public class TransitionsTests
{
    private static Transitions Armed(decimal seed = 0m)
    {
        var t = new Transitions();
        t.Arm(seed);
        return t;
    }

    [Fact]
    public void NotArmed_IgnoresEverything()
    {
        var t = new Transitions();
        Assert.False(t.Armed);
        Assert.Empty(t.OnPosition(1m));
        Assert.Empty(t.OnPosition(0m));
        Assert.Empty(t.OnPosition(-3m));
    }

    [Fact]
    public void Arm_SeedsLastAndEmitsSyncOnly()
    {
        var t = new Transitions();
        var events = t.Arm(-2m);
        Assert.True(t.Armed);
        Assert.Equal(Dir.Short, t.Last);
        var ev = Assert.Single(events);
        Assert.Equal(new BridgeEvent("sync", "sell"), ev);
    }

    [Fact]
    public void Arm_Flat_SyncFlat()
    {
        var t = new Transitions();
        Assert.Equal(new BridgeEvent("sync", "flat"), Assert.Single(t.Arm(0m)));
    }

    [Fact]
    public void FlatToLong_OpenBuy()
    {
        var t = Armed();
        Assert.Equal(new[] { new BridgeEvent("open", "buy") }, t.OnPosition(1m));
        Assert.Equal(Dir.Long, t.Last);
    }

    [Fact]
    public void FlatToShort_OpenSell()
    {
        Assert.Equal(new[] { new BridgeEvent("open", "sell") }, Armed().OnPosition(-1m));
    }

    [Fact]
    public void LongToFlat_Close()
    {
        Assert.Equal(new[] { new BridgeEvent("close", null) }, Armed(2m).OnPosition(0m));
    }

    [Fact]
    public void ShortToFlat_Close()
    {
        Assert.Equal(new[] { new BridgeEvent("close", null) }, Armed(-1m).OnPosition(0m));
    }

    [Fact]
    public void LongToShort_CloseThenOpenSell()
    {
        var t = Armed(1m);
        Assert.Equal(new[] { new BridgeEvent("close", null), new BridgeEvent("open", "sell") }, t.OnPosition(-1m));
        Assert.Equal(Dir.Short, t.Last);
    }

    [Fact]
    public void ShortToLong_CloseThenOpenBuy()
    {
        Assert.Equal(new[] { new BridgeEvent("close", null), new BridgeEvent("open", "buy") }, Armed(-2m).OnPosition(3m));
    }

    [Fact]
    public void LongToBiggerLong_Nothing()
    {
        Assert.Empty(Armed(1m).OnPosition(2m));
    }

    [Fact]
    public void LongToSmallerLong_Nothing()
    {
        Assert.Empty(Armed(3m).OnPosition(1m));
    }

    [Fact]
    public void Sync_ReportsCurrentDirection()
    {
        Assert.Equal(new BridgeEvent("sync", "buy"), Armed(1m).Sync());
        Assert.Equal(new BridgeEvent("sync", "flat"), Armed(0m).Sync());
    }

    // --- provisional arming: Position was null at the first real-time tick ---

    [Fact]
    public void ArmProvisional_FirstPositionWithinWindow_ReseedsAndEmitsSync()
    {
        var now = new DateTime(2026, 10, 29, 9, 0, 0, DateTimeKind.Utc);
        var t = new Transitions(() => now);
        Assert.Equal(new BridgeEvent("sync", "flat"), Assert.Single(t.ArmProvisional()));
        Assert.True(t.Armed && t.Provisional);
        now = now.AddSeconds(1);
        var events = t.OnPosition(1m);                       // late position snapshot, not a click
        Assert.Equal(new[] { new BridgeEvent("sync", "buy") }, events);
        Assert.Equal(Dir.Long, t.Last);
        Assert.False(t.Provisional);
    }

    [Fact]
    public void ArmProvisional_PositionAfterWindow_IsATransition()
    {
        var now = new DateTime(2026, 10, 29, 9, 0, 0, DateTimeKind.Utc);
        var t = new Transitions(() => now);
        t.ArmProvisional();
        now = now.AddSeconds(Transitions.SeedWindowSeconds + 1);
        Assert.Equal(new[] { new BridgeEvent("open", "buy") }, t.OnPosition(1m));
        Assert.False(t.Provisional);
    }

    [Fact]
    public void ArmProvisional_FlatSnapshotWithinWindow_StaysFlatNoEvent()
    {
        var now = new DateTime(2026, 10, 29, 9, 0, 0, DateTimeKind.Utc);
        var t = new Transitions(() => now);
        t.ArmProvisional();
        Assert.Empty(t.OnPosition(0m));
        Assert.False(t.Provisional);
        Assert.Equal(new[] { new BridgeEvent("open", "sell") }, t.OnPosition(-1m));
    }

    [Fact]
    public void Arm_WithVolume_IsNotProvisional()
    {
        var t = Armed(1m);
        Assert.False(t.Provisional);
    }

    [Theory]
    [InlineData(0, Dir.Flat)]
    [InlineData(1, Dir.Long)]
    [InlineData(-1, Dir.Short)]
    [InlineData(0.5, Dir.Long)]
    public void DirOf_SignOfVolume(double volume, Dir expected)
    {
        Assert.Equal(expected, Transitions.DirOf((decimal)volume));
    }
}
