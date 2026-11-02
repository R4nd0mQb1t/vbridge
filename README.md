# V Bridge

Trade on the futures chart in **ATAS X**, execute on your CFD prop-firm account in **MetaTrader 5**.

V Bridge mirrors the *direction* of the position on an ATAS chart to one or more MT5 accounts. You set
the risk per trade, the stop and the target on each chart; the bridge sizes the CFD from your account
balance, opens it with the stop and target attached, and closes it when your ATAS position goes flat.
ATAS is the master. Adds, partial closes and stop moves are not copied.

> **Notice.** V Bridge sends your ATAS trades to your MT5 account. Some prop firms restrict copiers or
> external software. Check your firm's rules; using V Bridge is your decision.

## Download

Get the latest installer from **[Releases](https://github.com/R4nd0mQb1t/vbridge/releases/latest)**: run
`VBridge-Setup-<version>.exe` and follow "Install (for users)" below.

## Install (for users)

1. Have **ATAS X** installed, and **MetaTrader 5** running and logged in to your prop-firm account with
   the **Algo Trading** toolbar button ON (green).
2. Run `VBridge-Setup-<version>.exe`. Windows SmartScreen may warn because the installer is not code-signed:
   click **More info -> Run anyway**. No administrator rights are needed.
3. A **V Bridge** icon appears in the system tray. Open **Accounts** and **tick your MT5 account**. New
   accounts are always off until you tick them.
4. In ATAS X, add the **V Bridge** indicator to the chart you trade from. In its settings choose your
   **Prop firm**, the **Risk %** per trade, the **Stop** and **Target** in points. Leave Token blank; pairing
   is automatic on this PC.
5. The status box on the chart turns **green** and shows the broker symbol it resolved, for example
   `FundingPips NDX100 (verified) | 1% / 20 pts -> 1.25 lots`. Trade with Chart Trader as usual.

Points are the chart's own price units: 20 on NQ is 20 index points, 2 on gold is 2 dollars, 0.5 on oil is
50 cents.

### Status box colours

| colour | meaning |
|---|---|
| green | connected, last action ok |
| amber | something to look at: symbol unverified (confirm it in the tray), `already mirrored`, lots capped at the broker maximum |
| red | service down, account not attached (Algo Trading off?), or the last action failed; the reason is in the box |
| grey | indicator disabled: new opens blocked, closes still mirrored |

### Execution modes (V Bridge settings, group "2. Risk")

- **Fixed risk** (default): the bridge sizes the CFD from *Risk %* and uses its own *Stop* and *Target*
  distances in points.
- **Follow ATAS**: set your stop and target in ATAS Chart Trader before you click. MT5 opens the
  **Lots** you set, with the same stop and target distances from your fill, and follows when you move
  either (breakeven, trail, drag). If ATAS has no stop within 1.5 s of the fill, nothing is opened.
  Removing the ATAS stop keeps the last MT5 stop. Safety net: a lot size that would risk more than 5% of
  the account at your stop is refused.

### Switching MT5 accounts

Log the terminal into another account, then tray -> **Refresh accounts now** (or wait up to 30 s). New
accounts appear under **Accounts** unticked; tick to trade. If an account shows "the broker does not allow
automated trading on this account", the prop firm has disabled Expert Advisors for it: the bridge cannot
trade there.

### Verified symbol names

FundingPips (NDX100, SPX500, XAUUSD, USOIL), FundedNext (NDX100, SPX500, XAUUSD, USOUSD), FTMO NQ
(US100.cash). Others are found automatically and confirmed once under **Confirm symbols** in the tray.

### Prop firms

The dropdown knows FundingPips (verified), FTMO, The5ers, FundedNext, Goat Funded, Trading Pit, and
**Other**. Where the symbol name is not verified, V Bridge finds the best match on your broker's symbol
list and asks you once, in the tray, to confirm it before the first trade. If it picks wrong, type the
right name into **Broker symbol override** on the chart. The firm is also recognised from your MT5 server
name, so two accounts at two firms each trade their own symbol from one click.

### Tray menu

Accounts (tick to enable, shows login, server, firm and balance), **Kill switch** (blocks new opens;
closes still go through), Open logs folder, Export diagnostics (a zip to send when asking for help; it
never contains the token), Check for updates, Quit.

### Safety rules built in

- Never opens a CFD without a stop. Never rounds the lot size up. Below the broker minimum = no trade.
- Risk above 5% per trade is refused. A stop wider than 5% of price is refused. A chart whose price is
  more than 3% away from the broker's quote is blocked (wrong symbol).
- MT5 is the source of truth: the bridge only ever closes positions it opened (its magic number).
- If the bridge was down when you clicked, the trade is **not** opened late at a worse price; the box
  turns red so you know.

### Uninstall

Windows Settings -> Apps -> V Bridge. This removes the service, the startup entry and the indicator DLL.

## For developers

```
service/      Python 3.11 service: pytest (214 tests)      cd service && python -m pytest
indicator/    C# ATAS X indicator (.NET 10, no WPF)        cd indicator/VBridge.Tests && dotnet test
packaging/    PyInstaller spec, Inno Setup script, build.ps1
docs/         design specs, plans and live-check notes
```

Build everything: `powershell -ExecutionPolicy Bypass -File packaging\build.ps1` -> `dist\VBridge-Setup-<version>.exe`.
Requires the .NET 10 SDK, Python 3.11 with `pip install -r service/requirements.txt pyinstaller`, and Inno
Setup 6 (`winget install JRSoftware.InnoSetup`). Deploy the indicator alone with
`dotnet build indicator/VBridge.csproj -c Release -t:Deploy` (ATAS X hot-reloads it).

Run the service from source: `cd service && python -m bridge` (tray) or `--no-tray`; `--enable LOGIN`
enables an account without the tray (VPS use); `--config config.yaml` is the explicit phase-1 mode.
Files live in `%LocalAppData%\VBridge` (settings.json, audit.jsonl, logs\, pairing.json) and the pairing
file is mirrored to `%AppData%\ATAS X\VBridge` because ATAS X cannot read `%LocalAppData%` from an
indicator. Test without ATAS: `python service/tools/send_event.py --token <t> open buy`.

Releases: publish `dist\VBridge-Setup-<version>.exe` and `packaging\latest.json` at the URL in
`service/bridge/update.py` (`UPDATE_URL`); bump `service/bridge/version.py` and the `<Version>` in
`indicator/VBridge.csproj` together.

## Support

Questions and bug reports: Telegram [t.me/R4ND0MQB1T](https://t.me/R4ND0MQB1T), or open an issue on GitHub.
Attach the zip from tray -> **Export diagnostics** (it never contains your token).

## License

Copyright (C) 2026 Q-BIT. V Bridge is free software under the **GNU General Public License v3.0**
(see [LICENSE](LICENSE)). It comes with no warranty: you are responsible for how you trade with it and for
your prop firm's rules.
