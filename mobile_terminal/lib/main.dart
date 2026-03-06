import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:url_launcher/url_launcher.dart';

void main() {
  runApp(const TradeCoreApp());
}

// ──────────────────────────────────────────────────────────────────────────────
// CONFIGURATION
// Must match the TRADECORE_API_KEY environment variable set on the server.
// For the paper account default: 'dev-paper'
// ──────────────────────────────────────────────────────────────────────────────
// [SPRINT 4 FIX] API key was missing from all HTTP requests — every call
// received 403 Forbidden after the hotfix_main.py middleware was deployed.
const String _apiKey = String.fromEnvironment(
  'TRADECORE_API_KEY',
  defaultValue: 'dev-paper',
);
const String _baseUrl = String.fromEnvironment(
  'TRADECORE_URL',
  defaultValue: 'http://127.0.0.1:8000',
);

// Auth headers added to every request
Map<String, String> get _headers => {'X-API-Key': _apiKey};

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

  // [SPRINT 4 FIX] Two separate timers:
  //   _statusTimer  — 3s:  fast live data (balance, positions, PnL)
  //   _perfTimer    — 30s: slow historical data (win rate, PF, equity curve)
  // Previously a single 3s timer called /bot/performance on every tick.
  // The server-side 60s cache meant those calls returned stale data anyway,
  // and they still cost a thread and a TCP connection each time.
  Timer? _statusTimer;
  Timer? _perfTimer;

  // Live MT5 State
  bool isBackendOnline = false;
  double balance = 0.0;
  double equity = 0.0;
  double marginLevel = 0.0;
  double freeMargin = 0.0;
  double totalPnl = 0.0;
  List<dynamic> activePositions = [];
  List<dynamic> newsEvents = [];
  String marketRegime = "CALIBRATING...";
  double dailyVaR = 0.0;

  // Performance State
  double totalRealized = 0.0;
  double monthlyRealized = 0.0;
  double winRate = 0.0;
  double profitFactor = 0.0;
  int totalTrades = 0;
  List<FlSpot> equitySpots = [];
  List<String> equityDates = [];

  final NumberFormat usdFormat = NumberFormat.currency(
    symbol: '\$ ',
    decimalDigits: 2,
  );
  final NumberFormat compactUsdFormat = NumberFormat.compactCurrency(
    symbol: '\$',
    decimalDigits: 2,
  );

  // ── CALCULATOR STATE ─────────────────────────────────────────────────────
  final TextEditingController _calcBalanceController = TextEditingController();
  final TextEditingController _calcRiskController = TextEditingController(
    text: "1.0",
  );
  final TextEditingController _calcSlController = TextEditingController(
    text: "20",
  );
  String _calcLotResult = "0.00";
  String _calcExposureResult = "\$0.00";

  // [SPRINT 4 FIX] Asset class selector for the calculator.
  // The old formula (slDist * 100) only worked correctly for FX pairs
  // with a pip-denominated SL. Gold, BTC and indices need different
  // pip/point values per lot. The selector drives _capitalPerLot().
  String _calcAssetClass = 'FX';
  static const List<String> _assetClasses = [
    'FX',
    'Gold',
    'BTC/ETH',
    'Indices',
  ];

  // SL unit labels displayed to the user per asset class
  static const Map<String, String> _slLabel = {
    'FX': 'SL (pips)',
    'Gold': 'SL (\$/oz)',
    'BTC/ETH': 'SL (\$)',
    'Indices': 'SL (pts)',
  };

  // Dollar at risk per lot per 1 unit of SL input
  // FX:      1 pip = $10/lot on standard lot
  // Gold:    $1/oz = $100/lot (100 oz contract)
  // BTC/ETH: $1    = $1/lot  (1 coin contract)
  // Indices: 1 pt  = $1/lot  (varies by broker — sensible default)
  static const Map<String, double> _lotValue = {
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

  @override
  void initState() {
    super.initState();

    // Immediate first fetch on launch
    _fetchStatus();
    _fetchPerformance();
    _fetchNews();

    // Status: every 3s
    _statusTimer = Timer.periodic(const Duration(seconds: 3), (_) {
      _fetchStatus();
      if (_currentIndex == 2) _fetchNews();
    });

    // Performance: every 30s (server cache is 60s so no point going faster)
    _perfTimer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (_currentIndex == 1) _fetchPerformance();
    });
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _perfTimer?.cancel();
    _calcBalanceController.dispose();
    _calcRiskController.dispose();
    _calcSlController.dispose();
    super.dispose();
  }

  // ── FETCH HELPERS ────────────────────────────────────────────────────────

  Future<void> _fetchStatus() async {
    try {
      final res = await http
          .get(Uri.parse('$_baseUrl/bot/status'), headers: _headers)
          .timeout(const Duration(seconds: 3));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        setState(() {
          isBackendOnline = data['is_running'] ?? false;
          balance = (data['account']?['balance'] ?? 0).toDouble();
          equity = (data['account']?['equity'] ?? 0).toDouble();
          marginLevel = (data['account']?['margin_level'] ?? 0).toDouble();
          freeMargin = (data['account']?['free_margin'] ?? 0).toDouble();
          totalPnl = (data['total_pnl'] ?? 0).toDouble();
          activePositions = data['positions'] ?? [];
          marketRegime = data['market_regime'] ?? "CALIBRATING...";
          dailyVaR = (data['daily_var'] ?? 0).toDouble();

          // Pre-fill calculator balance field on first run
          if (_calcBalanceController.text.isEmpty && balance > 0) {
            _calcBalanceController.text = balance.toStringAsFixed(2);
            _calculatePositionSize();
          }
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
      final res = await http
          .get(Uri.parse('$_baseUrl/bot/performance'), headers: _headers)
          .timeout(const Duration(seconds: 5));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        setState(() {
          totalRealized = (data['total_realized'] ?? 0).toDouble();
          monthlyRealized = (data['monthly_realized'] ?? 0).toDouble();
          winRate = (data['win_rate'] ?? 0).toDouble();
          profitFactor = (data['profit_factor'] ?? 0).toDouble();
          totalTrades = data['total_trades'] ?? 0;

          final List<dynamic> curveData = data['curve'] ?? [];
          equitySpots.clear();
          equityDates.clear();
          for (int i = 0; i < curveData.length; i++) {
            equitySpots.add(
              FlSpot(i.toDouble(), (curveData[i]['profit'] ?? 0).toDouble()),
            );
            equityDates.add(curveData[i]['date'] ?? '');
          }
        });
      }
    } catch (_) {}
  }

  Future<void> _fetchNews() async {
    try {
      final res = await http
          .get(Uri.parse('$_baseUrl/bot/news'), headers: _headers)
          .timeout(const Duration(seconds: 5));
      if (res.statusCode == 200) {
        setState(() => newsEvents = json.decode(res.body));
      }
    } catch (_) {}
  }

  Future<void> _fetchData() async {
    await Future.wait([_fetchStatus(), _fetchPerformance(), _fetchNews()]);
  }

  Future<void> _downloadAuditReport() async {
    // Audit CSV download — auth via query param since url_launcher
    // cannot set custom headers on browser navigation
    final Uri url = Uri.parse('$_baseUrl/quant/export_report?key=$_apiKey');
    if (!await launchUrl(url)) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not trigger report download.')),
        );
      }
    }
  }

  // ── CALCULATOR ───────────────────────────────────────────────────────────

  void _calculatePositionSize() {
    final double bal = double.tryParse(_calcBalanceController.text) ?? 0.0;
    final double riskPct = double.tryParse(_calcRiskController.text) ?? 1.0;
    final double slUnits = double.tryParse(_calcSlController.text) ?? 0.0;

    if (bal <= 0 || slUnits <= 0) return;

    final double riskCapital = bal * (riskPct / 100);
    final double lotValue = _lotValue[_calcAssetClass] ?? 10.0;
    final double minLot = _minLot[_calcAssetClass] ?? 0.01;
    final double capitalPerLot = slUnits * lotValue;
    final double rawLots = riskCapital / capitalPerLot;
    final double finalLots = rawLots < minLot ? minLot : rawLots;
    final double finalExposure = finalLots * capitalPerLot;

    setState(() {
      _calcLotResult = finalLots.toStringAsFixed(2);
      _calcExposureResult = compactUsdFormat.format(finalExposure);
    });
  }

  // ──────────────────────────────────────────────────────────────────────────
  // BUILD
  // ──────────────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final pages = [
      _buildLiveTerminal(),
      _buildQuantDashboard(),
      _buildNewsGuard(),
    ];

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'TradeCore v51.0',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        backgroundColor: const Color(0xFF161B22),
        elevation: 0,
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16.0),
            child: Row(
              children: [
                Icon(
                  Icons.circle,
                  size: 12,
                  color: isBackendOnline
                      ? const Color(0xFF00C853)
                      : Colors.redAccent,
                ),
                const SizedBox(width: 8),
                Text(
                  isBackendOnline ? "ONLINE" : "OFFLINE",
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
              ],
            ),
          ),
        ],
      ),
      body: pages[_currentIndex],
      bottomNavigationBar: BottomNavigationBar(
        backgroundColor: const Color(0xFF161B22),
        selectedItemColor: const Color(0xFF00C853),
        unselectedItemColor: Colors.grey,
        currentIndex: _currentIndex,
        onTap: (index) {
          setState(() => _currentIndex = index);
          // Trigger appropriate fetch immediately when switching tabs
          if (index == 1) _fetchPerformance();
          if (index == 2) _fetchNews();
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

  // ── LIVE TERMINAL ─────────────────────────────────────────────────────────

  Widget _buildLiveTerminal() {
    return RefreshIndicator(
      onRefresh: _fetchData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _card(
            child: Column(
              children: [
                const Text(
                  "LIVE FLOATING PnL",
                  style: TextStyle(color: Colors.grey, letterSpacing: 1.2),
                ),
                const SizedBox(height: 16),
                Text(
                  usdFormat.format(totalPnl),
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
                    _statCol("Balance", usdFormat.format(balance)),
                    _statCol("Equity", usdFormat.format(equity)),
                  ],
                ),
                const SizedBox(height: 16),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    _statCol("Free Margin", usdFormat.format(freeMargin)),
                    _statCol(
                      "Margin Level",
                      "${marginLevel.toStringAsFixed(1)}%",
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          const Text(
            "ACTIVE TRADES",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          if (activePositions.isEmpty)
            const Center(
              child: Padding(
                padding: EdgeInsets.all(32.0),
                child: Text(
                  "Scanning markets...",
                  style: TextStyle(color: Colors.white54),
                ),
              ),
            )
          else
            ...activePositions.map((pos) {
              final bool isBuy = pos['type'] == 'BUY';
              final double profit = (pos['profit'] ?? 0).toDouble();
              return Card(
                margin: const EdgeInsets.only(bottom: 8),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
                child: ListTile(
                  leading: CircleAvatar(
                    backgroundColor: isBuy
                        ? const Color(0xFF00C853).withOpacity(0.2)
                        : Colors.redAccent.withOpacity(0.2),
                    child: Icon(
                      isBuy ? Icons.arrow_upward : Icons.arrow_downward,
                      color: isBuy ? const Color(0xFF00C853) : Colors.redAccent,
                    ),
                  ),
                  title: Text(
                    "${pos['symbol']}  •  ${pos['volume']} Lots",
                    style: const TextStyle(fontWeight: FontWeight.bold),
                  ),
                  subtitle: Text(
                    "Open: ${pos['open_price']}  |  SL: ${pos['sl']}",
                  ),
                  trailing: Text(
                    usdFormat.format(profit),
                    style: TextStyle(
                      fontWeight: FontWeight.bold,
                      fontSize: 16,
                      color: profit >= 0
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
  }

  // ── QUANT DASHBOARD ───────────────────────────────────────────────────────

  Widget _buildQuantDashboard() {
    final double dynamicTarget = balance > 0 ? (balance * 0.05) : 500.0;
    final double progressPct = dynamicTarget > 0
        ? (monthlyRealized / dynamicTarget).clamp(0.0, 1.0)
        : 0.0;

    return RefreshIndicator(
      onRefresh: _fetchData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // MONTHLY TARGET
          const Text(
            "DYNAMIC MONTHLY TARGET (5%)",
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
                          "Achieved",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFormat.format(monthlyRealized),
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
                          "Target",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFormat.format(dynamicTarget),
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
                  value: progressPct,
                  backgroundColor: Colors.white10,
                  color: const Color(0xFF2962FF),
                  minHeight: 6,
                  borderRadius: BorderRadius.circular(4),
                ),
              ],
            ),
          ),

          // ALGORITHMIC AUDIT
          const SizedBox(height: 24),
          const Text(
            "ALGORITHMIC AUDIT",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: _metricTile(
                  "Win Rate",
                  "${winRate.toStringAsFixed(1)}%",
                  color: winRate > 50 ? const Color(0xFF00C853) : Colors.orange,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _metricTile(
                  "Profit Factor",
                  profitFactor.toStringAsFixed(2),
                  color: profitFactor >= 1.5
                      ? const Color(0xFF2962FF)
                      : Colors.white,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _metricTile("Executions", totalTrades.toString()),
              ),
            ],
          ),

          // INSTITUTIONAL RISK
          const SizedBox(height: 24),
          const Text(
            "INSTITUTIONAL RISK",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          _card(
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                _statCol("GARCH Regime", marketRegime),
                _statCol(
                  "Daily VaR",
                  dailyVaR == 0 ? "..." : usdFormat.format(dailyVaR),
                ),
              ],
            ),
          ),

          // EQUITY CURVE
          const SizedBox(height: 24),
          const Text(
            "EQUITY CURVE",
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
                      "Awaiting trade data...",
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

          // RISK CALCULATOR
          const SizedBox(height: 24),
          const Text(
            "RISK CALCULATOR & AUDIT",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          _card(
            child: Column(
              children: [
                // Asset class selector
                Row(
                  children: _assetClasses.map((cls) {
                    final bool selected = cls == _calcAssetClass;
                    return Expanded(
                      child: GestureDetector(
                        onTap: () {
                          setState(() {
                            _calcAssetClass = cls;
                            // Reset SL field to sensible default per class
                            _calcSlController.text = switch (cls) {
                              'FX' => '20',
                              'Gold' => '5',
                              'BTC/ETH' => '500',
                              'Indices' => '50',
                              _ => '20',
                            };
                          });
                          _calculatePositionSize();
                        },
                        child: Container(
                          margin: const EdgeInsets.only(right: 4),
                          padding: const EdgeInsets.symmetric(vertical: 6),
                          decoration: BoxDecoration(
                            color: selected
                                ? const Color(0xFF2962FF)
                                : Colors.white10,
                            borderRadius: BorderRadius.circular(6),
                          ),
                          child: Text(
                            cls,
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: 11,
                              fontWeight: FontWeight.bold,
                              color: selected ? Colors.white : Colors.grey,
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
                        controller: _calcBalanceController,
                        decoration: const InputDecoration(
                          labelText: "Bal (\$)",
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calculatePositionSize(),
                        style: const TextStyle(fontSize: 14),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: _calcRiskController,
                        decoration: const InputDecoration(
                          labelText: "Risk (%)",
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calculatePositionSize(),
                        style: const TextStyle(fontSize: 14),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: _calcSlController,
                        decoration: InputDecoration(
                          labelText: _slLabel[_calcAssetClass] ?? "SL",
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calculatePositionSize(),
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
                      "Vol: $_calcLotResult",
                      style: const TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Color(0xFF2962FF),
                      ),
                    ),
                    Text(
                      "Risk: $_calcExposureResult",
                      style: const TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Colors.redAccent,
                      ),
                    ),
                    TextButton.icon(
                      onPressed: _downloadAuditReport,
                      icon: const Icon(Icons.download, size: 16),
                      label: const Text("CSV"),
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

  Widget _buildNewsGuard() {
    return RefreshIndicator(
      onRefresh: _fetchData,
      child: newsEvents.isEmpty
          ? const Center(
              child: Text(
                "No High-Impact News Detected.",
                style: TextStyle(color: Colors.white54),
              ),
            )
          : ListView.builder(
              padding: const EdgeInsets.all(16),
              itemCount: newsEvents.length,
              itemBuilder: (context, index) {
                final event = newsEvents[index];
                final int tier = event['tier'] ?? 2;
                final Color tierColor = tier == 1
                    ? Colors.redAccent
                    : Colors.orangeAccent;
                return Card(
                  margin: const EdgeInsets.only(bottom: 12),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(16.0),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Icon(Icons.warning_amber_rounded, color: tierColor),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Text(
                                "${event['country']} — T$tier ${event['impact']}",
                                style: TextStyle(
                                  fontWeight: FontWeight.bold,
                                  color: tierColor,
                                ),
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        Text(
                          event['title'] ?? '',
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          event['time'] ?? '',
                          style: const TextStyle(color: Colors.grey),
                        ),
                        if ((event['insight'] ?? '').isNotEmpty) ...[
                          const SizedBox(height: 6),
                          Text(
                            event['insight'],
                            style: const TextStyle(
                              color: Colors.white54,
                              fontSize: 12,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                );
              },
            ),
    );
  }

  // ── SHARED WIDGETS ────────────────────────────────────────────────────────

  Widget _card({required Widget child}) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: const Color(0xFF161B22),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white10),
      ),
      child: child,
    );
  }

  Widget _statCol(String label, String value) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: const TextStyle(color: Colors.grey, fontSize: 12)),
        Text(
          value,
          style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
        ),
      ],
    );
  }

  Widget _metricTile(String label, String value, {Color color = Colors.white}) {
    return Container(
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
}
