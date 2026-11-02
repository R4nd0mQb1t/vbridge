using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Threading;
using ATAS.DataFeedsCore;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;
using VBridge.Core;

namespace ATAS.Indicators.Technical;

public enum PropFirm
{
	FundingPips,
	FTMO,
	The5ers,
	FundedNext,
	GoatFunded,
	TradingPit,
	Other
}

public enum RiskBase
{
	Balance,
	Equity
}

public enum ExecutionMode
{
	[Description("Fixed risk (risk %, stop, target)")]
	FixedRisk,
	[Description("Follow ATAS (lots + ATAS stop/target)")]
	FollowAtas,
	[Description("Percent Follow (risk % + ATAS stop/target)")]
	PercentFollow
}

public enum StatusCorner
{
	TopRight,
	TopLeft,
	BottomRight,
	BottomLeft
}

/// <summary>
/// V Bridge: watches the position of the account/instrument this chart trades and mirrors it to the
/// bridge service (which executes on MT5 CFD accounts). Two modes:
///  - Fixed risk: the bridge sizes from risk % and uses its own stop/target distances.
///  - Follow ATAS: the bridge opens a fixed lot size with the stop/target you placed in ATAS, and follows
///    later moves of either.
///  - Percent Follow: as Follow ATAS, but the lot size comes from risk % over the ATAS stop distance.
/// Sensor + display only: this indicator never places an order in ATAS.
/// </summary>
[DisplayName("V Bridge")]
[Description("Mirrors this chart's position to the ATAS->MT5 bridge service.")]
public class VBridge : Indicator
{
	public const string ClientVersion = "2.2.0";

	private const string AccountGroup = "1. Account";
	private const string RiskGroup = "2. Risk";
	private const string BridgeGroup = "3. Bridge";

	private static readonly RenderFont StatusFont = new("Segoe UI", 10f);
	private static readonly object LogLock = new();
	private static readonly string LogPath = Path.Combine(
		Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "ATAS X", "Logs", "vbridge.log");

	private readonly Transitions _transitions = new();
	private FollowEngine _follow = new("", () => DateTime.UtcNow);
	private readonly object _lock = new();   // serialises Transitions/Gate/Tracker across ATAS, client and timer threads
	private BridgeClient _client;
	private ITradingManager _tm;
	private Timer _followTimer;
	private bool _subscribed;
	private string _registryKey = "";
	private long _generation;
	private bool _standby;          // this copy lost the in-process registry
	private bool _serviceStandby;   // the service answers 'standby' (another copy armed later); not sticky
	private long _armTs;
	private long _pendingOpenId = -1;
	private string _lastEvent = "-";
	private string _lastAck = "-";
	private string _lastAckStatus = "ok";
	private string _followNote = "";
	private Preview _preview;
	private string _serviceVersion = "";
	private decimal _lastVolume;
	private decimal _chartPrice;
	private string _pairingNote = "";
	private string _account = "";
	private string _security = "";

	// settings
	private PropFirm _firm = PropFirm.FundingPips;
	private string _symbolOverride = "";
	private ExecutionMode _mode = ExecutionMode.FixedRisk;
	private decimal _lots = 0.10m;
	private decimal _riskPct = 1.0m;
	private decimal _stopPoints = 20m;
	private decimal _tpPoints = 40m;
	private RiskBase _riskBase = RiskBase.Balance;
	private bool _enabled = true;
	private bool _showStatus = true;
	private StatusCorner _corner = StatusCorner.TopRight;
	private string _host = "127.0.0.1";
	private int _port = 5577;
	private string _token = "";
	private string _symbolAlias = "";

	public VBridge()
	{
		DenyToChangePanel = true;
		EnableCustomDrawing = true;
		SubscribeToDrawingEvents(DrawingLayouts.LatestBar);
		DataSeries[0].IsHidden = true;
	}

	// ATAS builds the chart legend from ToString(); the default lists every parameter.
	public override string ToString() => "V Bridge";

	// ------------------------------------------------------------------ 1. Account

	[OFT.Attributes.Parameter]
	[Display(Name = "Prop firm", GroupName = AccountGroup, Order = 10)]
	public PropFirm Firm
	{
		get => _firm;
		set { _firm = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Broker symbol override (blank = automatic)", GroupName = AccountGroup, Order = 20)]
	public string SymbolOverride
	{
		get => _symbolOverride;
		set { _symbolOverride = (value ?? "").Trim(); RecalculateValues(); }
	}

	// ------------------------------------------------------------------ 2. Risk

	[OFT.Attributes.Parameter]
	[Display(Name = "Mode", GroupName = RiskGroup, Order = 5)]
	public ExecutionMode Mode
	{
		get => _mode;
		set { _mode = value; Log($"Mode={value}"); RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Lots (Follow ATAS mode)", GroupName = RiskGroup, Order = 6)]
	[Range(0.01, 1000)]
	public decimal Lots
	{
		get => _lots;
		set { _lots = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Risk % per trade (Fixed risk / Percent Follow)", GroupName = RiskGroup, Order = 10)]
	[Range(0.01, 100)]
	public decimal RiskPct
	{
		get => _riskPct;
		set { _riskPct = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Stop (points, Fixed risk mode)", GroupName = RiskGroup, Order = 20)]
	[Range(0.01, 1000000)]
	public decimal StopPoints
	{
		get => _stopPoints;
		set { _stopPoints = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Target (points, 0 = none, Fixed risk mode)", GroupName = RiskGroup, Order = 30)]
	[Range(0, 1000000)]
	public decimal TpPoints
	{
		get => _tpPoints;
		set { _tpPoints = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Risk base", GroupName = RiskGroup, Order = 40)]
	public RiskBase RiskBaseMode
	{
		get => _riskBase;
		set { _riskBase = value; RecalculateValues(); }
	}

	// ------------------------------------------------------------------ 3. Bridge

	[OFT.Attributes.Parameter]
	[Display(Name = "Enabled (off = block new opens)", GroupName = BridgeGroup, Order = 10)]
	public bool Enabled
	{
		get => _enabled;
		set { _enabled = value; Log($"Enabled={value}"); RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Show status box", GroupName = BridgeGroup, Order = 20)]
	public bool ShowStatus
	{
		get => _showStatus;
		set { _showStatus = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Status box corner", GroupName = BridgeGroup, Order = 25)]
	public StatusCorner Corner
	{
		get => _corner;
		set { _corner = value; RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Advanced: service host", GroupName = BridgeGroup, Order = 30)]
	public string Host
	{
		get => _host;
		set { _host = string.IsNullOrWhiteSpace(value) ? "127.0.0.1" : value.Trim(); RestartClient(); RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Advanced: service port (0 = from pairing)", GroupName = BridgeGroup, Order = 40)]
	[Range(0, 65535)]
	public int Port
	{
		get => _port;
		set { _port = value; RestartClient(); RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Advanced: token (blank = from pairing)", GroupName = BridgeGroup, Order = 50)]
	public string Token
	{
		get => _token;
		set { _token = (value ?? "").Trim(); RestartClient(); RecalculateValues(); }
	}

	[OFT.Attributes.Parameter]
	[Display(Name = "Advanced: symbol alias (blank = instrument root)", GroupName = BridgeGroup, Order = 60)]
	public string SymbolAlias
	{
		get => _symbolAlias;
		set { _symbolAlias = (value ?? "").Trim(); RecalculateValues(); }
	}

	private bool Follow => _mode is ExecutionMode.FollowAtas or ExecutionMode.PercentFollow;
	private bool PercentFollow => _mode == ExecutionMode.PercentFollow;

	// ------------------------------------------------------------------ lifecycle

	protected override void OnInitialize()
	{
		Log($"OnInitialize v{ClientVersion}");
	}

	protected override void OnCalculate(int bar, decimal value)
	{
		// Nothing per bar: this indicator reacts to position and order events, not candles.
	}

	/// <summary>First real-time trade = chart finished loading. Arm here, never during history.</summary>
	protected override void OnNewTrade(MarketDataArg trade)
	{
		if (trade != null && trade.Price > 0)
			_chartPrice = trade.Price;
		if (_subscribed)
			return;
		var tm = TradingManager;
		if (tm == null)
			return;
		_tm = tm;
		_subscribed = true;
		Arm("first real-time trade");
		_followTimer ??= new Timer(_ => PumpFollow(), null, 100, 100);
	}

	protected override void OnDispose()
	{
		_subscribed = false;
		_followTimer?.Dispose();
		_followTimer = null;
		InstanceRegistry.Release(_registryKey, _generation);
		_client?.Dispose();
		_client = null;
		Log("OnDispose");
	}

	// ------------------------------------------------------------------ arming / identity

	private void Arm(string why)
	{
		var pos = _tm?.Position;
		_account = pos?.AccountID ?? _tm?.Portfolio?.AccountID ?? "";
		_security = pos?.SecurityId ?? _tm?.Security?.SecurityId ?? "";
		_registryKey = $"{_account}|{Alias()}";
		_generation = InstanceRegistry.Claim(_registryKey);
		_standby = false;
		_armTs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
		lock (_lock)
		{
			_follow = new FollowEngine(_security, () => DateTime.UtcNow);
			if (pos == null)
			{
				_lastVolume = 0m;
				_transitions.ArmProvisional();
				_follow.OnTransition(0, 0m);
			}
			else
			{
				_lastVolume = pos.Volume;
				_transitions.Arm(pos.Volume);
				var side = Math.Sign(pos.Volume);
				if (side != 0)
				{
					// ATAS restarted mid-trade: pick up the existing stop/target and keep following
					_follow.Resume(side, pos.AveragePrice, SnapActiveOrders());
				}
				else
				{
					_follow.OnTransition(0, 0m);
				}
			}
		}
		Log($"armed ({why}): position={(pos == null ? "null (provisional)" : pos.Volume.ToString(CultureInfo.InvariantCulture))} " +
		    $"dir={_transitions.Last} account={_account} security={_security} alias={Alias()} firm={_firm} mode={_mode} gen={_generation}");
		var client = EnsureClient();
		client.ArmTs = _armTs;
		client.SetIdentity(Alias(), _account, _security);
	}

	private bool IsLive()
	{
		if (!string.IsNullOrEmpty(_registryKey) && !InstanceRegistry.IsCurrent(_registryKey, _generation))
		{
			if (InstanceRegistry.TryReclaim(_registryKey, ref _generation))
			{
				Log("the newer V Bridge copy left; this copy takes the chart back");
				_standby = false;
				return true;
			}
			if (!_standby) Log("standby: a newer V Bridge copy took over this chart/account");
			_standby = true;
			return false;
		}
		return true;
	}

	protected override void OnPortfolioSelected(Portfolio portfolio)
	{
		base.OnPortfolioSelected(portfolio);
		if (_subscribed && !string.IsNullOrEmpty(portfolio?.AccountID))
			Arm("portfolio selected");
	}

	// ------------------------------------------------------------------ orders (Follow ATAS)

	private OrderSnap Snap(Order o) => new(
		o.Id ?? o.ExtId.ToString(CultureInfo.InvariantCulture),
		o.Type.ToString(),
		o.Direction == OrderDirections.Buy ? 1 : -1,
		o.Price, o.TriggerPrice, o.Comment ?? "",
		o.State == OrderStates.Active && !o.Canceled,
		o.SecurityId ?? "");

	private IEnumerable<OrderSnap> SnapActiveOrders()
	{
		var list = new List<OrderSnap>();
		try
		{
			foreach (var o in _tm?.Orders ?? Array.Empty<Order>())
				if (o != null && o.State == OrderStates.Active && OwnAccount(o.AccountID))
					list.Add(Snap(o));
		}
		catch (Exception ex) { Log("reading active orders failed: " + ex.Message); }
		return list;
	}

	private bool OwnAccount(string accountId)
	{
		var chartAccount = _tm?.Portfolio?.AccountID;
		return string.IsNullOrEmpty(chartAccount) || string.IsNullOrEmpty(accountId) || accountId == chartAccount;
	}

	protected override void OnNewOrder(Order order)
	{
		base.OnNewOrder(order);
		OnOrderEvent(order, "new");
	}

	protected override void OnOrderChanged(Order order)
	{
		base.OnOrderChanged(order);
		OnOrderEvent(order, "changed");
	}

	private void OnOrderEvent(Order order, string what)
	{
		if (order == null || !_subscribed || !OwnAccount(order.AccountID))
			return;
		var snap = Snap(order);
		if (snap.Kind == "Market")
			return;
		lock (_lock)
		{
			_follow.OnOrder(snap);
		}
		Log($"order {what}: {snap.Kind} {(snap.Side > 0 ? "buy" : "sell")} price={snap.Price} trigger={snap.Trigger} " +
		    $"comment={snap.Comment} active={snap.Active} -> stop={_follow.StopPrice} target={_follow.TargetPrice}");
		PumpFollow();
	}

	/// <summary>Runs the follow gate and sends its actions. Called on order events and every 100 ms.</summary>
	private void PumpFollow()
	{
		if (!Follow || !_subscribed || !IsLive())
			return;
		IReadOnlyList<FollowAction> actions;
		lock (_lock)
			actions = _follow.Poll();
		if (actions.Count == 0)
			return;
		var client = EnsureClient();
		foreach (var a in actions)
		{
			switch (a)
			{
				case FollowAction.Open open:
					_followNote = "";
					if (PercentFollow ? !FollowEngine.ServiceSupportsPercentFollow(_serviceVersion)
					                  : !FollowEngine.ServiceSupportsFollow(_serviceVersion))
					{
						lock (_lock) _follow.Abandon();
						_followNote = $"service {(_serviceVersion == "" ? "version unknown" : _serviceVersion)} is too old for "
						              + (PercentFollow ? "Percent Follow (needs 2.2+)" : "Follow ATAS (needs 2.1+)") + ": not mirrored";
						_lastAck = _followNote;
						_lastAckStatus = "error";
						Log(_followNote);
						break;
					}
					if (!_enabled)
					{
						Log($"follow open {open.Dir} suppressed: indicator disabled");
						_lastEvent = $"open {open.Dir} BLOCKED (off)";
						break;
					}
					var id = client.Send("open", open.Dir, Alias(), _account, _security, open.Offsets.AtasEntry, _lots,
					                     CurrentRisk(), open.Offsets);
					_lastEvent = $"open {open.Dir} {SizeLabel()} sl {open.Offsets.SlOffset:0.##} tp {Fmt(open.Offsets.TpOffset)} #{id}";
					if (id < 0)
					{
						lock (_lock) _follow.Abandon();      // never send moves for a trade MT5 never got
						_lastAck = "NOT SENT (service down): not mirrored";
						_lastAckStatus = "error";
					}
					else
						_pendingOpenId = id;
					Log($"follow open {open.Dir} {SizeLabel()} sl_offset={open.Offsets.SlOffset} tp_offset={open.Offsets.TpOffset} -> id {id}");
					break;
				case FollowAction.Modify mod:
					var mid = client.Send("modify", null, Alias(), _account, _security, 0m, 0m, CurrentRisk(), mod.Offsets);
					_lastEvent = $"modify sl {mod.Offsets.SlOffset:0.##} tp {Fmt(mod.Offsets.TpOffset)} #{mid}";
					Log($"follow modify sl_offset={mod.Offsets.SlOffset} tp_offset={mod.Offsets.TpOffset} -> id {mid}");
					break;
				case FollowAction.GiveUp give:
					_followNote = give.Reason;
					_lastAck = give.Reason;
					_lastAckStatus = "error";
					Log(give.Reason);
					break;
			}
		}
		RedrawChart();
	}

	private static string Fmt(decimal? v) => v.HasValue ? v.Value.ToString("0.##", CultureInfo.InvariantCulture) : "none";

	// ------------------------------------------------------------------ positions

	/// <summary>Base class relays TradingManager.PositionChanged here for EVERY position on the account.</summary>
	protected override void OnPositionChanged(Position p)
	{
		base.OnPositionChanged(p);
		if (p == null || !_subscribed)
			return;
		var current = _tm?.Security;
		if (current != null && !string.IsNullOrEmpty(p.SecurityId) && !string.IsNullOrEmpty(current.SecurityId)
		    && p.SecurityId != current.SecurityId)
			return; // another instrument on the same account
		if (!OwnAccount(p.AccountID))
		{
			Log($"ignored PositionChanged for other account {p.AccountID}");
			return;
		}
		IReadOnlyList<BridgeEvent> events;
		lock (_lock)
		{
			if (p.Volume == _lastVolume && _transitions.Armed && !_transitions.Provisional)
				return; // P&L tick on an unchanged position
			_lastVolume = p.Volume;
			events = _transitions.OnPosition(p.Volume);
			if (Follow && events.Count > 0)
			{
				// a real open/close forgets the previous trade's bracket (the new stop arrives ~0.15 s later);
				// a lone sync is the re-seed after an ATAS restart mid-trade: resume from the active orders
				_follow.OnPositionEvents(events, Math.Sign(p.Volume), p.AveragePrice, SnapActiveOrders);
			}
		}
		Log($"PositionChanged volume={p.Volume} avg={p.AveragePrice} account={p.AccountID} security={p.SecurityId} " +
		    $"-> {events.Count} event(s) mode={_mode}");
		if (events.Count == 0 || !IsLive())
			return;
		var client = EnsureClient();
		foreach (var ev in events)
		{
			if (ev.Type == "open" && Follow)
				continue;           // Follow mode opens from the gate once the ATAS stop is known
			if (!_enabled && ev.Type == "open")
			{
				Log($"open {ev.Dir} suppressed: indicator disabled (closes/syncs still flow)");
				_lastEvent = $"open {ev.Dir} BLOCKED (off)";
				continue;
			}
			var id = client.Send(ev.Type, ev.Dir, Alias(), p.AccountID ?? "", p.SecurityId ?? "", p.AveragePrice, p.Volume,
			                     CurrentRisk());
			_lastEvent = $"{ev.Type} {ev.Dir ?? ""} #{id}";
			if (id < 0)
			{
				_lastAck = "NOT SENT (service down)";
				_lastAckStatus = "error";
			}
			Log($"sent {ev.Type} {ev.Dir} -> id {id}");
		}
		PumpFollow();
		RedrawChart();
	}

	private RiskSettings CurrentRisk()
	{
		FollowOffsets offsets;
		lock (_lock)
			offsets = _follow.Current;
		return new RiskSettings(
			_firm.ToString(),
			string.IsNullOrEmpty(_symbolOverride) ? null : _symbolOverride,
			InstrumentInfo?.Instrument,
			_chartPrice > 0 ? _chartPrice : null,
			_riskPct, _stopPoints, _tpPoints,
			_riskBase == RiskBase.Balance ? "balance" : "equity",
			ClientVersion,
			PercentFollow ? "follow_pct" : Follow ? "follow" : "fixed",
			PercentFollow ? 0m : Follow ? _lots : null,
			Follow ? offsets : null);
	}

	private string SizeLabel() => PercentFollow
		? $"{_riskPct.ToString("0.##", CultureInfo.InvariantCulture)}% risk"
		: $"{_lots.ToString("0.##", CultureInfo.InvariantCulture)} lots";

	private string Alias()
	{
		if (!string.IsNullOrWhiteSpace(_symbolAlias))
			return _symbolAlias.Trim();
		var instrument = InstrumentInfo?.Instrument ?? "";
		var cut = 0;
		while (cut < instrument.Length && char.IsLetter(instrument[cut]))
			cut++;
		return cut > 0 ? instrument.Substring(0, cut) : instrument;
	}

	// ------------------------------------------------------------------ client

	private BridgeClient EnsureClient()
	{
		if (_client != null)
			return _client;
		var token = _token;
		var port = _port;
		if (string.IsNullOrEmpty(token) || port <= 0)
		{
			if (Pairing.TryReadAny(out var pairedToken, out var pairedPort, out var pairingDir))
			{
				if (string.IsNullOrEmpty(token)) token = pairedToken;
				if (port <= 0) port = pairedPort;
				_pairingNote = "";
				Log($"pairing read from {pairingDir}");
			}
			else
			{
				_pairingNote = "no pairing file: start the V Bridge service once";
				Log($"pairing read failed: {Pairing.LastError}");
				if (port <= 0) port = 5577;
			}
		}
		Log($"client: port {port}, token from {(string.IsNullOrEmpty(_token) ? "pairing file" : "settings")}" +
		    (string.IsNullOrEmpty(token) ? " (EMPTY)" : ""));
		_client = new BridgeClient(_host, port, token, SyncProvider, Log, null, CurrentRisk)
		{
			// a stale value typed into the Token setting must never strand the user: fall back to the pairing file
			TokenFallback = () => Pairing.TryReadAny(out var t, out _, out _) ? t : null
		};
		_client.AckReceived += OnAck;
		_client.Start();
		return _client;
	}

	private void RestartClient()
	{
		if (_client == null)
			return;
		_client.Dispose();
		_client = null;
		if (_subscribed)
			Arm("settings changed");
	}

	/// <summary>Runs on the client thread. Read-only: the ATAS thread is the only writer of Transitions.</summary>
	private BridgeEvent SyncProvider()
	{
		if (!IsLive())
			return null;
		lock (_lock)
		{
			if (!_transitions.Armed)
				return null;
			var pos = _tm?.Position;
			if (pos != null && Transitions.DirOf(pos.Volume) != _transitions.Last)
				Log($"sync: live position {pos.Volume} disagrees with tracked {_transitions.Last}; reporting tracked, awaiting PositionChanged");
			return _transitions.Sync();
		}
	}

	private void OnAck(AckInfo ack)
	{
		if (ack.Standby != _serviceStandby)
			Log(ack.Standby ? "standby: the service gives this chart/account to a newer V Bridge copy"
			                : "the service accepts this copy again");
		_serviceStandby = ack.Standby;
		if (ack.Id == _pendingOpenId)
		{
			_pendingOpenId = -1;
			if (ack.Status == "error")
			{
				lock (_lock) _follow.Abandon();          // refused open: stop moves would look mirrored
				_followNote = "open refused on MT5: not mirrored";
			}
		}
		_lastAck = $"#{ack.Id} {ack.Summary}";
		_lastAckStatus = ack.Status;
		if (ack.Preview != null)
			_preview = ack.Preview;
		if (!string.IsNullOrEmpty(ack.ServiceVersion))
			_serviceVersion = ack.ServiceVersion;
		Log($"ack #{ack.Id} {ack.Status}: {ack.Summary}" + (ack.Preview != null ? $" | preview {ack.Preview.Symbol} lots {ack.Preview.Lots} {ack.Preview.Note}" : ""));
		RedrawChart();
	}

	// ------------------------------------------------------------------ status box

	private bool VersionMismatch()
	{
		if (string.IsNullOrEmpty(_serviceVersion)) return false;
		return ClientVersion.Split('.')[0] != _serviceVersion.Split('.')[0];
	}

	protected override void OnRender(RenderContext context, DrawingLayouts layout)
	{
		if (!_showStatus)
			return;
		string line1, line2, note;
		Color back;
		if (_standby || _serviceStandby)
		{
			line1 = $"V Bridge {ClientVersion} | standby: another V Bridge copy is handling this chart";
			line2 = "remove this copy or keep it; it sends nothing";
			note = "";
			back = Color.FromArgb(200, 90, 90, 90);
		}
		else
		{
			var connected = _client?.Connected == true;
			var state = _tm == null && !_subscribed ? "no trading account on chart"
				: !_subscribed ? "waiting for live data" : connected ? "connected" : "DISCONNECTED";
			var mode = _enabled ? "" : " | OFF: opens blocked, closes still mirrored";
			var symbol = _preview?.Symbol != null
				? $"{_preview.Symbol} ({(_preview.Verified ? "verified" : _preview.Source)})"
				: (string.IsNullOrEmpty(_symbolOverride) ? "symbol: pending" : $"override {_symbolOverride}");
			string risk;
			if (Follow)
			{
				var cur = _follow.Current;
				risk = (PercentFollow ? $"Percent Follow {SizeLabel()}" : $"Follow ATAS {SizeLabel()}")
				       + (cur != null ? $" | sl {Fmt(cur.SlOffset)} tp {Fmt(cur.TpOffset)}" : "")
				       + (PercentFollow && cur != null && _preview?.Lots != null
					       ? $" -> {_preview.Lots.Value.ToString("0.##", CultureInfo.InvariantCulture)} lots" : "");
			}
			else
			{
				risk = $"{_riskPct.ToString("0.##", CultureInfo.InvariantCulture)}% / {_stopPoints.ToString("0.##", CultureInfo.InvariantCulture)} pts"
				       + (_preview?.Lots != null ? $" -> {_preview.Lots.Value.ToString("0.##", CultureInfo.InvariantCulture)} lots" : "");
			}
			line1 = $"V Bridge {ClientVersion} | {state}{mode} | {_firm} {symbol} | {risk} | pos {_lastVolume} ({_transitions.Last})";
			line2 = $"last: {_lastEvent} | ack: {_lastAck}";
			note = _preview?.Note ?? "";
			if (Follow && _follow.StopMissing) note = "ATAS stop removed: MT5 keeps its last stop. " + note;
			if (Follow && !string.IsNullOrEmpty(_followNote)) note = _followNote + ". " + note;
			if (VersionMismatch()) note = $"indicator {ClientVersion} / service {_serviceVersion} mismatch: update both. " + note;
			if (!string.IsNullOrEmpty(_pairingNote)) note = _pairingNote + ". " + note;
			back = !connected || _lastAckStatus == "error" ? Color.FromArgb(210, 160, 30, 30)
				: !_enabled ? Color.FromArgb(200, 90, 90, 90)
				: _lastAckStatus == "warn" || !string.IsNullOrEmpty(note) || (_preview != null && !_preview.Verified)
					? Color.FromArgb(210, 190, 120, 20)
					: Color.FromArgb(210, 25, 120, 60);
		}

		var s1 = context.MeasureString(line1, StatusFont);
		var s2 = context.MeasureString(line2, StatusFont);
		var s3 = string.IsNullOrEmpty(note) ? new Size(0, 0) : context.MeasureString(note, StatusFont);
		var width = Math.Max(s1.Width, Math.Max(s2.Width, s3.Width)) + 16;
		var height = s1.Height + s2.Height + (s3.Height > 0 ? s3.Height : 0) + 12;
		var region = Container?.Region ?? new Rectangle(0, 0, width + 16, height + 16);
		const int margin = 8;
		var x = _corner is StatusCorner.TopRight or StatusCorner.BottomRight
			? Math.Max(region.Left + margin, region.Right - width - margin)
			: region.Left + margin;
		var y = _corner is StatusCorner.BottomLeft or StatusCorner.BottomRight
			? Math.Max(region.Top + margin, region.Bottom - height - margin)
			: region.Top + margin;
		var rect = new Rectangle(x, y, width, height);
		context.FillRectangle(back, rect, 4);
		var ty = rect.Top + 4;
		context.DrawString(line1, StatusFont, Color.White, rect.Left + 8, ty);
		ty += s1.Height;
		context.DrawString(line2, StatusFont, Color.White, rect.Left + 8, ty);
		if (s3.Height > 0)
		{
			ty += s2.Height;
			context.DrawString(note, StatusFont, Color.FromArgb(255, 255, 230, 150), rect.Left + 8, ty);
		}
	}

	// ------------------------------------------------------------------ log

	private static void Log(string line)
	{
		try
		{
			lock (LogLock)
			{
				Directory.CreateDirectory(Path.GetDirectoryName(LogPath) ?? ".");
				File.AppendAllText(LogPath,
					DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture) + " " + line + Environment.NewLine);
			}
		}
		catch
		{
			// never let logging break the chart
		}
	}
}
