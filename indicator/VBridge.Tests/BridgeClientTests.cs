using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading;
using VBridge.Core;
using Xunit;

namespace VBridge.Tests;

/// <summary>Minimal NDJSON server: records every line, answers hello, answers events with a canned ack.</summary>
internal sealed class FakeServer : IDisposable
{
    private readonly TcpListener _listener;
    private readonly Thread _thread;
    private volatile bool _stop;
    private volatile bool _pauseAccept;
    private TcpClient? _client;
    public readonly ConcurrentQueue<JsonDocument> Lines = new();
    public volatile string ResultsJson = "[{\"account\":\"a\",\"status\":\"ok\",\"lots\":0.8,\"ticket\":123456}]";
    public volatile string ServiceVersion = "2.0.0";
    public volatile string? AcceptToken = null;   // when set, only this token is accepted
    public int Port { get; }

    public FakeServer()
    {
        _listener = new TcpListener(IPAddress.Loopback, 0);
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _thread = new Thread(Loop) { IsBackground = true };
        _thread.Start();
    }

    public bool PauseAccept { set => _pauseAccept = value; }

    public void Drop()
    {
        try { _client?.Close(); } catch { }
    }

    private void Loop()
    {
        while (!_stop)
        {
            TcpClient client;
            try { client = _listener.AcceptTcpClient(); } catch { return; }
            if (_pauseAccept) { client.Close(); continue; }
            _client = client;
            try
            {
                using var stream = client.GetStream();
                using var reader = new StreamReader(stream);
                using var writer = new StreamWriter(stream) { AutoFlush = true, NewLine = "\n" };
                string? line;
                while ((line = reader.ReadLine()) != null)
                {
                    var doc = JsonDocument.Parse(line);
                    Lines.Enqueue(doc);
                    var type = doc.RootElement.GetProperty("type").GetString();
                    if (type == "hello")
                    {
                        var tok = doc.RootElement.TryGetProperty("token", out var t) ? t.GetString() : "";
                        if (AcceptToken != null && tok != AcceptToken) break;   // reject: close like the real server
                        writer.WriteLine("{\"id\":null,\"hello\":\"ok\"}");
                    }
                    else if (doc.RootElement.TryGetProperty("id", out var id))
                        writer.WriteLine($"{{\"id\":{id.GetInt64()},\"results\":{ResultsJson},\"service_version\":\"{ServiceVersion}\"}}");
                }
            }
            catch { }
            finally { client.Close(); }
        }
    }

    public JsonDocument WaitLine(Func<JsonDocument, bool> pred, int timeoutMs = 5000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        var seen = new List<JsonDocument>();
        while (DateTime.UtcNow < deadline)
        {
            while (Lines.TryDequeue(out var d))
            {
                if (pred(d)) return d;
                seen.Add(d);
            }
            Thread.Sleep(20);
        }
        throw new TimeoutException("line not received; saw: " + string.Join(" | ", seen.Select(s => s.RootElement.ToString())));
    }

    public static string Type(JsonDocument d) => d.RootElement.GetProperty("type").GetString()!;

    public void Dispose()
    {
        _stop = true;
        Drop();
        _listener.Stop();
    }
}

public class BridgeClientTests
{
    private static readonly BridgeClientOptions Fast = new(ReconnectInitialMs: 50, ReconnectMaxMs: 100,
        SyncIntervalMs: 60000, CloseRetryWindowMs: 5000);

    private static BridgeClient NewClient(int port, Func<BridgeEvent?> sync, List<AckInfo>? acks = null)
    {
        var c = new BridgeClient("127.0.0.1", port, "tok", sync, _ => { }, Fast);
        if (acks != null) c.AckReceived += a => { lock (acks) acks.Add(a); };
        return c;
    }

    private static void WaitUntil(Func<bool> cond, int timeoutMs = 5000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (!cond() && DateTime.UtcNow < deadline) Thread.Sleep(20);
        Assert.True(cond(), "condition not met in time");
    }

    [Fact]
    public void Sync_NotSentUntilIdentityIsSet()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => new BridgeEvent("sync", "flat"));
        client.Start();
        Assert.Equal("hello", FakeServer.Type(server.WaitLine(d => true)));
        WaitUntil(() => client.Connected);
        Thread.Sleep(300);
        Assert.Throws<TimeoutException>(() => server.WaitLine(d => FakeServer.Type(d) == "sync", 300));
        client.SetIdentity("ES", "acc9", "ESZ6");
        var sync = server.WaitLine(d => FakeServer.Type(d) == "sync");
        Assert.Equal("ES", sync.RootElement.GetProperty("symbol").GetString());
        Assert.Equal("acc9", sync.RootElement.GetProperty("account").GetString());
        Assert.Equal("ESZ6", sync.RootElement.GetProperty("security").GetString());
    }

    [Fact]
    public void Connect_SendsHelloFirst_ThenSync()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => new BridgeEvent("sync", "flat"));
        client.SetIdentity("NQ", "acc1", "NQZ6");
        client.Start();
        var hello = server.WaitLine(d => true);
        Assert.Equal("hello", FakeServer.Type(hello));
        Assert.Equal("tok", hello.RootElement.GetProperty("token").GetString());
        var sync = server.WaitLine(d => true);
        Assert.Equal("sync", FakeServer.Type(sync));
        Assert.Equal("flat", sync.RootElement.GetProperty("dir").GetString());
        WaitUntil(() => client.Connected);
    }

    [Fact]
    public void Send_WritesOneJsonLine_AndRaisesAck()
    {
        using var server = new FakeServer();
        var acks = new List<AckInfo>();
        using var client = NewClient(server.Port, () => null, acks);
        client.Start();
        WaitUntil(() => client.Connected);
        var id = client.Send("open", "buy", "NQ", "acc1", "NQZ6", 20123.5m, 1m);
        Assert.True(id >= 1);
        var line = server.WaitLine(d => FakeServer.Type(d) == "open");
        var r = line.RootElement;
        Assert.Equal(id, r.GetProperty("id").GetInt64());
        Assert.Equal("buy", r.GetProperty("dir").GetString());
        Assert.Equal("NQ", r.GetProperty("symbol").GetString());
        Assert.Equal("acc1", r.GetProperty("account").GetString());
        Assert.Equal("NQZ6", r.GetProperty("security").GetString());
        Assert.Equal(20123.5, r.GetProperty("price").GetDouble());
        Assert.False(string.IsNullOrEmpty(r.GetProperty("ts").GetString()));
        WaitUntil(() => { lock (acks) return acks.Count > 0; });
        AckInfo ack;
        lock (acks) ack = acks[0];
        Assert.Equal(id, ack.Id);
        Assert.Equal("ok", ack.Status);
        Assert.Contains("a ok 0.8 lots #123456", ack.Summary);
    }

    [Fact]
    public void Send_WhenDisconnected_ReturnsMinusOne()
    {
        int deadPort;
        using (var l = new TcpListener(IPAddress.Loopback, 0)) { l.Start(); deadPort = ((IPEndPoint)l.LocalEndpoint).Port; l.Stop(); }
        using var client = NewClient(deadPort, () => null);
        client.Start();
        Thread.Sleep(200);
        Assert.False(client.Connected);
        Assert.Equal(-1, client.Send("open", "buy", "NQ", "acc1", "NQZ6", 1m, 1m));
    }

    [Fact]
    public void Reconnect_SendsHelloAndSync()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => new BridgeEvent("sync", "sell"));
        client.SetIdentity("NQ", "acc1", "NQZ6");
        client.Start();
        server.WaitLine(d => FakeServer.Type(d) == "sync");
        WaitUntil(() => client.Connected);
        server.Drop();
        WaitUntil(() => !client.Connected);
        var hello = server.WaitLine(d => true);
        Assert.Equal("hello", FakeServer.Type(hello));
        var sync = server.WaitLine(d => true);
        Assert.Equal("sync", FakeServer.Type(sync));
        Assert.Equal("sell", sync.RootElement.GetProperty("dir").GetString());
        WaitUntil(() => client.Connected);
    }

    [Fact]
    public void Close_SentAfterReconnect_WithinWindow()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => new BridgeEvent("sync", "flat"));
        client.SetIdentity("NQ", "acc1", "NQZ6");
        client.Start();
        WaitUntil(() => client.Connected);
        server.PauseAccept = true;
        server.Drop();
        WaitUntil(() => !client.Connected);
        Assert.Equal(-1, client.Send("close", null, "NQ", "acc1", "NQZ6", 0m, 0m));
        Thread.Sleep(300);
        server.PauseAccept = false;
        var close = server.WaitLine(d => FakeServer.Type(d) == "close");
        Assert.Equal("NQ", close.RootElement.GetProperty("symbol").GetString());
    }

    [Fact]
    public void OpenNotQueuedAcrossReconnect()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => new BridgeEvent("sync", "flat"));
        client.SetIdentity("NQ", "acc1", "NQZ6");
        client.Start();
        WaitUntil(() => client.Connected);
        server.PauseAccept = true;
        server.Drop();
        WaitUntil(() => !client.Connected);
        Assert.Equal(-1, client.Send("open", "buy", "NQ", "acc1", "NQZ6", 0m, 1m));
        server.PauseAccept = false;
        WaitUntil(() => client.Connected);
        Thread.Sleep(500);
        Assert.Throws<TimeoutException>(() => server.WaitLine(d => FakeServer.Type(d) == "open", 500));
    }

    private static readonly RiskSettings Risk = new("FundingPips", null, "NQZ6@CME", 29470.25m, 0.5m, 10m, 0m, "balance", "2.0.0");

    [Fact]
    public void Send_WithRiskSettings_WritesV2Fields()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => null);
        client.Start();
        WaitUntil(() => client.Connected);
        client.Send("open", "buy", "NQ", "acc1", "NQZ6", 29470m, 1m, Risk);
        var r = server.WaitLine(d => FakeServer.Type(d) == "open").RootElement;
        Assert.Equal("FundingPips", r.GetProperty("firm").GetString());
        Assert.Equal("NQZ6@CME", r.GetProperty("instrument").GetString());
        Assert.Equal(29470.25, r.GetProperty("chart_price").GetDouble());
        Assert.Equal(0.5, r.GetProperty("risk_pct").GetDouble());
        Assert.Equal(10, r.GetProperty("stop_points").GetDouble());
        Assert.Equal(0, r.GetProperty("tp_points").GetDouble());
        Assert.Equal("balance", r.GetProperty("risk_base").GetString());
        Assert.Equal("2.0.0", r.GetProperty("client_version").GetString());
        Assert.False(r.TryGetProperty("symbol_override", out _));   // null -> omitted
    }

    [Fact]
    public void Send_WithoutRiskSettings_OmitsV2Fields()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => null);
        client.Start();
        WaitUntil(() => client.Connected);
        client.Send("open", "buy", "NQ", "acc1", "NQZ6", 29470m, 1m);
        var r = server.WaitLine(d => FakeServer.Type(d) == "open").RootElement;
        Assert.False(r.TryGetProperty("risk_pct", out _));
        Assert.False(r.TryGetProperty("firm", out _));
    }

    [Fact]
    public void Ack_ParsesPreviewAndServiceVersion()
    {
        using var server = new FakeServer();
        server.ResultsJson = "[{\"account\":\"a\",\"status\":\"warn\",\"reason\":\"confirm symbol NDX100 for NQ in the tray\"," +
                             "\"preview\":{\"symbol\":\"NDX100\",\"verified\":false,\"source\":\"discovery\",\"lots\":1.25," +
                             "\"balance\":50000.0,\"value_per_point\":20.0,\"note\":\"confirm symbol NDX100 for NQ in the tray\"}}]";
        server.ServiceVersion = "2.3.1";
        var acks = new List<AckInfo>();
        using var client = NewClient(server.Port, () => null, acks);
        client.Start();
        WaitUntil(() => client.Connected);
        client.Send("sync", "flat", "NQ", "acc1", "NQZ6", 0m, 0m, Risk);
        WaitUntil(() => { lock (acks) return acks.Count > 0; });
        AckInfo ack;
        lock (acks) ack = acks[0];
        Assert.Equal("2.3.1", ack.ServiceVersion);
        Assert.NotNull(ack.Preview);
        Assert.Equal("NDX100", ack.Preview!.Symbol);
        Assert.False(ack.Preview.Verified);
        Assert.Equal(1.25m, ack.Preview.Lots);
        Assert.Contains("confirm symbol", ack.Preview.Note);
    }

    [Fact]
    public void Sync_CarriesRiskAndChartPrice()
    {
        using var server = new FakeServer();
        var client = new BridgeClient("127.0.0.1", server.Port, "tok", () => new BridgeEvent("sync", "flat"), _ => { }, Fast,
                                      () => Risk);
        using (client)
        {
            client.SetIdentity("NQ", "acc1", "NQZ6");
            client.Start();
            var sync = server.WaitLine(d => FakeServer.Type(d) == "sync").RootElement;
            Assert.Equal(29470.25, sync.GetProperty("chart_price").GetDouble());
            Assert.Equal(0.5, sync.GetProperty("risk_pct").GetDouble());
            Assert.Equal("FundingPips", sync.GetProperty("firm").GetString());
        }
    }

    [Fact]
    public void Hello_RejectedThenFallbackTokenUsed()
    {
        using var server = new FakeServer();
        server.AcceptToken = "paired-token";
        var logs = new List<string>();
        using var client = new BridgeClient("127.0.0.1", server.Port, "stale-typed-token", () => null,
                                            l => { lock (logs) logs.Add(l); }, Fast)
        { TokenFallback = () => "paired-token" };
        client.Start();
        WaitUntil(() => client.Connected, 8000);
        Assert.Contains(logs, l => l.Contains("switching to pairing token"));
    }

    [Fact]
    public void Hello_RejectedRepeatedly_PicksUpPairingTokenWhenItChanges()
    {
        using var server = new FakeServer();
        server.AcceptToken = "good";
        var pairingFile = "stale";                                  // the file is still wrong at first ...
        using var client = new BridgeClient("127.0.0.1", server.Port, "stale", () => null, _ => { }, Fast)
        { TokenFallback = () => pairingFile };
        client.Start();
        Thread.Sleep(400);
        Assert.False(client.Connected);
        pairingFile = "good";                                       // ... until the service rewrites it
        WaitUntil(() => client.Connected, 8000);
    }

    [Fact]
    public void Hello_RejectedWithoutFallback_StaysDisconnected()
    {
        using var server = new FakeServer();
        server.AcceptToken = "paired-token";
        using var client = new BridgeClient("127.0.0.1", server.Port, "wrong", () => null, _ => { }, Fast);
        client.Start();
        Thread.Sleep(600);
        Assert.False(client.Connected);
    }

    [Fact]
    public void Send_FollowOpen_WritesFollowObject()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => null);
        client.Start();
        WaitUntil(() => client.Connected);
        var risk = Risk with { Mode = "follow", FollowLots = 0.5m };
        client.Send("open", "buy", "NQ", "acc1", "NQZ6", 28775.5m, 1m, risk, new FollowOffsets(20m, 12.5m, 28775.5m));
        var r = server.WaitLine(d => FakeServer.Type(d) == "open").RootElement;
        Assert.Equal("follow", r.GetProperty("mode").GetString());
        var f = r.GetProperty("follow");
        Assert.Equal(0.5, f.GetProperty("lots").GetDouble());
        Assert.Equal(20, f.GetProperty("sl_offset").GetDouble());
        Assert.Equal(12.5, f.GetProperty("tp_offset").GetDouble());
        Assert.Equal(28775.5, f.GetProperty("atas_entry").GetDouble());
    }

    [Fact]
    public void Send_Modify_NullTargetWrittenAsNull()
    {
        using var server = new FakeServer();
        using var client = NewClient(server.Port, () => null);
        client.Start();
        WaitUntil(() => client.Connected);
        var risk = Risk with { Mode = "follow", FollowLots = 0.5m };
        client.Send("modify", null, "NQ", "acc1", "NQZ6", 0m, 0m, risk, new FollowOffsets(0m, null, 28775.5m));
        var f = server.WaitLine(d => FakeServer.Type(d) == "modify").RootElement.GetProperty("follow");
        Assert.Equal(0, f.GetProperty("sl_offset").GetDouble());
        Assert.Equal(System.Text.Json.JsonValueKind.Null, f.GetProperty("tp_offset").ValueKind);
    }

    [Fact]
    public void Sync_InFollowMode_CarriesCurrentOffsets()
    {
        using var server = new FakeServer();
        var risk = Risk with { Mode = "follow", FollowLots = 0.5m, Offsets = new FollowOffsets(5m, 30m, 28775.5m) };
        var client = new BridgeClient("127.0.0.1", server.Port, "tok", () => new BridgeEvent("sync", "buy"), _ => { },
                                      Fast, () => risk);
        using (client)
        {
            client.SetIdentity("NQ", "acc1", "NQZ6");
            client.Start();
            var f = server.WaitLine(d => FakeServer.Type(d) == "sync").RootElement.GetProperty("follow");
            Assert.Equal(5, f.GetProperty("sl_offset").GetDouble());
            Assert.Equal(30, f.GetProperty("tp_offset").GetDouble());
        }
    }

    [Fact]
    public void Ack_StandbyFlag()
    {
        using var server = new FakeServer();
        server.ResultsJson = "[{\"account\":\"a\",\"status\":\"warn\",\"reason\":\"standby: replaced by a newer copy for acc1/NQ\"}]";
        var acks = new List<AckInfo>();
        using var client = NewClient(server.Port, () => null, acks);
        client.Start();
        WaitUntil(() => client.Connected);
        client.Send("sync", "flat", "NQ", "acc1", "NQZ6", 0m, 0m);
        WaitUntil(() => { lock (acks) return acks.Count > 0; });
        lock (acks) Assert.True(acks[0].Standby);
    }

    [Fact]
    public void Registry_NewerGenerationWins()
    {
        var key = "acc-" + Guid.NewGuid().ToString("N") + "|NQ";
        var g1 = InstanceRegistry.Claim(key);
        Assert.True(InstanceRegistry.IsCurrent(key, g1));
        var g2 = InstanceRegistry.Claim(key);
        Assert.False(InstanceRegistry.IsCurrent(key, g1));
        Assert.True(InstanceRegistry.IsCurrent(key, g2));
        InstanceRegistry.Release(key, g1);                     // the old copy leaving must not evict the new one
        Assert.True(InstanceRegistry.IsCurrent(key, g2));
    }

    [Fact]
    public void AckStatus_IsWorstOfResults()
    {
        using var server = new FakeServer();
        server.ResultsJson = "[{\"account\":\"a\",\"status\":\"ok\",\"lots\":1.25,\"ticket\":7}," +
                             "{\"account\":\"b\",\"status\":\"warn\",\"reason\":\"not mirrored\"}," +
                             "{\"account\":\"c\",\"status\":\"error\",\"reason\":\"login mismatch\"}]";
        var acks = new List<AckInfo>();
        using var client = NewClient(server.Port, () => null, acks);
        client.Start();
        WaitUntil(() => client.Connected);
        client.Send("open", "sell", "NQ", "acc1", "NQZ6", 0m, 1m);
        WaitUntil(() => { lock (acks) return acks.Count > 0; });
        AckInfo ack;
        lock (acks) ack = acks[0];
        Assert.Equal("error", ack.Status);
        Assert.Contains("a ok 1.25 lots #7", ack.Summary);
        Assert.Contains("b warn not mirrored", ack.Summary);
        Assert.Contains("c error login mismatch", ack.Summary);
    }
}

public class ArmTsTests
{
    [Fact]
    public void Send_WritesArmTs()
    {
        using var server = new FakeServer();
        using var client = new BridgeClient("127.0.0.1", server.Port, "tok", () => null, _ => { },
            new BridgeClientOptions(50, 100, 60000, 5000)) { ArmTs = 1730552400123 };
        client.Start();
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (!client.Connected && DateTime.UtcNow < deadline) System.Threading.Thread.Sleep(20);
        client.Send("sync", "flat", "NQ", "a", "s", 0m, 0m);
        var r = server.WaitLine(d => FakeServer.Type(d) == "sync").RootElement;
        Assert.Equal(1730552400123, r.GetProperty("arm_ts").GetInt64());
    }
}
