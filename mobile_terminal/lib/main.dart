// File: mobile_terminal/lib/main.dart

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'dart:async';
import 'dart:math';
import 'package:candlesticks/candlesticks.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';

void main() {
  runApp(const TradeCoreApp());
}

class TradeCoreApp extends StatelessWidget {
  const TradeCoreApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TradeCore v43',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF0F1115),
        cardColor: const Color(0xFF1E222B),
        iconTheme: const IconThemeData(color: Colors.white70),
        colorScheme: const ColorScheme.dark(
          primary: Colors.cyanAccent,
          secondary: Colors.purpleAccent,
        ),
      ),
      home: const MainTabController(),
    );
  }
}

class MainTabController extends StatefulWidget {
  const MainTabController({super.key});
  @override
  State<MainTabController> createState() => _MainTabControllerState();
}

class _MainTabControllerState extends State<MainTabController> {
  int _currentIndex = 0;
  final List<Widget> _pages = [
    const DashboardPage(),
    const ScannerPage(),
    const AuditPage(),
  ];
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _pages[_currentIndex],
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) => setState(() => _currentIndex = index),
        backgroundColor: Theme.of(context).cardColor,
        selectedItemColor: Colors.cyanAccent,
        unselectedItemColor: Colors.grey,
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.dashboard),
            label: "Command",
          ),
          BottomNavigationBarItem(icon: Icon(Icons.radar), label: "Scanner"),
          BottomNavigationBarItem(icon: Icon(Icons.pie_chart), label: "Audit"),
        ],
      ),
    );
  }
}

// --- 1. TRADE DETAIL ---
class TradeDetailScreen extends StatefulWidget {
  final String symbol, signal;
  const TradeDetailScreen({
    super.key,
    required this.symbol,
    required this.signal,
  });
  @override
  State<TradeDetailScreen> createState() => _TradeDetailScreenState();
}

class _TradeDetailScreenState extends State<TradeDetailScreen> {
  bool _loading = true;
  Map<String, dynamic>? _data;
  List<Candle> _candles = [];
  double _riskAmount = 50.0;
  int _mode = 0;
  double _manualLot = 0.01;
  double _aiLot = 0.0;
  bool _chartInteractive = false;
  bool _showTools = true;

  @override
  void initState() {
    super.initState();
    _fetchData();
  }

  Future<void> _fetchData() async {
    try {
      final res = await http.get(
        Uri.parse('http://127.0.0.1:8000/quant/market_data/${widget.symbol}'),
      );
      if (res.statusCode == 200) {
        final d = jsonDecode(res.body);
        setState(() {
          _data = d;
          if (d['chart_data'] != null && (d['chart_data'] as List).isNotEmpty) {
            _candles = (d['chart_data'] as List)
                .map(
                  (e) => Candle(
                    date: DateTime.fromMillisecondsSinceEpoch(e['date']),
                    high: (e['high'] as num).toDouble(),
                    low: (e['low'] as num).toDouble(),
                    open: (e['open'] as num).toDouble(),
                    close: (e['close'] as num).toDouble(),
                    volume: (e['volume'] as num).toDouble(),
                  ),
                )
                .toList()
                .reversed
                .toList();
          }
          _loading = false;
        });
      } else {
        throw Exception("API Error");
      }
    } catch (e) {
      if (mounted) {
        setState(() => _loading = false);
      }
    }
  }

  Future<void> _askAI() async {
    try {
      final res = await http.post(
        Uri.parse('http://127.0.0.1:8000/quant/ai_setup'),
        headers: {"Content-Type": "application/json"},
        body: jsonEncode({"symbol": widget.symbol, "risk": _riskAmount}),
      );
      if (res.statusCode == 200) {
        final d = jsonDecode(res.body);
        setState(() {
          _aiLot = d['ai_lot'];
          _mode = 1;
        });
      }
    } catch (e) {}
  }

  Future<void> _execute() async {
    double finalLot = _mode == 0 ? _manualLot : _aiLot;
    final res = await http.post(
      Uri.parse('http://127.0.0.1:8000/quant/execute_manual'),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({
        "symbol": widget.symbol,
        "signal": widget.signal,
        "lot_size": finalLot,
      }),
    );
    if (mounted) {
      final body = jsonDecode(res.body);
      bool success = body['status'] == "Executed";
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          backgroundColor: success ? Colors.green : Colors.red,
          content: Text(body['message']),
        ),
      );
      if (success) Navigator.pop(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading)
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    if (_candles.isEmpty)
      return Scaffold(
        appBar: AppBar(title: Text(widget.symbol)),
        body: const Center(child: Text("No Chart Data Available")),
      );

    final price = _data!['current_price'] ?? 0.0;
    final ichi = _data!['ichimoku'];
    final fib = _data!['fibonacci'];
    bool isBuy = widget.signal.contains("BUY");
    double slPrice = isBuy ? price * 0.998 : price * 1.002;
    double tpPrice = isBuy ? price * 1.004 : price * 0.996;
    double pips =
        (price - slPrice).abs() * (widget.symbol.contains("JPY") ? 100 : 10000);
    double projectedProfit = _riskAmount * 2.0;

    return Scaffold(
      appBar: AppBar(
        title: Text(widget.symbol),
        actions: [
          IconButton(
            icon: Icon(_showTools ? Icons.layers : Icons.layers_clear),
            onPressed: () => setState(() => _showTools = !_showTools),
            tooltip: "Toggle Overlay",
          ),
          IconButton(
            icon: const Icon(Icons.fullscreen),
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(
                builder: (_) => FullscreenChart(
                  symbol: widget.symbol,
                  candles: _candles,
                  entry: price,
                  sl: slPrice,
                  tp: tpPrice,
                  isBuy: isBuy,
                ),
              ),
            ),
            tooltip: "Fullscreen",
          ),
        ],
      ),
      body: SingleChildScrollView(
        physics: _chartInteractive
            ? const NeverScrollableScrollPhysics()
            : const BouncingScrollPhysics(),
        child: Column(
          children: [
            Stack(
              children: [
                SizedBox(
                  height: 400,
                  child: AbsorbPointer(
                    absorbing: !_chartInteractive,
                    child: Candlesticks(candles: _candles),
                  ),
                ),
                if (_showTools)
                  Positioned.fill(
                    child: CustomPaint(
                      painter: AutoToolPainter(
                        candles: _candles,
                        entry: price,
                        sl: slPrice,
                        tp: tpPrice,
                        isBuy: isBuy,
                      ),
                    ),
                  ),
                Positioned(
                  bottom: 10,
                  right: 10,
                  child: FloatingActionButton.small(
                    backgroundColor: _chartInteractive
                        ? Colors.green
                        : Colors.grey,
                    child: Icon(
                      _chartInteractive ? Icons.lock_open : Icons.lock,
                    ),
                    onPressed: () =>
                        setState(() => _chartInteractive = !_chartInteractive),
                    tooltip: "Unlock to Zoom",
                  ),
                ),
                Positioned(
                  top: 10,
                  left: 10,
                  child: Container(
                    padding: const EdgeInsets.all(4),
                    color: Colors.black54,
                    child: Text(
                      _chartInteractive ? "ZOOM/PAN ACTIVE" : "SCROLL LOCKED",
                      style: const TextStyle(
                        fontSize: 10,
                        color: Colors.white70,
                      ),
                    ),
                  ),
                ),
              ],
            ),

            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    "Technical Intelligence",
                    style: TextStyle(
                      fontWeight: FontWeight.bold,
                      color: Colors.cyan,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Container(
                    padding: const EdgeInsets.all(15),
                    decoration: BoxDecoration(
                      color: Theme.of(context).cardColor,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Column(
                      children: [
                        _Row(
                          "Ichimoku Cloud",
                          ichi['status'] ?? "N/A",
                          (ichi['status'] == "Bullish")
                              ? Colors.green
                              : Colors.red,
                        ),
                        _Row(
                          "Tenkan (9)",
                          (ichi['tenkan_sen'] as num).toStringAsFixed(4),
                          Colors.grey,
                        ),
                        _Row(
                          "Fib 61.8%",
                          (fib['61.8%'] as num).toStringAsFixed(4),
                          Colors.amber,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 20),
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Colors.white10,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Column(
                      children: [
                        Row(
                          children: [
                            Expanded(
                              child: ChoiceChip(
                                label: const Text("Manual"),
                                selected: _mode == 0,
                                onSelected: (b) => setState(() => _mode = 0),
                              ),
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: ChoiceChip(
                                label: const Text("AI Risk"),
                                selected: _mode == 1,
                                onSelected: (b) => _askAI(),
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 15),
                        if (_mode == 0)
                          Slider(
                            value: _manualLot,
                            min: 0.01,
                            max: 2.0,
                            divisions: 199,
                            onChanged: (v) => setState(() => _manualLot = v),
                          ),
                        if (_mode == 1)
                          Column(
                            children: [
                              Text(
                                "Risking \$${_riskAmount.toInt()}",
                                style: const TextStyle(color: Colors.redAccent),
                              ),
                              Slider(
                                value: _riskAmount,
                                min: 10,
                                max: 1000,
                                divisions: 99,
                                activeColor: Colors.red,
                                onChanged: (v) {
                                  setState(() => _riskAmount = v);
                                  _askAI();
                                },
                              ),
                              Text(
                                "AI Lot: $_aiLot",
                                style: const TextStyle(
                                  fontWeight: FontWeight.bold,
                                  fontSize: 18,
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 15),
                  Container(
                    padding: const EdgeInsets.all(15),
                    decoration: BoxDecoration(
                      border: Border.all(
                        color: isBuy
                            ? Colors.green.withOpacity(0.5)
                            : Colors.red.withOpacity(0.5),
                      ),
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Column(
                      children: [
                        _Row(
                          "Projected Profit",
                          "\$${(projectedProfit ?? 0.0).toStringAsFixed(2)}",
                          Colors.greenAccent,
                        ),
                        _Row(
                          "Entry Price",
                          price.toStringAsFixed(5),
                          Colors.white,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 20),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      onPressed: _execute,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: isBuy ? Colors.green : Colors.red,
                        padding: const EdgeInsets.symmetric(vertical: 20),
                      ),
                      child: Text("CONFIRM EXECUTION"),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _Row(String l, String v, Color c) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 4),
    child: Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(l),
        Text(
          v,
          style: TextStyle(color: c, fontWeight: FontWeight.bold),
        ),
      ],
    ),
  );
}

// --- PAINTER & FULLSCREEN ---
class AutoToolPainter extends CustomPainter {
  final List<Candle> candles;
  final double entry, sl, tp;
  final bool isBuy;
  AutoToolPainter({
    required this.candles,
    required this.entry,
    required this.sl,
    required this.tp,
    required this.isBuy,
  });
  @override
  void paint(Canvas canvas, Size size) {
    if (candles.isEmpty) return;
    final Paint line = Paint()
      ..strokeWidth = 1.0
      ..style = PaintingStyle.stroke;
    final Paint fill = Paint()..style = PaintingStyle.fill;
    double maxP = candles.map((c) => c.high).reduce(max);
    double minP = candles.map((c) => c.low).reduce(min);
    double range = maxP - minP;
    if (range == 0) return;
    double getY(double p) => size.height - ((p - minP) / range * size.height);
    double yE = getY(entry);
    double ySL = getY(sl);
    double yTP = getY(tp);
    line.color = Colors.white70;
    _drawDashed(canvas, 0, size.width, yE, line);
    fill.color = Colors.red.withOpacity(0.1);
    canvas.drawRect(
      Rect.fromLTRB(0, min(yE, ySL), size.width, max(yE, ySL)),
      fill,
    );
    fill.color = Colors.green.withOpacity(0.1);
    canvas.drawRect(
      Rect.fromLTRB(0, min(yE, yTP), size.width, max(yE, yTP)),
      fill,
    );
    line.color = Colors.cyan.withOpacity(0.5);
    line.strokeWidth = 2.0;
    canvas.drawLine(
      Offset(0, getY(candles.last.close)),
      Offset(size.width, getY(candles.first.close)),
      line,
    );
  }

  void _drawDashed(Canvas c, double start, double end, double y, Paint p) {
    while (start < end) {
      c.drawLine(Offset(start, y), Offset(start + 5, y), p);
      start += 10;
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter old) => true;
}

class FullscreenChart extends StatefulWidget {
  final String symbol;
  final List<Candle> candles;
  final double entry, sl, tp;
  final bool isBuy;
  const FullscreenChart({
    super.key,
    required this.symbol,
    required this.candles,
    required this.entry,
    required this.sl,
    required this.tp,
    required this.isBuy,
  });
  @override
  State<FullscreenChart> createState() => _FullscreenChartState();
}

class _FullscreenChartState extends State<FullscreenChart> {
  @override
  void initState() {
    super.initState();
    SystemChrome.setPreferredOrientations([DeviceOrientation.landscapeRight]);
  }

  @override
  void dispose() {
    SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.symbol)),
      body: Stack(
        children: [
          Candlesticks(candles: widget.candles),
          Positioned.fill(
            child: CustomPaint(
              painter: AutoToolPainter(
                candles: widget.candles,
                entry: widget.entry,
                sl: widget.sl,
                tp: widget.tp,
                isBuy: widget.isBuy,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// --- 2. COMMAND PAGE ---
class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key});
  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  bool _isRunning = false;
  List<String> _logs = ["Connecting..."];
  Map<String, dynamic>? _account;
  List<dynamic> _positions = [];
  Timer? _timer;
  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(seconds: 1), (t) => _fetch());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetch() async {
    try {
      final res = await http.get(Uri.parse('http://127.0.0.1:8000/bot/status'));
      if (res.statusCode == 200) {
        final d = jsonDecode(res.body);
        if (mounted)
          setState(() {
            _isRunning = d['is_running'];
            _logs = List<String>.from(d['recent_logs']);
            _account = d['account'];
            _positions = d['positions'] ?? [];
          });
      }
    } catch (e) {}
  }

  @override
  Widget build(BuildContext context) {
    final eq = _account != null
        ? "\$${(_account!['equity'] ?? 0.0).toStringAsFixed(2)}"
        : "---";
    final bal = _account != null
        ? "\$${(_account!['balance'] ?? 0.0).toStringAsFixed(2)}"
        : "---";
    final pnl = _account != null
        ? "\$${(_account!['profit'] ?? 0.0).toStringAsFixed(2)}"
        : "---";
    return Scaffold(
      appBar: AppBar(
        title: const Text("Command Center"),
        backgroundColor: Colors.transparent,
      ),
      body: SingleChildScrollView(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: const [
                      _Clock("London", 8, 16, Colors.blue),
                      _Clock("New York", 13, 21, Colors.green),
                      _Clock("Tokyo", 0, 9, Colors.red),
                    ],
                  ),
                  const SizedBox(height: 20),
                  Row(
                    children: [
                      Expanded(child: _Card("Equity", eq, Colors.purple)),
                      const SizedBox(width: 10),
                      Expanded(child: _Card("Balance", bal, Colors.blue)),
                    ],
                  ),
                  const SizedBox(height: 10),
                  _Card(
                    "Floating PnL",
                    pnl,
                    pnl.contains("-") ? Colors.red : Colors.green,
                  ),
                  const SizedBox(height: 20),
                  const Text(
                    "Active Positions",
                    style: TextStyle(
                      fontWeight: FontWeight.bold,
                      color: Colors.cyan,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Container(
                    decoration: BoxDecoration(
                      color: Theme.of(context).cardColor,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    height: 200,
                    child: _positions.isEmpty
                        ? const Center(
                            child: Text(
                              "No Active Trades",
                              style: TextStyle(color: Colors.grey),
                            ),
                          )
                        : ListView.builder(
                            itemCount: _positions.length,
                            itemBuilder: (c, i) {
                              final p = _positions[i];
                              return ListTile(
                                dense: true,
                                leading: Icon(
                                  p['type'] == "BUY"
                                      ? Icons.arrow_upward
                                      : Icons.arrow_downward,
                                  color: p['type'] == "BUY"
                                      ? Colors.green
                                      : Colors.red,
                                  size: 16,
                                ),
                                title: Text(
                                  p['symbol'],
                                  style: const TextStyle(
                                    fontWeight: FontWeight.bold,
                                  ),
                                ),
                                subtitle: Text("Vol: ${p['volume']}"),
                                trailing: Text(
                                  "\$${p['profit'].toStringAsFixed(2)}",
                                  style: TextStyle(
                                    color: p['profit'] >= 0
                                        ? Colors.green
                                        : Colors.red,
                                    fontWeight: FontWeight.bold,
                                  ),
                                ),
                              );
                            },
                          ),
                  ),
                  const SizedBox(height: 20),
                  SwitchListTile(
                    title: const Text("System Power"),
                    value: _isRunning,
                    onChanged: (v) async {
                      await http.post(
                        Uri.parse(
                          'http://127.0.0.1:8000/bot/${v ? "start" : "stop"}',
                        ),
                      );
                      _fetch();
                    },
                  ),
                  ElevatedButton.icon(
                    onPressed: () async {
                      final Uri url = Uri.parse(
                        'http://127.0.0.1:8000/system/logs',
                      );
                      if (!await launchUrl(url))
                        throw Exception('Could not launch');
                    },
                    icon: const Icon(Icons.file_download),
                    label: const Text("Download Live Logs"),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.white10,
                    ),
                  ),
                  const Divider(),
                  Container(
                    height: 150,
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Colors.black54,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: ListView.builder(
                      itemCount: _logs.length,
                      itemBuilder: (c, i) => Text(
                        _logs[i],
                        style: const TextStyle(
                          fontFamily: "Courier",
                          fontSize: 11,
                          color: Colors.white70,
                        ),
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
  }
}

class _Clock extends StatelessWidget {
  final String c;
  final int o, cl;
  final Color k;
  const _Clock(this.c, this.o, this.cl, this.k);
  @override
  Widget build(BuildContext context) {
    final h = DateTime.now().toUtc().hour;
    bool open = h >= o && h < cl;
    return Column(
      children: [
        Text(
          c,
          style: TextStyle(color: k, fontWeight: FontWeight.bold),
        ),
        Text(
          open ? "OPEN" : "CLOSED",
          style: TextStyle(
            fontSize: 10,
            color: open ? Colors.white : Colors.grey,
          ),
        ),
      ],
    );
  }
}

class _Card extends StatelessWidget {
  final String l, v;
  final Color c;
  const _Card(this.l, this.v, this.c);
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(15),
      decoration: BoxDecoration(
        color: Theme.of(context).cardColor,
        border: Border.all(color: c.withOpacity(0.5)),
        borderRadius: BorderRadius.circular(10),
      ),
      width: double.infinity,
      child: Column(
        children: [
          Text(l, style: const TextStyle(color: Colors.grey, fontSize: 10)),
          Text(
            v,
            style: TextStyle(
              color: c,
              fontSize: 18,
              fontWeight: FontWeight.bold,
            ),
          ),
        ],
      ),
    );
  }
}

// --- 3. SCANNER PAGE (HOT START FIX) ---
class ScannerPage extends StatefulWidget {
  const ScannerPage({super.key});
  @override
  State<ScannerPage> createState() => _ScannerPageState();
}

class _ScannerPageState extends State<ScannerPage> {
  List<dynamic> _opps = [];
  bool _loading = false;
  Future<void> _scan() async {
    // VISUAL FEEDBACK: Clear list immediately so user knows refresh is happening
    setState(() {
      _loading = true;
      _opps = [];
    });
    try {
      final res = await http.get(
        Uri.parse('http://127.0.0.1:8000/quant/scan_all'),
      );
      if (res.statusCode == 200) {
        var list = jsonDecode(res.body)['opportunities'] as List;
        list.sort((a, b) => b['confidence'].compareTo(a['confidence']));
        if (mounted) setState(() => _opps = list);
      }
    } catch (e) {
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Smart Scanner")),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: ElevatedButton(
              onPressed: _loading ? null : _scan,
              child: _loading
                  ? const CircularProgressIndicator()
                  : const Text("SCAN NOW"),
            ),
          ),
          Expanded(
            child: ListView.builder(
              itemCount: _opps.length,
              itemBuilder: (c, i) {
                final t = _opps[i];
                bool buy = t['signal'].toString().contains("BUY");
                return ListTile(
                  leading: CircleAvatar(
                    backgroundColor: buy
                        ? Colors.green.withOpacity(0.2)
                        : Colors.red.withOpacity(0.2),
                    child: Icon(
                      buy ? Icons.arrow_upward : Icons.arrow_downward,
                      color: buy ? Colors.green : Colors.red,
                    ),
                  ),
                  title: Text(
                    t['symbol'],
                    style: const TextStyle(fontWeight: FontWeight.bold),
                  ),
                  subtitle: Text(t['reason']),
                  trailing: Container(
                    padding: const EdgeInsets.all(6),
                    decoration: BoxDecoration(
                      color: Colors.blueAccent,
                      borderRadius: BorderRadius.circular(5),
                    ),
                    child: Text(
                      "${(t['confidence'] * 100).toInt()}% Match",
                      style: const TextStyle(color: Colors.white, fontSize: 10),
                    ),
                  ),
                  onTap: () => Navigator.push(
                    context,
                    MaterialPageRoute(
                      builder: (_) => TradeDetailScreen(
                        symbol: t['symbol'],
                        signal: t['signal'],
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

// --- 4. AUDIT PAGE (UNCHANGED) ---
class AuditPage extends StatefulWidget {
  const AuditPage({super.key});
  @override
  State<AuditPage> createState() => _AuditPageState();
}

class _AuditPageState extends State<AuditPage> {
  Map<String, dynamic>? _audit;
  Timer? _timer;
  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(seconds: 5), (t) => _fetch());
    _fetch();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetch() async {
    try {
      final res = await http.get(
        Uri.parse('http://127.0.0.1:8000/quant/audit'),
      );
      if (res.statusCode == 200) {
        if (mounted) setState(() => _audit = jsonDecode(res.body));
      }
    } catch (e) {}
  }

  @override
  Widget build(BuildContext context) {
    if (_audit == null) return const Center(child: CircularProgressIndicator());
    final netProfit = _audit?['net_profit'] ?? 0.0;
    final winRate = _audit?['win_rate'] ?? 0.0;
    final totalTrades = _audit?['total_trades'] ?? 0;
    final curve = (_audit?['equity_curve'] as List<dynamic>? ?? [])
        .map((e) => double.tryParse(e.toString()) ?? 0.0)
        .toList();
    List<FlSpot> spots = [];
    for (int i = 0; i < curve.length; i++)
      spots.add(FlSpot(i.toDouble(), curve[i]));

    return Scaffold(
      appBar: AppBar(
        title: const Text("Account Audit"),
        backgroundColor: Colors.transparent,
      ),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            Row(
              children: [
                Expanded(
                  child: _Card(
                    "Net Profit",
                    "\$${netProfit}",
                    (netProfit >= 0) ? Colors.green : Colors.red,
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(child: _Card("Win Rate", "${winRate}%", Colors.cyan)),
              ],
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: _Card("Total Trades", "$totalTrades", Colors.white),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: _Card("Source", "${_audit?['source']}", Colors.amber),
                ),
              ],
            ),
            const SizedBox(height: 20),
            const Text(
              "Equity Growth Curve",
              style: TextStyle(fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 10),
            SizedBox(
              height: 200,
              child: spots.isNotEmpty
                  ? LineChart(
                      LineChartData(
                        gridData: FlGridData(show: false),
                        titlesData: FlTitlesData(show: false),
                        borderData: FlBorderData(
                          show: true,
                          border: Border.all(color: Colors.white10),
                        ),
                        lineBarsData: [
                          LineChartBarData(
                            spots: spots,
                            isCurved: true,
                            color: Colors.purpleAccent,
                            barWidth: 3,
                            belowBarData: BarAreaData(
                              show: true,
                              color: Colors.purple.withOpacity(0.2),
                            ),
                          ),
                        ],
                      ),
                    )
                  : const Text("No Data"),
            ),
            const Spacer(),
            // AUDIT DOWNLOAD BUTTON
            ElevatedButton.icon(
              onPressed: () async {
                final Uri url = Uri.parse(
                  'http://127.0.0.1:8000/quant/export_report',
                );
                if (!await launchUrl(url)) throw Exception('Could not launch');
              },
              icon: const Icon(Icons.download),
              label: const Text("Download Trade History (CSV)"),
            ),
          ],
        ),
      ),
    );
  }
}
