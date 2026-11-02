#nullable enable
// Follow-ATAS logic: which ATAS orders are the stop and the target, and when to open / modify on MT5.
// Pure C#, no ATAS references: compiled into the indicator and the xUnit project.
//
// Measured 2026-11-02 (NQ demo, Rithmic): ATAS places the bracket AFTER the entry fill as two orders,
// stop +0.36 s ("SL", Stop, opposite side, TriggerPrice) and target +0.51 s ("TP", Limit, opposite side, Price).
// ITradingManager.IsStopLossOrder/IsTakeProfitOrder are false while active, so classification is by
// comment, then by type + side.
using System;
using System.Collections.Generic;

namespace VBridge.Core
{
    /// <summary>The fields of an ATAS order the tracker needs. Kind: Stop | StopLimit | Limit | Market | Other.
    /// Side: +1 buy, -1 sell.</summary>
    public sealed record OrderSnap(string Id, string Kind, int Side, decimal Price, decimal Trigger, string Comment,
                                   bool Active, string Security);

    /// <summary>Signed offsets from the ATAS entry: SlOffset = side*(entry-SL) (>0 loss side, 0 breakeven,
    /// &lt;0 in profit); TpOffset = side*(TP-entry), null = no target.</summary>
    public sealed record FollowOffsets(decimal? SlOffset, decimal? TpOffset, decimal AtasEntry)
    {
        public bool SameLevels(FollowOffsets? other) =>
            other != null && other.SlOffset == SlOffset && other.TpOffset == TpOffset;
    }

    public abstract record FollowAction
    {
        public sealed record Open(string Dir, FollowOffsets Offsets) : FollowAction;
        public sealed record Modify(FollowOffsets Offsets) : FollowAction;
        public sealed record GiveUp(string Reason) : FollowAction;
    }

    /// <summary>Tracks the active stop and target orders for one security.</summary>
    public sealed class BracketTracker
    {
        private readonly string _security;
        private int _positionSide;
        private long _seq;
        private readonly Dictionary<string, (decimal price, long seq)> _stops = new();
        private readonly Dictionary<string, (decimal price, long seq)> _targets = new();
        private readonly HashSet<string> _seen = new();    // every order id classified so far
        private readonly HashSet<string> _stale = new();   // ids that belong to an earlier position: ignored

        public BracketTracker(string security) { _security = security ?? ""; }

        public void SetPositionSide(int side) => _positionSide = Math.Sign(side);

        public void Reset()
        {
            _stops.Clear();
            _targets.Clear();
        }

        /// <summary>A new position (or flat) starts: forget the old bracket and ignore any late update of its
        /// orders. ATAS reports the new bracket ~0.15 s AFTER the position change (2026-11-02 live bug:
        /// a short opened with the previous long's stop).</summary>
        public void ResetForNewPosition(int side)
        {
            _stale.UnionWith(_seen);
            Reset();
            SetPositionSide(side);
        }

        public void Prime(IEnumerable<OrderSnap> orders, int positionSide)
        {
            Reset();
            SetPositionSide(positionSide);
            foreach (var o in orders)
                if (o.Active) Update(o);
        }

        public void Update(OrderSnap o)
        {
            if (!string.IsNullOrEmpty(_security) && !string.IsNullOrEmpty(o.Security) && o.Security != _security)
                return;
            var comment = (o.Comment ?? "").Trim().ToUpperInvariant();
            var opposite = _positionSide != 0 && o.Side == -_positionSide;
            bool isStop, isTarget;
            if (comment == "SL") { isStop = true; isTarget = false; }
            else if (comment == "TP") { isStop = false; isTarget = true; }
            else
            {
                isStop = opposite && (o.Kind == "Stop" || o.Kind == "StopLimit");
                isTarget = opposite && o.Kind == "Limit";
            }
            if (!isStop && !isTarget) return;
            if (_stale.Contains(o.Id)) return;
            _seen.Add(o.Id);
            var book = isStop ? _stops : _targets;
            if (!o.Active)
            {
                book.Remove(o.Id);
                return;
            }
            var level = isStop ? (o.Trigger > 0 ? o.Trigger : o.Price) : (o.Price > 0 ? o.Price : o.Trigger);
            if (level <= 0) return;
            book[o.Id] = (level, ++_seq);
        }

        public decimal? StopPrice => Latest(_stops);
        public decimal? TargetPrice => Latest(_targets);

        private static decimal? Latest(Dictionary<string, (decimal price, long seq)> book)
        {
            decimal? best = null;
            long bestSeq = -1;
            foreach (var v in book.Values)
                if (v.seq > bestSeq) { bestSeq = v.seq; best = v.price; }
            return best;
        }
    }

    /// <summary>Tracker + gate wired in the only safe order. The indicator talks to this, never to the parts.</summary>
    public sealed class FollowEngine
    {
        private readonly BracketTracker _tracker;
        private readonly FollowGate _gate;

        public FollowEngine(string security, Func<DateTime> clock)
        {
            _tracker = new BracketTracker(security);
            _gate = new FollowGate(clock);
        }

        /// <summary>Position direction changed (incl. flat and flips). Old orders are forgotten; the gate
        /// waits for the NEW stop.</summary>
        public void OnTransition(int side, decimal entry)
        {
            _tracker.ResetForNewPosition(side);
            _gate.OnTransition(side, entry);
        }

        public void OnOrder(OrderSnap order)
        {
            _tracker.Update(order);
            _gate.OnBracket(_tracker.StopPrice, _tracker.TargetPrice);
        }

        /// <summary>Armed while already in a position (ATAS restart): prime from the active orders.</summary>
        public void Resume(int side, decimal entry, IEnumerable<OrderSnap> activeOrders)
        {
            _tracker.Prime(activeOrders, side);
            _gate.Resume(side, entry, _tracker.StopPrice, _tracker.TargetPrice);
        }

        /// <summary>Route the transition events of one position change. A lone "sync" is the provisional
        /// re-seed after an ATAS restart mid-trade: the bracket is already active, so resume (never wait for a
        /// new stop that ATAS will not report again). Anything with open/close is a real new position.</summary>
        public void OnPositionEvents(IReadOnlyList<BridgeEvent> events, int side, decimal entry,
                                     Func<IEnumerable<OrderSnap>> activeOrders)
        {
            if (events.Count == 0) return;
            var real = false;
            foreach (var e in events)
                if (e.Type == "open" || e.Type == "close") real = true;
            if (real) OnTransition(side, entry);
            else Resume(side, entry, activeOrders());
        }

        /// <summary>The open failed or was never sent: stop following this position (no modifies).</summary>
        public void Abandon() => _gate.Abandon();

        /// <summary>Follow mode needs service 2.1+; an older service would execute the open as fixed risk.</summary>
        public static bool ServiceSupportsFollow(string? version)
        {
            if (string.IsNullOrWhiteSpace(version)) return false;
            var parts = version!.Split('.');
            if (!int.TryParse(parts[0], out var major)) return false;
            var minor = parts.Length > 1 && int.TryParse(parts[1], out var m) ? m : 0;
            return major > 2 || (major == 2 && minor >= 1);
        }

        public bool InPosition => _gate.Current != null;
        public IReadOnlyList<FollowAction> Poll() => _gate.Poll();
        public FollowOffsets? Current => _gate.Current;
        public bool StopMissing => _gate.StopMissing;
        public decimal? StopPrice => _tracker.StopPrice;
        public decimal? TargetPrice => _tracker.TargetPrice;
    }

    /// <summary>Turns position transitions and bracket changes into Open / Modify / GiveUp actions.
    /// Not thread-safe: the owner serialises calls.</summary>
    public sealed class FollowGate
    {
        public const double WaitMs = 1500;           // bracket must appear within this after the entry
        public const double DebounceMs = 250;        // a drag sends one modify
        public const double TpRemoveConfirmMs = 1000; // target cancel+new inside this is a move, not a removal

        private enum State { Flat, Waiting, GaveUp, InPosition }

        private readonly Func<DateTime> _now;
        private State _state = State.Flat;
        private int _side;
        private decimal _entry;
        private DateTime _since;
        private decimal? _stop, _target;
        private DateTime _inputChangedAt;
        private DateTime? _tpMissingSince;
        private FollowOffsets? _lastSent;

        public FollowGate(Func<DateTime> clock) { _now = clock; }

        /// <summary>Last offsets sent to the service while in a mirrored position (for sync repair).</summary>
        public FollowOffsets? Current => _state == State.InPosition ? _lastSent : null;

        /// <summary>True while ATAS has no stop order but MT5 keeps the last one.</summary>
        public bool StopMissing { get; private set; }

        public bool Waiting => _state == State.Waiting;
        public bool GaveUp => _state == State.GaveUp;

        public void Abandon()
        {
            if (_state != State.Flat) _state = State.GaveUp;
            _lastSent = null;
        }

        public void OnTransition(int side, decimal entry)
        {
            _side = Math.Sign(side);
            _stop = _target = null;
            _lastSent = null;
            _tpMissingSince = null;
            StopMissing = false;
            if (_side == 0)
            {
                _state = State.Flat;
                return;
            }
            _entry = entry;
            _since = _now();
            _inputChangedAt = _since;
            _state = State.Waiting;
        }

        /// <summary>ATAS restarted mid-trade: continue following an already mirrored position.</summary>
        public void Resume(int side, decimal entry, decimal? stop, decimal? target)
        {
            _side = Math.Sign(side);
            _entry = entry;
            _stop = stop;
            _target = target;
            _state = _side == 0 ? State.Flat : State.InPosition;
            _lastSent = _state == State.InPosition
                ? new FollowOffsets(stop.HasValue ? SlOff(stop.Value) : null, target.HasValue ? TpOff(target.Value) : null, entry)
                : null;
            _inputChangedAt = _now();
        }

        public void OnBracket(decimal? stop, decimal? target)
        {
            if (stop != _stop || target != _target)
                _inputChangedAt = _now();
            if (target == null && _target != null)
                _tpMissingSince = _now();
            if (target != null)
                _tpMissingSince = null;
            _stop = stop;
            _target = target;
        }

        private decimal SlOff(decimal stop) => _side * (_entry - stop);
        private decimal TpOff(decimal target) => _side * (target - _entry);

        public IReadOnlyList<FollowAction> Poll()
        {
            var now = _now();
            switch (_state)
            {
                case State.Waiting:
                    if (_stop.HasValue)
                    {
                        _lastSent = new FollowOffsets(SlOff(_stop.Value), _target.HasValue ? TpOff(_target.Value) : null, _entry);
                        _state = State.InPosition;
                        return new FollowAction[] { new FollowAction.Open(_side > 0 ? "buy" : "sell", _lastSent) };
                    }
                    if ((now - _since).TotalMilliseconds > WaitMs)
                    {
                        _state = State.GaveUp;
                        return new FollowAction[] { new FollowAction.GiveUp(
                            $"Follow ATAS: no stop in ATAS within {WaitMs / 1000:0.#} s, trade not mirrored") };
                    }
                    return Array.Empty<FollowAction>();

                case State.InPosition:
                    decimal? sl;
                    if (_stop.HasValue)
                    {
                        StopMissing = false;
                        sl = SlOff(_stop.Value);
                    }
                    else
                    {
                        StopMissing = true;                 // never unprotect: keep the last MT5 stop
                        sl = _lastSent?.SlOffset;
                    }
                    decimal? tp;
                    if (_target.HasValue)
                        tp = TpOff(_target.Value);
                    else if (_lastSent?.TpOffset != null && _tpMissingSince.HasValue
                             && (now - _tpMissingSince.Value).TotalMilliseconds < TpRemoveConfirmMs)
                        tp = _lastSent.TpOffset;                // maybe a cancel+new move: wait before removing
                    else
                        tp = null;
                    var desired = new FollowOffsets(sl, tp, _entry);
                    if (desired.SameLevels(_lastSent))
                        return Array.Empty<FollowAction>();
                    if ((now - _inputChangedAt).TotalMilliseconds < DebounceMs)
                        return Array.Empty<FollowAction>();
                    _lastSent = desired;
                    return new FollowAction[] { new FollowAction.Modify(desired) };

                default:
                    return Array.Empty<FollowAction>();
            }
        }
    }
}
