import 'dart:async';
import 'dart:convert';
// ignore: avoid_web_libraries_in_flutter
import 'dart:html' as html;
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:url_launcher/url_launcher.dart';

void main() {
  runApp(const TradeCoreApp());
}

// ──────────────────────────────────────────────────────────────────────────────
// RUNTIME CONFIGURATION — stored in browser localStorage
// No --dart-define flags needed. Settings persist across sessions.
// First launch shows a settings modal automatically.
// ──────────────────────────────────────────────────────────────────────────────
const String _kUrlKey = 'tc_base_url';
const String _kApiKeyKey = 'tc_api_key';
const String _kDefaultUrl = 'http://127.0.0.1:8000';
const String _kDefaultKey = 'dev-paper';

String _storedUrl() => html.window.localStorage[_kUrlKey] ?? _kDefaultUrl;
String _storedKey() => html.window.localStorage[_kApiKeyKey] ?? _kDefaultKey;
void _saveSettings(String url, String key) {
  html.window.localStorage[_kUrlKey] = url.trim();
  html.window.localStorage[_kApiKeyKey] = key.trim();
}

Map<String, String> _authHeaders() => {'X-API-Key': _storedKey()};

// ──────────────────────────────────────────────────────────────────────────────

class TradeCoreApp extends StatelessWidget {
  const TradeCoreApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TradeCore v51.0',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF0A0E14),
        cardColor: const Color(0xFF161B22),
        primaryColor: const Color(0xFF00C853),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFF00C853),
          secondary: Color(0xFF2962FF),
          surface: Color(0xFF161B22),
        ),
      ),
      home: const MainScreen(),
    );
  }
}

class MainScreen extends StatefulWidget {
  const MainScreen({super.key});
  @override
  State<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends State<MainScreen> {
  int _currentIndex = 1;
  Timer? _statusTimer;
  Timer? _perfTimer;

  // Live MT5 State
  bool isBackendOnline = false;
  double balance = 0.0;
  double equity = 0.0;
  double marginLevel = 0.0;
  double freeMargin = 0.0;
  double totalPnl = 0.0;
  bool killSwitch = false;
  List<dynamic> activePositions = [];
  List<dynamic> newsData = [];
  String marketRegime = 'CALIBRATING...';
  double dailyVaR = 0.0;

  // Performance State
  double totalRealized = 0.0;
  double monthlyRealized = 0.0;
  double winRate = 0.0;
  double profitFactor = 0.0;
  int totalTrades = 0;
  List<FlSpot> equitySpots = [];
  List<String> equityDates = [];

  bool _authFailed = false; // shows orange banner + prompts settings

  final NumberFormat usdFmt = NumberFormat.currency(
    symbol: '\$ ',
    decimalDigits: 2,
  );
  final NumberFormat compactFmt = NumberFormat.compactCurrency(
    symbol: '\$',
    decimalDigits: 2,
  );

  // Calculator
  final _calcBal = TextEditingController();
  final _calcRisk = TextEditingController(text: '1.0');
  final _calcSl = TextEditingController(text: '20');
  String _calcLots = '0.00';
  String _calcExposure = '\$0.00';
  String _calcAsset = 'FX';

  static const List<String> _assets = ['FX', 'Gold', 'BTC/ETH', 'Indices'];
  static const Map<String, String> _slLabel = {
    'FX': 'SL (pips)',
    'Gold': 'SL (\$/oz)',
    'BTC/ETH': 'SL (\$)',
    'Indices': 'SL (pts)',
  };
  static const Map<String, double> _lotVal = {
    'FX': 10.0,
    'Gold': 100.0,
    'BTC/ETH': 1.0,
    'Indices': 1.0,
  };
  static const Map<String, double> _minLot = {
    'FX': 0.01,
    'Gold': 0.01,
    'BTC/ETH': 0.01,
    'Indices': 0.10,
  };
  static const Map<String, String> _defSl = {
    'FX': '20',
    'Gold': '5',
    'BTC/ETH': '500',
    'Indices': '50',
  };

  // Settings
  final _settingsUrl = TextEditingController();
  final _settingsKey = TextEditingController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_storedKey() == _kDefaultKey) _openSettings(firstRun: true);
    });
    _fetchStatus();
    _fetchPerformance();
    _fetchNews();
    _statusTimer = Timer.periodic(const Duration(seconds: 3), (_) {
      _fetchStatus();
      if (_currentIndex == 2) _fetchNews();
    });
    _perfTimer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (_currentIndex == 1) _fetchPerformance();
    });
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _perfTimer?.cancel();
    _calcBal.dispose();
    _calcRisk.dispose();
    _calcSl.dispose();
    _settingsUrl.dispose();
    _settingsKey.dispose();
    super.dispose();
  }

  // ── SETTINGS ─────────────────────────────────────────────────────────────

  void _openSettings({bool firstRun = false}) {
    _settingsUrl.text = _storedUrl();
    _settingsKey.text = _storedKey();
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF161B22),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => Padding(
        padding: EdgeInsets.only(
          left: 24,
          right: 24,
          top: 24,
          bottom: MediaQuery.of(context).viewInsets.bottom + 32,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.settings, color: Color(0xFF00C853)),
                const SizedBox(width: 8),
                Text(
                  firstRun ? 'First-Run Configuration' : 'Connection Settings',
                  style: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ],
            ),
            if (firstRun) ...[
              const SizedBox(height: 8),
              const Text(
                'Enter the server URL and API key (TRADECORE_API_KEY env var).\n'
                'Saved in your browser — never transmitted anywhere else.',
                style: TextStyle(color: Colors.white54, fontSize: 13),
              ),
            ],
            const SizedBox(height: 20),
            TextField(
              controller: _settingsUrl,
              decoration: const InputDecoration(
                labelText: 'Server URL',
                hintText: 'http://127.0.0.1:8000',
                prefixIcon: Icon(Icons.link),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _settingsKey,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'API Key',
                hintText: 'Value of TRADECORE_API_KEY',
                prefixIcon: Icon(Icons.key),
              ),
            ),
            const SizedBox(height: 20),
            SizedBox(
              width: double.infinity,
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF00C853),
                  foregroundColor: Colors.black,
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                onPressed: () {
                  final u = _settingsUrl.text.trim();
                  final k = _settingsKey.text.trim();
                  if (u.isEmpty || k.isEmpty) return;
                  _saveSettings(u, k);
                  setState(() => _authFailed = false);
                  Navigator.pop(context);
                  _fetchStatus();
                  _fetchPerformance();
                  _fetchNews();
                },
                child: const Text(
                  'Save & Reconnect',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
              ),
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }

  // ── FETCH ─────────────────────────────────────────────────────────────────

  Future<void> _fetchStatus() async {
    try {
      final r = await http
          .get(Uri.parse('${_storedUrl()}/bot/status'), headers: _authHeaders())
          .timeout(const Duration(seconds: 3));
      if (r.statusCode == 200) {
        final d = json.decode(r.body);
        setState(() {
          _authFailed = false;
          isBackendOnline = d['is_running'] ?? false;
          balance = (d['account']?['balance'] ?? 0).toDouble();
          equity = (d['account']?['equity'] ?? 0).toDouble();
          marginLevel = (d['account']?['margin_level'] ?? 0).toDouble();
          freeMargin = (d['account']?['free_margin'] ?? 0).toDouble();
          totalPnl = (d['total_pnl'] ?? 0).toDouble();
          activePositions = d['positions'] ?? [];
          marketRegime = d['market_regime'] ?? 'CALIBRATING...';
          dailyVaR = (d['daily_var'] ?? 0).toDouble();
          killSwitch = d['kill_switch'] ?? false;
          if (_calcBal.text.isEmpty && balance > 0) {
            _calcBal.text = balance.toStringAsFixed(2);
            _calcSize();
          }
        });
      } else if (r.statusCode == 403) {
        setState(() {
          isBackendOnline = false;
          _authFailed = true;
        });
      } else {
        setState(() => isBackendOnline = false);
      }
    } catch (_) {
      setState(() => isBackendOnline = false);
    }
  }

  Future<void> _fetchPerformance() async {
    try {
      final r = await http
          .get(
            Uri.parse('${_storedUrl()}/bot/performance'),
            headers: _authHeaders(),
          )
          .timeout(const Duration(seconds: 5));
      if (r.statusCode != 200) return;
      final d = json.decode(r.body);
      setState(() {
        totalRealized = (d['total_realized'] ?? 0).toDouble();
        monthlyRealized = (d['monthly_realized'] ?? 0).toDouble();
        winRate = (d['win_rate'] ?? 0).toDouble();
        profitFactor = (d['profit_factor'] ?? 0).toDouble();
        totalTrades = d['total_trades'] ?? 0;
        final List curve = d['curve'] ?? [];
        equitySpots.clear();
        equityDates.clear();
        for (int i = 0; i < curve.length; i++) {
          equitySpots.add(
            FlSpot(i.toDouble(), (curve[i]['profit'] ?? 0).toDouble()),
          );
          equityDates.add(curve[i]['date'] ?? '');
        }
      });
    } catch (_) {}
  }

  Future<void> _fetchNews() async {
    try {
      final r = await http
          .get(Uri.parse('${_storedUrl()}/bot/news'), headers: _authHeaders())
          .timeout(const Duration(seconds: 5));
      if (r.statusCode == 200) setState(() => newsData = json.decode(r.body));
    } catch (_) {}
  }

  Future<void> _fetchData() =>
      Future.wait([_fetchStatus(), _fetchPerformance(), _fetchNews()]);

  Future<void> _downloadCsv() async {
    final uri = Uri.parse(
      '${_storedUrl()}/quant/export_report?key=${_storedKey()}',
    );
    if (!await launchUrl(uri) && mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Could not open CSV.')));
    }
  }

  // ── CALCULATOR ───────────────────────────────────────────────────────────

  void _calcSize() {
    final bal = double.tryParse(_calcBal.text) ?? 0.0;
    final risk = double.tryParse(_calcRisk.text) ?? 1.0;
    final sl = double.tryParse(_calcSl.text) ?? 0.0;
    if (bal <= 0 || sl <= 0) return;
    final lotV = _lotVal[_calcAsset] ?? 10.0;
    final minL = _minLot[_calcAsset] ?? 0.01;
    final capLot = sl * lotV;
    final raw = (bal * risk / 100) / capLot;
    final lots = raw < minL ? minL : raw;
    setState(() {
      _calcLots = lots.toStringAsFixed(2);
      _calcExposure = compactFmt.format(lots * capLot);
    });
  }

  // ── BUILD ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'TradeCore v51.0',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        backgroundColor: const Color(0xFF161B22),
        elevation: 0,
        actions: [
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
            onPressed: _openSettings,
          ),
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: GestureDetector(
              onTap: _authFailed ? _openSettings : null,
              child: Row(
                children: [
                  Icon(
                    Icons.circle,
                    size: 12,
                    color: _authFailed
                        ? Colors.orange
                        : isBackendOnline
                        ? const Color(0xFF00C853)
                        : Colors.redAccent,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    _authFailed
                        ? 'AUTH ERR'
                        : isBackendOnline
                        ? 'ONLINE'
                        : 'OFFLINE',
                    style: const TextStyle(
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          // 403 banner
          if (_authFailed)
            MaterialBanner(
              backgroundColor: Colors.orange.withOpacity(0.12),
              leading: const Icon(Icons.key_off, color: Colors.orange),
              content: const Text(
                '403 Forbidden — API key mismatch. Tap Settings to fix.',
                style: TextStyle(color: Colors.orange),
              ),
              actions: [
                TextButton(
                  onPressed: _openSettings,
                  child: const Text(
                    'Fix Now',
                    style: TextStyle(color: Colors.orange),
                  ),
                ),
              ],
            ),
          // Kill switch banner
          if (killSwitch)
            MaterialBanner(
              backgroundColor: Colors.redAccent.withOpacity(0.12),
              leading: const Icon(Icons.lock_outline, color: Colors.redAccent),
              content: const Text(
                'Kill Switch Active — VaR limit breached. 8-hour lockout in effect.',
                style: TextStyle(color: Colors.redAccent),
              ),
              actions: [TextButton(onPressed: () {}, child: const Text(''))],
            ),
          Expanded(
            child: [
              _buildTerminal(),
              _buildQuant(),
              _buildNews(),
            ][_currentIndex],
          ),
        ],
      ),
      bottomNavigationBar: BottomNavigationBar(
        backgroundColor: const Color(0xFF161B22),
        selectedItemColor: const Color(0xFF00C853),
        unselectedItemColor: Colors.grey,
        currentIndex: _currentIndex,
        onTap: (i) {
          setState(() => _currentIndex = i);
          if (i == 1) _fetchPerformance();
          if (i == 2) _fetchNews();
        },
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.monitor_heart),
            label: 'Terminal',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.analytics),
            label: 'Quant Dash',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.public),
            label: 'News Guard',
          ),
        ],
      ),
    );
  }

  // ── TERMINAL ──────────────────────────────────────────────────────────────

  Widget _buildTerminal() => RefreshIndicator(
    onRefresh: _fetchData,
    child: ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _card(
          child: Column(
            children: [
              const Text(
                'LIVE FLOATING PnL',
                style: TextStyle(color: Colors.grey, letterSpacing: 1.2),
              ),
              const SizedBox(height: 16),
              Text(
                usdFmt.format(totalPnl),
                style: TextStyle(
                  fontSize: 40,
                  fontWeight: FontWeight.bold,
                  color: totalPnl >= 0
                      ? const Color(0xFF00C853)
                      : Colors.redAccent,
                ),
              ),
              const SizedBox(height: 16),
              const Divider(color: Colors.white24),
              const SizedBox(height: 16),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  _sc('Balance', usdFmt.format(balance)),
                  _sc('Equity', usdFmt.format(equity)),
                ],
              ),
              const SizedBox(height: 16),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  _sc('Free Margin', usdFmt.format(freeMargin)),
                  _sc('Margin Level', '${marginLevel.toStringAsFixed(1)}%'),
                ],
              ),
            ],
          ),
        ),
        const SizedBox(height: 24),
        const Text(
          'ACTIVE TRADES',
          style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
        ),
        const SizedBox(height: 12),
        if (activePositions.isEmpty)
          const Center(
            child: Padding(
              padding: EdgeInsets.all(32),
              child: Text(
                'Scanning markets...',
                style: TextStyle(color: Colors.white54),
              ),
            ),
          )
        else
          ...activePositions.map((p) {
            final bool buy = p['type'] == 'BUY';
            final double pnl = (p['profit'] ?? 0).toDouble();
            return Card(
              margin: const EdgeInsets.only(bottom: 8),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
              child: ListTile(
                leading: CircleAvatar(
                  backgroundColor: buy
                      ? const Color(0xFF00C853).withOpacity(0.2)
                      : Colors.redAccent.withOpacity(0.2),
                  child: Icon(
                    buy ? Icons.arrow_upward : Icons.arrow_downward,
                    color: buy ? const Color(0xFF00C853) : Colors.redAccent,
                  ),
                ),
                title: Text(
                  '${p['symbol']}  •  ${p['volume']} Lots',
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: Text('Open: ${p['open_price']}  |  SL: ${p['sl']}'),
                trailing: Text(
                  usdFmt.format(pnl),
                  style: TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 16,
                    color: pnl >= 0
                        ? const Color(0xFF00C853)
                        : Colors.redAccent,
                  ),
                ),
              ),
            );
          }),
      ],
    ),
  );

  // ── QUANT DASHBOARD ───────────────────────────────────────────────────────

  Widget _buildQuant() {
    final tgt = balance > 0 ? balance * 0.05 : 500.0;
    final prg = tgt > 0 ? (monthlyRealized / tgt).clamp(0.0, 1.0) : 0.0;
    return RefreshIndicator(
      onRefresh: _fetchData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'DYNAMIC MONTHLY TARGET (5%)',
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          _card(
            child: Column(
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'Achieved',
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFmt.format(monthlyRealized),
                          style: TextStyle(
                            fontSize: 24,
                            fontWeight: FontWeight.bold,
                            color: monthlyRealized >= 0
                                ? const Color(0xFF00C853)
                                : Colors.redAccent,
                          ),
                        ),
                      ],
                    ),
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        const Text(
                          'Target',
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFmt.format(tgt),
                          style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                LinearProgressIndicator(
                  value: prg,
                  backgroundColor: Colors.white10,
                  color: const Color(0xFF2962FF),
                  minHeight: 6,
                  borderRadius: BorderRadius.circular(4),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          const Text(
            'ALGORITHMIC AUDIT',
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: _mt(
                  'Win Rate',
                  '${winRate.toStringAsFixed(1)}%',
                  color: winRate > 50 ? const Color(0xFF00C853) : Colors.orange,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _mt(
                  'Profit Factor',
                  profitFactor.toStringAsFixed(2),
                  color: profitFactor >= 1.5
                      ? const Color(0xFF2962FF)
                      : Colors.white,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(child: _mt('Executions', totalTrades.toString())),
            ],
          ),
          const SizedBox(height: 24),
          const Text(
            'INSTITUTIONAL RISK',
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          _card(
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                _sc('GARCH Regime', marketRegime),
                _sc(
                  'Daily VaR',
                  dailyVaR == 0 ? '...' : usdFmt.format(dailyVaR),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          const Text(
            'EQUITY CURVE',
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Container(
            height: 200,
            padding: const EdgeInsets.only(
              right: 20,
              left: 10,
              top: 24,
              bottom: 10,
            ),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: Colors.white10),
            ),
            child: equitySpots.length < 2
                ? const Center(
                    child: Text(
                      'Awaiting trade data...',
                      style: TextStyle(color: Colors.white54),
                    ),
                  )
                : LineChart(
                    LineChartData(
                      gridData: FlGridData(
                        show: true,
                        drawVerticalLine: false,
                        getDrawingHorizontalLine: (_) =>
                            const FlLine(color: Colors.white10, strokeWidth: 1),
                      ),
                      titlesData: FlTitlesData(
                        rightTitles: const AxisTitles(
                          sideTitles: SideTitles(showTitles: false),
                        ),
                        topTitles: const AxisTitles(
                          sideTitles: SideTitles(showTitles: false),
                        ),
                        bottomTitles: const AxisTitles(
                          sideTitles: SideTitles(showTitles: false),
                        ),
                        leftTitles: AxisTitles(
                          sideTitles: SideTitles(
                            showTitles: true,
                            reservedSize: 40,
                            getTitlesWidget: (v, _) => Text(
                              '\$${v.toInt()}',
                              style: const TextStyle(
                                color: Colors.grey,
                                fontSize: 9,
                              ),
                            ),
                          ),
                        ),
                      ),
                      borderData: FlBorderData(show: false),
                      minX: 0,
                      maxX: (equitySpots.length - 1).toDouble(),
                      lineBarsData: [
                        LineChartBarData(
                          spots: equitySpots,
                          isCurved: true,
                          color: const Color(0xFF2962FF),
                          barWidth: 2,
                          dotData: const FlDotData(show: false),
                          belowBarData: BarAreaData(
                            show: true,
                            color: const Color(0xFF2962FF).withOpacity(0.15),
                          ),
                        ),
                      ],
                    ),
                  ),
          ),
          const SizedBox(height: 24),
          const Text(
            'RISK CALCULATOR & AUDIT',
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          _card(
            child: Column(
              children: [
                Row(
                  children: _assets.map((a) {
                    final sel = a == _calcAsset;
                    return Expanded(
                      child: GestureDetector(
                        onTap: () {
                          setState(() {
                            _calcAsset = a;
                            _calcSl.text = _defSl[a] ?? '20';
                          });
                          _calcSize();
                        },
                        child: Container(
                          margin: const EdgeInsets.only(right: 4),
                          padding: const EdgeInsets.symmetric(vertical: 6),
                          decoration: BoxDecoration(
                            color: sel
                                ? const Color(0xFF2962FF)
                                : Colors.white10,
                            borderRadius: BorderRadius.circular(6),
                          ),
                          child: Text(
                            a,
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: 11,
                              fontWeight: FontWeight.bold,
                              color: sel ? Colors.white : Colors.grey,
                            ),
                          ),
                        ),
                      ),
                    );
                  }).toList(),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _calcBal,
                        decoration: const InputDecoration(
                          labelText: 'Bal (\$)',
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calcSize(),
                        style: const TextStyle(fontSize: 14),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: _calcRisk,
                        decoration: const InputDecoration(
                          labelText: 'Risk (%)',
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calcSize(),
                        style: const TextStyle(fontSize: 14),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: _calcSl,
                        decoration: InputDecoration(
                          labelText: _slLabel[_calcAsset] ?? 'SL',
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calcSize(),
                        style: const TextStyle(fontSize: 14),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      'Vol: $_calcLots',
                      style: const TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Color(0xFF2962FF),
                      ),
                    ),
                    Text(
                      'Risk: $_calcExposure',
                      style: const TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Colors.redAccent,
                      ),
                    ),
                    TextButton.icon(
                      onPressed: _downloadCsv,
                      icon: const Icon(Icons.download, size: 16),
                      label: const Text('CSV'),
                      style: TextButton.styleFrom(
                        visualDensity: VisualDensity.compact,
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ── NEWS GUARD ────────────────────────────────────────────────────────────

  Widget _buildNews() {
    if (newsData.isEmpty) {
      return const Center(
        child: Text(
          "No High Impact News Found.",
          style: TextStyle(color: Colors.grey),
        ),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: newsData.length,
      itemBuilder: (context, index) {
        final ev = newsData[index];
        final isHigh = ev['impact'] == 'High';

        return Padding(
          padding: const EdgeInsets.only(bottom: 12.0),
          child: _card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      isHigh ? Icons.warning : Icons.info_outline,
                      color: isHigh ? Colors.redAccent : Colors.orangeAccent,
                      size: 20,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        "${ev['country']} • ${ev['time']}",
                        style: const TextStyle(
                          fontWeight: FontWeight.bold,
                          color: Colors.grey,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  ev['title'],
                  style: const TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 8),
                Container(
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: Colors.cyanAccent.withOpacity(0.1),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(top: 2.0),
                        child: Icon(
                          Icons.lightbulb_outline,
                          size: 16,
                          color: Colors.cyanAccent,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          ev['insight'] ?? 'Volatility expected.',
                          style: const TextStyle(
                            color: Colors.cyanAccent,
                            fontStyle: FontStyle.italic,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  // ── SHARED HELPERS ────────────────────────────────────────────────────────

  Widget _card({required Widget child}) => Container(
    padding: const EdgeInsets.all(20),
    decoration: BoxDecoration(
      color: const Color(0xFF161B22),
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: Colors.white10),
    ),
    child: child,
  );

  Widget _sc(String label, String value) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(label, style: const TextStyle(color: Colors.grey, fontSize: 12)),
      Text(
        value,
        style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
      ),
    ],
  );

  Widget _mt(String label, String value, {Color color = Colors.white}) =>
      Container(
        padding: const EdgeInsets.symmetric(vertical: 16, horizontal: 8),
        decoration: BoxDecoration(
          color: const Color(0xFF161B22),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.white10),
        ),
        child: Column(
          children: [
            Text(
              label,
              style: const TextStyle(color: Colors.grey, fontSize: 11),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              value,
              style: TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.bold,
                color: color,
              ),
            ),
          ],
        ),
      );
}
