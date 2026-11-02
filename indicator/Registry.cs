#nullable enable
// One live V Bridge per (account, instrument root) inside the ATAS process. ATAS can keep an old
// indicator instance alive for minutes next to its replacement (seen 2026-11-02); the newest claim wins
// and older instances go to standby. The service does the same per connection for copies loaded in
// another load context.
using System.Collections.Generic;

namespace VBridge.Core
{
    public static class InstanceRegistry
    {
        private static readonly object Gate = new();
        private static readonly Dictionary<string, long> Current = new();
        private static long _next;

        public static long Claim(string key)
        {
            lock (Gate)
            {
                var gen = ++_next;
                Current[key] = gen;
                return gen;
            }
        }

        public static bool IsCurrent(string key, long generation)
        {
            lock (Gate)
                return Current.TryGetValue(key, out var g) && g == generation;
        }

        /// <summary>When the newer copy has gone (removed from the chart), the old copy takes the key back.</summary>
        public static bool TryReclaim(string key, ref long generation)
        {
            lock (Gate)
            {
                if (Current.TryGetValue(key, out var g))
                    return g == generation;
                generation = ++_next;
                Current[key] = generation;
                return true;
            }
        }

        public static void Release(string key, long generation)
        {
            lock (Gate)
                if (Current.TryGetValue(key, out var g) && g == generation)
                    Current.Remove(key);
        }
    }
}
