using System;
using System.Linq;
using VBridge.Core;
using Xunit;

namespace VBridge.Tests;

public class FollowTests
{
    private const string Sec = "NQZ6@CME";
    private DateTime _now = new(2026, 11, 2, 11, 14, 27, DateTimeKind.Utc);

    // Orders exactly as ATAS reported them in the 2026-11-02 spike (long 1 @ 28775.50).
    private static OrderSnap Stop(string id, decimal trigger, int side = -1, bool active = true, string comment = "SL", string sec = Sec)
        => new(id, "Stop", side, 0m, trigger, comment, active, sec);
    private static OrderSnap Target(string id, decimal price, int side = -1, bool active = true, string comment = "TP", string sec = Sec)
        => new(id, "Limit", side, price, 0m, comment, active, sec);

    private (BracketTracker t, FollowGate g) Long(decimal entry = 28775.50m)
    {
        var t = new BracketTracker(Sec);
        var g = new FollowGate(() => _now);
        t.SetPositionSide(+1);
        g.OnTransition(+1, entry);
        return (t, g);
    }

    private void Advance(double ms) => _now = _now.AddMilliseconds(ms);

    [Fact]
    public void StopArrivesInWindow_OpenWithOffsets()
    {
        var (t, g) = Long();
        Assert.Empty(g.Poll());                                  // no bracket yet: wait
        Advance(160);
        t.Update(Stop("32", 28755.50m));
        Advance(150);
        t.Update(Target("33", 28788.00m));
        g.OnBracket(t.StopPrice, t.TargetPrice);
        var a = Assert.Single(g.Poll());
        var open = Assert.IsType<FollowAction.Open>(a);
        Assert.Equal("buy", open.Dir);
        Assert.Equal(20.00m, open.Offsets.SlOffset);
        Assert.Equal(12.50m, open.Offsets.TpOffset);
    }

    [Fact]
    public void NoStopInWindow_GivesUp()
    {
        var (_, g) = Long();
        Advance(1499);
        Assert.Empty(g.Poll());
        Advance(2);
        var give = Assert.IsType<FollowAction.GiveUp>(Assert.Single(g.Poll()));
        Assert.Contains("no stop", give.Reason);
        Advance(5000);
        Assert.Empty(g.Poll());                                  // gives up once, then silent until flat
    }

    [Fact]
    public void TargetOptional()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m));
        g.OnBracket(t.StopPrice, t.TargetPrice);
        var open = Assert.IsType<FollowAction.Open>(Assert.Single(g.Poll()));
        Assert.Null(open.Offsets.TpOffset);
    }

    [Fact]
    public void StopMove_DebouncedSingleModify()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); t.Update(Target("33", 28788.00m));
        g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        foreach (var p in new[] { 28760m, 28765m, 28770m })      // a drag: three updates within 250 ms
        {
            Advance(50);
            t.Update(Stop("32", p));
            g.OnBracket(t.StopPrice, t.TargetPrice);
            Assert.Empty(g.Poll());
        }
        Advance(260);
        var m = Assert.IsType<FollowAction.Modify>(Assert.Single(g.Poll()));
        Assert.Equal(5.50m, m.Offsets.SlOffset);
        Assert.Equal(12.50m, m.Offsets.TpOffset);
        Advance(500);
        Assert.Empty(g.Poll());
    }

    [Fact]
    public void BreakevenOffsetZero()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        t.Update(Stop("32", 28775.50m)); g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(300);
        Assert.Equal(0m, Assert.IsType<FollowAction.Modify>(Assert.Single(g.Poll())).Offsets.SlOffset);
    }

    [Fact]
    public void ShortOffsetsSigned()
    {
        var t = new BracketTracker(Sec);
        var g = new FollowGate(() => _now);
        t.SetPositionSide(-1);
        g.OnTransition(-1, 28775.50m);
        t.Update(Stop("40", 28790.50m, side: +1));               // buy stop above a short
        t.Update(Target("41", 28745.50m, side: +1));             // buy limit below
        g.OnBracket(t.StopPrice, t.TargetPrice);
        var open = Assert.IsType<FollowAction.Open>(Assert.Single(g.Poll()));
        Assert.Equal("sell", open.Dir);
        Assert.Equal(15.00m, open.Offsets.SlOffset);
        Assert.Equal(30.00m, open.Offsets.TpOffset);
        t.Update(Stop("40", 28770.50m, side: +1));               // trailed into profit, 5 below entry on a short
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(300);
        Assert.Equal(-5.00m, Assert.IsType<FollowAction.Modify>(Assert.Single(g.Poll())).Offsets.SlOffset);
    }

    [Fact]
    public void TargetCancelThenNew_IsMove()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); t.Update(Target("33", 28788.00m));
        g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        t.Update(Target("33", 28788.00m, active: false));        // ATAS moves the target by cancel ...
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(300);
        Assert.Empty(g.Poll());                                  // not removed yet: within the 1 s confirm
        t.Update(Target("34", 28800.00m));                       // ... and a new order
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(300);
        var m = Assert.IsType<FollowAction.Modify>(Assert.Single(g.Poll()));
        Assert.Equal(24.50m, m.Offsets.TpOffset);                // moved, never removed
    }

    [Fact]
    public void TargetRemoved_ConfirmedAfter1s()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); t.Update(Target("33", 28788.00m));
        g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        t.Update(Target("33", 28788.00m, active: false));
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(900);
        Assert.Empty(g.Poll());
        Advance(400);
        var m = Assert.IsType<FollowAction.Modify>(Assert.Single(g.Poll()));
        Assert.Null(m.Offsets.TpOffset);
        Assert.Equal(20.00m, m.Offsets.SlOffset);
    }

    [Fact]
    public void StopRemoved_KeepsLastStop()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); t.Update(Target("33", 28788.00m));
        g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        t.Update(Stop("32", 28755.50m, active: false));
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(2000);
        Assert.Empty(g.Poll());                                  // no modify that would unprotect
        Assert.True(g.StopMissing);
        Assert.Equal(20.00m, g.Current!.SlOffset);
    }

    [Fact]
    public void FlatResetsGate()
    {
        var (t, g) = Long();
        t.Update(Stop("32", 28755.50m)); g.OnBracket(t.StopPrice, t.TargetPrice); g.Poll();
        g.OnTransition(0, 0m);
        t.Update(Stop("32", 28755.50m, active: false));
        g.OnBracket(t.StopPrice, t.TargetPrice);
        Advance(3000);
        Assert.Empty(g.Poll());
        Assert.Null(g.Current);
    }

    [Fact]
    public void PrimeFromExistingOrders()
    {
        var t = new BracketTracker(Sec);
        t.Prime(new[] { Stop("32", 28755.50m), Target("33", 28788.00m), Target("x", 1m, sec: "ESZ6@CME") }, +1);
        var g = new FollowGate(() => _now);
        g.Resume(+1, 28775.50m, t.StopPrice, t.TargetPrice);
        Assert.Empty(g.Poll());                                  // resumed in position: no open
        Assert.Equal(20.00m, g.Current!.SlOffset);
        Assert.Equal(12.50m, g.Current!.TpOffset);
    }

    [Fact]
    public void OtherSecurityIgnored()
    {
        var (t, _) = Long();
        t.Update(Stop("90", 5000m, sec: "ESZ6@CME"));
        Assert.Null(t.StopPrice);
    }

    [Fact]
    public void ClassifiesByTypeWhenCommentMissing()
    {
        var (t, _) = Long();
        t.Update(Stop("32", 28755.50m, comment: ""));
        t.Update(Target("33", 28788.00m, comment: ""));
        Assert.Equal(28755.50m, t.StopPrice);
        Assert.Equal(28788.00m, t.TargetPrice);
        t.Update(new OrderSnap("50", "Limit", +1, 28700m, 0m, "", true, Sec));   // same-side limit = an add, not a target
        Assert.Equal(28788.00m, t.TargetPrice);
    }
}

public class FollowEngineTests
{
    private const string Sec = "NQZ6@CME";
    private DateTime _now = new(2026, 11, 2, 12, 5, 41, DateTimeKind.Utc);
    private void Advance(double ms) => _now = _now.AddMilliseconds(ms);

    private static OrderSnap Snap(string id, string kind, int side, decimal price, decimal trigger, string comment, bool active = true)
        => new(id, kind, side, price, trigger, comment, active, Sec);

    [Fact]
    public void NewPosition_IgnoresPreviousTradesStop_WaitsForItsOwn()
    {
        // 2026-11-02 live bug: the short opened with the previous long's stop (offset 1.25) -> "Invalid stops".
        var e = new FollowEngine(Sec, () => _now);
        e.OnTransition(+1, 28749.00m);
        e.OnOrder(Snap("old-sl", "Stop", -1, 0m, 28741.50m, "SL"));
        Assert.IsType<FollowAction.Open>(Assert.Single(e.Poll()));
        e.OnTransition(0, 0m);                                   // flat; ATAS has not reported the cancel yet
        e.OnTransition(-1, 28740.25m);                           // new short
        Assert.Empty(e.Poll());                                  // must NOT open with the stale 28741.50 stop
        e.OnOrder(Snap("old-sl", "Stop", -1, 0m, 28741.50m, "SL"));   // a late update of the old order: ignored
        Assert.Empty(e.Poll());
        Advance(150);
        e.OnOrder(Snap("new-sl", "Stop", +1, 0m, 28755.75m, "SL"));
        var open = Assert.IsType<FollowAction.Open>(Assert.Single(e.Poll()));
        Assert.Equal("sell", open.Dir);
        Assert.Equal(15.50m, open.Offsets.SlOffset);
    }

    [Fact]
    public void Flip_WithoutFlat_ResetsToo()
    {
        var e = new FollowEngine(Sec, () => _now);
        e.OnTransition(+1, 28740.50m);
        e.OnOrder(Snap("a", "Stop", -1, 0m, 28725.00m, "SL"));
        e.Poll();
        e.OnTransition(-1, 28730.00m);
        Assert.Empty(e.Poll());
        e.OnOrder(Snap("b", "Stop", +1, 0m, 28740.00m, "SL"));
        Assert.Equal(10.00m, Assert.IsType<FollowAction.Open>(Assert.Single(e.Poll())).Offsets.SlOffset);
    }

    [Fact]
    public void ResumePrimesFromActiveOrders()
    {
        var e = new FollowEngine(Sec, () => _now);
        e.Resume(+1, 28775.50m, new[] { Snap("s", "Stop", -1, 0m, 28755.50m, "SL"), Snap("t", "Limit", -1, 28788m, 0m, "TP") });
        Assert.Empty(e.Poll());
        Assert.Equal(20.00m, e.Current!.SlOffset);
        Assert.Equal(12.50m, e.Current!.TpOffset);
    }
}

public class ReviewFixTests
{
    private const string Sec = "NQZ6@CME";
    private DateTime _now = new(2026, 11, 2, 13, 0, 0, DateTimeKind.Utc);
    private void Advance(double ms) => _now = _now.AddMilliseconds(ms);
    private static OrderSnap Snap(string id, string kind, int side, decimal price, decimal trigger, string comment)
        => new(id, kind, side, price, trigger, comment, true, Sec);

    [Fact]
    public void ProvisionalReseed_SyncOnly_ResumesInsteadOfWaiting()
    {
        // I2: after an ATAS restart mid-trade the position arrives as a lone sync; the bracket is already active.
        var e = new FollowEngine(Sec, () => _now);
        var active = new[] { Snap("s", "Stop", -1, 0m, 28755.50m, "SL"), Snap("t", "Limit", -1, 28788m, 0m, "TP") };
        e.OnPositionEvents(new[] { new BridgeEvent("sync", "buy") }, +1, 28775.50m, () => active);
        Advance(3000);
        Assert.Empty(e.Poll());                                   // no GiveUp, no Open
        Assert.Equal(20.00m, e.Current!.SlOffset);
        e.OnOrder(Snap("s", "Stop", -1, 0m, 28775.50m, "SL"));    // and moves are still followed
        Advance(300);
        Assert.Equal(0m, Assert.IsType<FollowAction.Modify>(Assert.Single(e.Poll())).Offsets.SlOffset);
    }

    [Fact]
    public void RealTransition_StillWaitsForNewStop()
    {
        var e = new FollowEngine(Sec, () => _now);
        e.OnPositionEvents(new[] { new BridgeEvent("open", "buy") }, +1, 28775.50m, () => Array.Empty<OrderSnap>());
        Assert.Empty(e.Poll());
        Assert.False(e.InPosition);
    }

    [Fact]
    public void Abandon_StopsModifies()
    {
        // I3: the open failed (service down or refused): stop moves must not be sent as if mirrored.
        var e = new FollowEngine(Sec, () => _now);
        e.OnTransition(+1, 28775.50m);
        e.OnOrder(Snap("s", "Stop", -1, 0m, 28755.50m, "SL"));
        Assert.IsType<FollowAction.Open>(Assert.Single(e.Poll()));
        e.Abandon();
        e.OnOrder(Snap("s", "Stop", -1, 0m, 28765.50m, "SL"));
        Advance(500);
        Assert.Empty(e.Poll());
        Assert.Null(e.Current);
    }

    [Theory]
    [InlineData("2.1.0", true)]
    [InlineData("2.1.1", true)]
    [InlineData("3.0.0", true)]
    [InlineData("2.0.0", false)]
    [InlineData("1.9.9", false)]
    [InlineData("", false)]
    [InlineData(null, false)]
    public void ServiceSupportsFollow(string? version, bool expected)
    {
        Assert.Equal(expected, FollowEngine.ServiceSupportsFollow(version));   // I4
    }

    [Fact]
    public void Registry_ReclaimWhenNewerCopyLeaves()
    {
        // I6: removing the accidental duplicate must give the original copy its chart back.
        var key = "acc-" + Guid.NewGuid().ToString("N") + "|ES";
        var old = InstanceRegistry.Claim(key);
        var newer = InstanceRegistry.Claim(key);
        Assert.False(InstanceRegistry.TryReclaim(key, ref old));          // newer copy still there
        InstanceRegistry.Release(key, newer);
        Assert.True(InstanceRegistry.TryReclaim(key, ref old));
        Assert.True(InstanceRegistry.IsCurrent(key, old));
    }
}
