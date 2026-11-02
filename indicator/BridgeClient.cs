#nullable enable
// TCP NDJSON client used by the V Bridge indicator. No ATAS or WPF references: compiled into the
// indicator DLL and into the xUnit test project.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace VBridge.Core
{
    /// <summary>Sizing preview returned by the service on sync (and on open acks).</summary>
    public sealed record Preview(string? Symbol, bool Verified, string Source, decimal? Lots, decimal? Balance,
                                 decimal? ValuePerPoint, string? Note);

    public sealed record AckInfo(long Id, string Status, string Summary, Preview? Preview = null,
                                 string ServiceVersion = "", bool Standby = false);

    /// <summary>Per-chart risk and symbol settings, sent with every open and sync (protocol v2/v3). Nulls are omitted.
    /// Mode "follow" adds a follow object: lots from FollowLots, offsets from the explicit argument or Offsets.</summary>
    public sealed record RiskSettings(string? Firm, string? SymbolOverride, string? Instrument, decimal? ChartPrice,
                                      decimal? RiskPct, decimal? StopPoints, decimal? TpPoints, string? RiskBase,
                                      string ClientVersion, string? Mode = null, decimal? FollowLots = null,
                                      FollowOffsets? Offsets = null);

    public sealed record BridgeClientOptions(
        int ReconnectInitialMs = 1000,
        int ReconnectMaxMs = 30000,
        int SyncIntervalMs = 30000,
        int CloseRetryWindowMs = 60000);

    /// <summary>
    /// Persistent connection to the bridge service. One background thread owns the socket:
    /// connect, hello, sync, read acks, periodic sync, reconnect with backoff.
    /// Send() writes from the caller's thread under a lock. An open that cannot be sent is dropped
    /// (never replayed late); a close is kept and resent on reconnect within CloseRetryWindowMs.
    /// </summary>
    public sealed class BridgeClient : IDisposable
    {
        private readonly string _host;
        private readonly int _port;
        private string _token;
        private readonly Func<BridgeEvent?> _syncProvider;

        /// <summary>Called once after the server rejects the current token; a different value is retried.</summary>
        public Func<string?>? TokenFallback { get; set; }

        /// <summary>When this indicator copy armed (unix ms). Sent with every event: the service gives the
        /// chart to the most recently armed copy, however late an older copy speaks.</summary>
        public long? ArmTs { get; set; }
        private bool _fallbackTried;
        private readonly Func<RiskSettings?>? _riskProvider;
        private readonly Action<string> _log;
        private readonly BridgeClientOptions _opt;

        private readonly object _writeLock = new();
        private Thread? _thread;
        private volatile bool _stopping;
        private volatile bool _connected;
        private TcpClient? _tcp;
        private StreamWriter? _writer;
        private long _nextId;
        private readonly Dictionary<long, string> _inflightTypes = new();
        private (string line, DateTime at)? _pendingClose;
        private string _symbol = "", _account = "", _security = "";
        private volatile bool _identitySet;

        public event Action<AckInfo>? AckReceived;
        public bool Connected => _connected;
        public string LastError { get; private set; } = "";

        public BridgeClient(string host, int port, string token, Func<BridgeEvent?> syncProvider, Action<string> log,
                            BridgeClientOptions? options = null, Func<RiskSettings?>? riskProvider = null)
        {
            _host = host;
            _port = port;
            _token = token;
            _syncProvider = syncProvider;
            _riskProvider = riskProvider;
            _log = log;
            _opt = options ?? new BridgeClientOptions();
        }

        public void Start()
        {
            if (_thread != null) return;
            _stopping = false;
            _thread = new Thread(Loop) { IsBackground = true, Name = "vbridge-client" };
            _thread.Start();
        }

        public void Stop()
        {
            _stopping = true;
            CloseSocket();
            _thread?.Join(2000);
            _thread = null;
        }

        public void Dispose() => Stop();

        /// <summary>Who this client speaks for. Syncs are not sent until this has been called.</summary>
        public void SetIdentity(string symbol, string account, string security)
        {
            var changed = !_identitySet || symbol != _symbol || account != _account || security != _security;
            lock (_writeLock)
            {
                _symbol = symbol; _account = account; _security = security;
                _identitySet = !string.IsNullOrEmpty(symbol);
            }
            if (changed && _identitySet && _connected) SendSync();
        }

        /// <summary>Returns the event id, or -1 when not connected.</summary>
        public long Send(string type, string? dir, string symbol, string account, string security, decimal price,
                         decimal volume, RiskSettings? risk = null, FollowOffsets? follow = null)
        {
            var id = Interlocked.Increment(ref _nextId);
            var line = BuildLine(id, type, dir, symbol, account, security, price, volume, risk, follow);
            if (!WriteLine(line, id, type))
            {
                if (type == "close")
                {
                    lock (_writeLock) _pendingClose = (line, DateTime.UtcNow);
                    _log($"close #{id} queued until reconnect");
                }
                else
                {
                    _log($"{type} #{id} dropped: not connected");
                }
                return -1;
            }
            return id;
        }

        private string BuildLine(long id, string type, string? dir, string symbol, string account, string security,
                                 decimal price, decimal volume, RiskSettings? risk, FollowOffsets? follow = null)
        {
            var sb = new StringBuilder(384);
            sb.Append("{\"id\":").Append(id)
              .Append(",\"type\":\"").Append(type).Append('"');
            if (dir != null) sb.Append(",\"dir\":\"").Append(dir).Append('"');
            sb.Append(",\"symbol\":").Append(JsonSerializer.Serialize(symbol))
              .Append(",\"account\":").Append(JsonSerializer.Serialize(account))
              .Append(",\"security\":").Append(JsonSerializer.Serialize(security))
              .Append(",\"price\":").Append(price.ToString(CultureInfo.InvariantCulture))
              .Append(",\"volume\":").Append(volume.ToString(CultureInfo.InvariantCulture))
              .Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture))
              .Append('"');
            if (ArmTs.HasValue) sb.Append(",\"arm_ts\":").Append(ArmTs.Value.ToString(CultureInfo.InvariantCulture));
            if (risk != null)
            {
                AppendStr(sb, "firm", risk.Firm);
                AppendStr(sb, "symbol_override", risk.SymbolOverride);
                AppendStr(sb, "instrument", risk.Instrument);
                AppendNum(sb, "chart_price", risk.ChartPrice);
                AppendNum(sb, "risk_pct", risk.RiskPct);
                AppendNum(sb, "stop_points", risk.StopPoints);
                AppendNum(sb, "tp_points", risk.TpPoints);
                AppendStr(sb, "risk_base", risk.RiskBase);
                AppendStr(sb, "client_version", risk.ClientVersion);
                AppendStr(sb, "mode", risk.Mode);
                if (risk.Mode == "follow")
                {
                    var off = follow ?? risk.Offsets;
                    sb.Append(",\"follow\":{\"lots\":")
                      .Append((risk.FollowLots ?? 0m).ToString(CultureInfo.InvariantCulture));
                    sb.Append(",\"sl_offset\":").Append(Num(off?.SlOffset))
                      .Append(",\"tp_offset\":").Append(Num(off?.TpOffset));
                    if (off != null)
                        sb.Append(",\"atas_entry\":").Append(off.AtasEntry.ToString(CultureInfo.InvariantCulture));
                    sb.Append('}');
                }
            }
            sb.Append('}');
            return sb.ToString();
        }

        private static string Num(decimal? v) => v.HasValue ? v.Value.ToString(CultureInfo.InvariantCulture) : "null";

        private static void AppendStr(StringBuilder sb, string key, string? value)
        {
            if (string.IsNullOrEmpty(value)) return;
            sb.Append(",\"").Append(key).Append("\":").Append(JsonSerializer.Serialize(value));
        }

        private static void AppendNum(StringBuilder sb, string key, decimal? value)
        {
            if (value == null) return;
            sb.Append(",\"").Append(key).Append("\":").Append(value.Value.ToString(CultureInfo.InvariantCulture));
        }

        private bool WriteLine(string line, long id, string type)
        {
            lock (_writeLock)
            {
                if (!_connected || _writer == null) return false;
                try
                {
                    _writer.Write(line);
                    _writer.Write('\n');
                    _writer.Flush();
                    _inflightTypes[id] = type;
                    return true;
                }
                catch (Exception ex)
                {
                    LastError = ex.Message;
                    _log("write failed: " + ex.Message);
                    _connected = false;
                    return false;
                }
            }
        }

        private void Loop()
        {
            var delay = _opt.ReconnectInitialMs;
            while (!_stopping)
            {
                try
                {
                    RunConnection();
                    delay = _opt.ReconnectInitialMs;
                }
                catch (Exception ex)
                {
                    LastError = ex.Message;
                    _log("connection: " + ex.Message);
                }
                finally
                {
                    CloseSocket();
                }
                if (_stopping) break;
                Thread.Sleep(delay);
                delay = Math.Min(delay * 2, _opt.ReconnectMaxMs);
            }
        }

        private void RunConnection()
        {
            var tcp = new TcpClient { NoDelay = true };
            tcp.Connect(_host, _port);
            var stream = tcp.GetStream();
            var reader = new StreamReader(stream, new UTF8Encoding(false));
            var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = false };
            writer.Write("{\"type\":\"hello\",\"token\":" + JsonSerializer.Serialize(_token) + "}\n");
            writer.Flush();
            var helloAck = reader.ReadLine();
            if (helloAck == null || !helloAck.Contains("\"hello\":\"ok\""))
            {
                // Re-read the pairing file after EVERY rejection: the service may rewrite it at any time
                // (restart, legacy import), and a stale token must never lock the chart out for good.
                if (TokenFallback != null)
                {
                    _fallbackTried = true;
                    string? alt = null;
                    try { alt = TokenFallback(); } catch (Exception ex) { _log("token fallback failed: " + ex.Message); }
                    if (!string.IsNullOrEmpty(alt) && alt != _token)
                    {
                        _token = alt!;
                        _log("token rejected by the service; switching to pairing token");
                    }
                }
                throw new IOException("hello rejected: " + (helloAck ?? "connection closed"));
            }
            _fallbackTried = false;
            lock (_writeLock)
            {
                _tcp = tcp;
                _writer = writer;
                _connected = true;
            }
            _log("connected to " + _host + ":" + _port);
            SendSync();
            FlushPendingClose();

            stream.ReadTimeout = Math.Max(250, _opt.SyncIntervalMs);
            var lastSync = DateTime.UtcNow;
            while (!_stopping && _connected)
            {
                string? line;
                try
                {
                    line = reader.ReadLine();
                }
                catch (IOException)
                {
                    if (_stopping || !_connected) break;
                    if ((DateTime.UtcNow - lastSync).TotalMilliseconds >= _opt.SyncIntervalMs)
                    {
                        SendSync();
                        lastSync = DateTime.UtcNow;
                    }
                    continue;
                }
                if (line == null) throw new IOException("server closed the connection");
                HandleAck(line);
                if ((DateTime.UtcNow - lastSync).TotalMilliseconds >= _opt.SyncIntervalMs)
                {
                    SendSync();
                    lastSync = DateTime.UtcNow;
                }
            }
        }

        private void SendSync()
        {
            if (!_identitySet)
            {
                _log("sync skipped: identity not set yet");
                return;
            }
            BridgeEvent? ev;
            try { ev = _syncProvider(); }
            catch (Exception ex) { _log("sync provider failed: " + ex.Message); return; }
            if (ev == null) return;
            RiskSettings? risk = null;
            try { risk = _riskProvider?.Invoke(); }
            catch (Exception ex) { _log("risk provider failed: " + ex.Message); }
            var id = Interlocked.Increment(ref _nextId);
            WriteLine(BuildLine(id, ev.Type, ev.Dir, _symbol, _account, _security, 0m, 0m, risk), id, ev.Type);
        }

        private void FlushPendingClose()
        {
            (string line, DateTime at)? pending;
            lock (_writeLock) { pending = _pendingClose; _pendingClose = null; }
            if (pending == null) return;
            if ((DateTime.UtcNow - pending.Value.at).TotalMilliseconds > _opt.CloseRetryWindowMs)
            {
                _log("queued close expired, not sent (sync will reconcile)");
                return;
            }
            var id = Interlocked.Increment(ref _nextId);
            // keep the original payload but give it a fresh id so acks stay unique
            var line = System.Text.RegularExpressions.Regex.Replace(pending.Value.line, "^\\{\"id\":\\d+", "{\"id\":" + id);
            if (WriteLine(line, id, "close")) _log($"queued close resent as #{id}");
        }

        private void HandleAck(string line)
        {
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (!root.TryGetProperty("id", out var idEl) || idEl.ValueKind != JsonValueKind.Number) return;
                var id = idEl.GetInt64();
                lock (_writeLock) _inflightTypes.Remove(id);
                var worst = 0;
                var parts = new List<string>();
                Preview? preview = null;
                var serviceVersion = root.TryGetProperty("service_version", out var sv) ? sv.GetString() ?? "" : "";
                if (root.TryGetProperty("results", out var results))
                {
                    foreach (var r in results.EnumerateArray())
                    {
                        var status = r.TryGetProperty("status", out var s) ? s.GetString() ?? "" : "";
                        worst = Math.Max(worst, Rank(status));
                        if (preview == null && r.TryGetProperty("preview", out var pv) && pv.ValueKind == JsonValueKind.Object)
                            preview = ParsePreview(pv);
                        var sb = new StringBuilder();
                        sb.Append(r.TryGetProperty("account", out var a) ? a.GetString() : "?").Append(' ').Append(status);
                        if (r.TryGetProperty("lots", out var lots)) sb.Append(' ').Append(lots.GetDouble().ToString(CultureInfo.InvariantCulture)).Append(" lots");
                        if (r.TryGetProperty("ticket", out var t)) sb.Append(" #").Append(t.GetInt64());
                        if (r.TryGetProperty("closed", out var closed) && closed.ValueKind == JsonValueKind.Array)
                            sb.Append(" closed ").Append(closed.GetArrayLength());
                        if (r.TryGetProperty("reason", out var reason)) sb.Append(' ').Append(reason.GetString());
                        parts.Add(sb.ToString());
                    }
                }
                var standby = false;
                if (root.TryGetProperty("results", out var rs2))
                    foreach (var r in rs2.EnumerateArray())
                        if (r.TryGetProperty("reason", out var rr) && (rr.GetString() ?? "").StartsWith("standby"))
                            standby = true;
                var info = new AckInfo(id, worst == 2 ? "error" : worst == 1 ? "warn" : "ok", string.Join(" | ", parts),
                                       preview, serviceVersion, standby);
                AckReceived?.Invoke(info);
            }
            catch (Exception ex)
            {
                _log("bad ack: " + ex.Message);
            }
        }

        private static Preview ParsePreview(JsonElement pv)
        {
            string? Str(string k) => pv.TryGetProperty(k, out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
            decimal? Num(string k) => pv.TryGetProperty(k, out var e) && e.ValueKind == JsonValueKind.Number ? e.GetDecimal() : null;
            var verified = pv.TryGetProperty("verified", out var v) && v.ValueKind == JsonValueKind.True;
            return new Preview(Str("symbol"), verified, Str("source") ?? "", Num("lots"), Num("balance"),
                               Num("value_per_point"), Str("note"));
        }

        private static int Rank(string status) => status == "error" ? 2 : status == "warn" ? 1 : 0;

        private void CloseSocket()
        {
            lock (_writeLock)
            {
                _connected = false;
                try { _writer?.Dispose(); } catch { }
                try { _tcp?.Close(); } catch { }
                _writer = null;
                _tcp = null;
            }
        }
    }
}
