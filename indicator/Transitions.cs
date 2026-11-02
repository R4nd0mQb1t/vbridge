#nullable enable
// Pure position-transition logic for the V Bridge indicator. No ATAS or platform references:
// this file is compiled into both the indicator DLL and the xUnit test project.
using System;
using System.Collections.Generic;

namespace VBridge.Core
{
    public enum Dir { Flat = 0, Long = 1, Short = -1 }

    /// <summary>One message for the bridge service. Type: open | close | sync. Dir: buy | sell | flat | null.</summary>
    public sealed record BridgeEvent(string Type, string? Dir);

    /// <summary>
    /// Turns a stream of net-position volumes into open/close events (spec 6.1).
    /// Nothing is emitted until Arm()/ArmProvisional() has seeded the starting direction, so history
    /// replay and chart loading can never produce an open.
    ///
    /// Provisional arming covers an ATAS restart mid-trade: the first real-time tick can arrive before
    /// the position snapshot. If ATAS had no Position object at arm time we assume flat, but a position
    /// reported within SeedWindowSeconds is treated as the late snapshot (re-seed + sync), not as a click.
    /// Not thread-safe: the owner serialises calls.
    /// </summary>
    public sealed class Transitions
    {
        public const double SeedWindowSeconds = 3.0;

        private readonly Func<DateTime> _now;
        private DateTime _armedAt;

        public Transitions() : this(() => DateTime.UtcNow) { }
        public Transitions(Func<DateTime> clock) { _now = clock; }

        public bool Armed { get; private set; }
        public bool Provisional { get; private set; }
        public Dir Last { get; private set; } = Dir.Flat;

        public static Dir DirOf(decimal volume) => volume > 0 ? Dir.Long : volume < 0 ? Dir.Short : Dir.Flat;

        private static string? DirWord(Dir d) => d switch
        {
            Dir.Long => "buy",
            Dir.Short => "sell",
            _ => "flat",
        };

        /// <summary>Seed from a known position. Returns a single sync event describing it.</summary>
        public IReadOnlyList<BridgeEvent> Arm(decimal currentVolume)
        {
            Last = DirOf(currentVolume);
            Armed = true;
            Provisional = false;
            _armedAt = _now();
            return new[] { Sync() };
        }

        /// <summary>Seed as flat because no position object exists yet; the first report inside the window re-seeds.</summary>
        public IReadOnlyList<BridgeEvent> ArmProvisional()
        {
            var events = Arm(0m);
            Provisional = true;
            return events;
        }

        public BridgeEvent Sync() => new("sync", DirWord(Last));

        /// <summary>Apply a new net volume. Returns the events to send, in order (possibly none).</summary>
        public IReadOnlyList<BridgeEvent> OnPosition(decimal volume)
        {
            if (!Armed) return Array.Empty<BridgeEvent>();
            if (Provisional)
            {
                Provisional = false;
                if ((_now() - _armedAt).TotalSeconds < SeedWindowSeconds)
                {
                    var seeded = DirOf(volume);
                    if (seeded == Last) return Array.Empty<BridgeEvent>();
                    Last = seeded;
                    return new[] { Sync() };
                }
            }
            var next = DirOf(volume);
            var prev = Last;
            Last = next;
            if (next == prev) return Array.Empty<BridgeEvent>();
            var events = new List<BridgeEvent>(2);
            if (prev != Dir.Flat) events.Add(new BridgeEvent("close", null));
            if (next != Dir.Flat) events.Add(new BridgeEvent("open", DirWord(next)));
            return events;
        }
    }
}
