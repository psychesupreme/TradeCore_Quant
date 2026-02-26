import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:url_launcher/url_launcher.dart'; // NEW IMPORT FOR DOWNLOADING

void main() {
  runApp(const TradeCoreApp());
}

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
  int _currentIndex = 0;
  Timer? _pollingTimer;
  final String baseUrl = 'http://127.0.0.1:8000';

  // Live MT5 State
  bool isBackendOnline = false;
  double balance = 0.0;
  double equity = 0.0;
  double marginLevel = 0.0;
  double freeMargin = 0.0;
  double totalPnl = 0.0;
  List<dynamic> activePositions = [];
  List<dynamic> newsEvents = [];

  // Performance State
  double totalRealized = 0.0;
  double monthlyRealized = 0.0;
  List<FlSpot> equitySpots = [];
  List<String> equityDates = [];

  final NumberFormat usdFormat = NumberFormat.currency(
    symbol: '\$ ',
    decimalDigits: 2,
  );
  final NumberFormat kesFormat = NumberFormat.currency(
    symbol: 'KES ',
    decimalDigits: 2,
  );

  // Expanded Calculator State
  final TextEditingController _calcBalanceController = TextEditingController();
  final TextEditingController _calcRiskController = TextEditingController(
    text: "1.0",
  );
  final TextEditingController _calcSlController = TextEditingController(
    text: "5.0",
  );
  String _calcLotResult = "0.00 Lots";
  String _calcExposureResult = "\$0.00";

  @override
  void initState() {
    super.initState();
    _fetchData();
    _pollingTimer = Timer.periodic(const Duration(seconds: 3), (timer) {
      _fetchData();
    });
  }

  @override
  void dispose() {
    _pollingTimer?.cancel();
    _calcBalanceController.dispose();
    _calcRiskController.dispose();
    _calcSlController.dispose();
    super.dispose();
  }

  Future<void> _fetchData() async {
    try {
      final statusResponse = await http
          .get(Uri.parse('$baseUrl/bot/status'))
          .timeout(const Duration(seconds: 2));
      if (statusResponse.statusCode == 200) {
        final data = json.decode(statusResponse.body);
        setState(() {
          isBackendOnline = data['is_running'] ?? false;
          balance = (data['account']?['balance'] ?? 0).toDouble();
          equity = (data['account']?['equity'] ?? 0).toDouble();
          marginLevel = (data['account']?['margin_level'] ?? 0).toDouble();
          freeMargin = (data['account']?['free_margin'] ?? 0).toDouble();
          totalPnl = (data['total_pnl'] ?? 0).toDouble();
          activePositions = data['positions'] ?? [];

          if (_calcBalanceController.text.isEmpty && balance > 0) {
            _calcBalanceController.text = balance.toStringAsFixed(2);
            _calculatePositionSize();
          }
        });
      }

      if (_currentIndex == 1) {
        final perfResponse = await http
            .get(Uri.parse('$baseUrl/bot/performance'))
            .timeout(const Duration(seconds: 2));
        if (perfResponse.statusCode == 200) {
          final perfData = json.decode(perfResponse.body);
          setState(() {
            totalRealized = (perfData['total_realized'] ?? 0).toDouble();
            monthlyRealized = (perfData['monthly_realized'] ?? 0).toDouble();

            final List<dynamic> curveData = perfData['curve'] ?? [];
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
      }

      if (_currentIndex == 2) {
        final newsResponse = await http
            .get(Uri.parse('$baseUrl/bot/news'))
            .timeout(const Duration(seconds: 2));
        if (newsResponse.statusCode == 200) {
          setState(() {
            newsEvents = json.decode(newsResponse.body);
          });
        }
      }
    } catch (e) {
      setState(() {
        isBackendOnline = false;
      });
    }
  }

  void _calculatePositionSize() {
    double bal = double.tryParse(_calcBalanceController.text) ?? 0.0;
    double riskPct = double.tryParse(_calcRiskController.text) ?? 1.0;
    double slDist = double.tryParse(_calcSlController.text) ?? 5.0;

    if (bal > 0 && slDist > 0) {
      double riskCapital = bal * (riskPct / 100);
      double capitalPerLot = slDist * 100;
      double lots = riskCapital / capitalPerLot;

      double finalLots = lots < 0.20 ? 0.20 : lots;
      double finalExposure = finalLots * capitalPerLot;

      setState(() {
        _calcLotResult = "${finalLots.toStringAsFixed(2)} Lots";
        _calcExposureResult = usdFormat.format(finalExposure);
      });
    }
  }

  Future<void> _downloadAuditReport() async {
    final Uri url = Uri.parse('$baseUrl/quant/export_report');
    if (!await launchUrl(url)) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not trigger report download.')),
        );
      }
    }
  }

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
        onTap: (index) => setState(() => _currentIndex = index),
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

  Widget _buildLiveTerminal() {
    return RefreshIndicator(
      onRefresh: _fetchData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Container(
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: Colors.white10),
            ),
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
                    _buildStatCol("Balance", usdFormat.format(balance)),
                    _buildStatCol("Equity", usdFormat.format(equity)),
                  ],
                ),
                const SizedBox(height: 16),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    _buildStatCol("Free Margin", usdFormat.format(freeMargin)),
                    _buildStatCol(
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
              bool isBuy = pos['type'] == 'BUY';
              double profit = (pos['profit'] ?? 0).toDouble();
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

  Widget _buildStatCol(String label, String value) {
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

  Widget _buildQuantDashboard() {
    double targetRecovery = 19000.0;
    double progressPct = (totalRealized / targetRecovery).clamp(0.0, 1.0);

    return RefreshIndicator(
      onRefresh: _fetchData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            "REALIZED PROFIT & RECOVERY",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: Colors.white10),
            ),
            child: Column(
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          "Total Recovered",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFormat.format(totalRealized),
                          style: const TextStyle(
                            fontSize: 24,
                            fontWeight: FontWeight.bold,
                            color: Color(0xFF00C853),
                          ),
                        ),
                        Text(
                          kesFormat.format(totalRealized * 130),
                          style: const TextStyle(
                            color: Colors.white70,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        const Text(
                          "This Month",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          usdFormat.format(monthlyRealized),
                          style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        Text(
                          kesFormat.format(monthlyRealized * 130),
                          style: const TextStyle(
                            color: Colors.white70,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
                const SizedBox(height: 24),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      "Target: ${usdFormat.format(targetRecovery)}",
                      style: const TextStyle(color: Colors.grey, fontSize: 12),
                    ),
                    Text(
                      "${(progressPct * 100).toStringAsFixed(1)}%",
                      style: const TextStyle(
                        color: Color(0xFF2962FF),
                        fontWeight: FontWeight.bold,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                LinearProgressIndicator(
                  value: progressPct,
                  backgroundColor: Colors.white10,
                  color: const Color(0xFF2962FF),
                  minHeight: 8,
                  borderRadius: BorderRadius.circular(4),
                ),
              ],
            ),
          ),

          const SizedBox(height: 32),
          const Text(
            "CUMULATIVE EQUITY CURVE",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Container(
            height: 250,
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
                      "Awaiting closed trades to plot curve...",
                      style: TextStyle(color: Colors.white54),
                    ),
                  )
                : LineChart(
                    LineChartData(
                      gridData: FlGridData(
                        show: true,
                        drawVerticalLine: false,
                        getDrawingHorizontalLine: (value) =>
                            const FlLine(color: Colors.white10, strokeWidth: 1),
                      ),
                      titlesData: FlTitlesData(
                        show: true,
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
                            reservedSize: 45,
                            getTitlesWidget: (value, meta) {
                              return Text(
                                '\$${value.toInt()}',
                                style: const TextStyle(
                                  color: Colors.grey,
                                  fontSize: 10,
                                ),
                              );
                            },
                          ),
                        ),
                      ),
                      borderData: FlBorderData(show: false),
                      minX: 0,
                      maxX: (equitySpots.length - 1).toDouble(),
                      minY:
                          (equitySpots
                              .map((s) => s.y)
                              .reduce((a, b) => a < b ? a : b)) -
                          10,
                      maxY:
                          (equitySpots
                              .map((s) => s.y)
                              .reduce((a, b) => a > b ? a : b)) +
                          10,
                      lineBarsData: [
                        LineChartBarData(
                          spots: equitySpots,
                          isCurved: true,
                          color: const Color(0xFF2962FF),
                          barWidth: 3,
                          isStrokeCapRound: true,
                          dotData: const FlDotData(show: false),
                          belowBarData: BarAreaData(
                            show: true,
                            color: const Color(0xFF2962FF).withOpacity(0.15),
                          ),
                        ),
                      ],
                      lineTouchData: LineTouchData(
                        touchTooltipData: LineTouchTooltipData(
                          getTooltipItems: (touchedSpots) {
                            return touchedSpots.map((spot) {
                              final index = spot.x.toInt();
                              final dateStr =
                                  (index >= 0 && index < equityDates.length)
                                  ? equityDates[index]
                                  : '';
                              return LineTooltipItem(
                                '${usdFormat.format(spot.y)}\n$dateStr',
                                const TextStyle(
                                  color: Colors.white,
                                  fontWeight: FontWeight.bold,
                                  fontSize: 12,
                                ),
                              );
                            }).toList();
                          },
                        ),
                      ),
                    ),
                  ),
          ),

          const SizedBox(height: 32),
          const Text(
            "QUANT-GRADE RISK CALCULATOR",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: Colors.white10),
            ),
            child: Column(
              children: [
                TextField(
                  controller: _calcBalanceController,
                  decoration: const InputDecoration(
                    labelText: "Account Balance (\$)",
                  ),
                  keyboardType: TextInputType.number,
                  onChanged: (_) => _calculatePositionSize(),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _calcRiskController,
                        decoration: const InputDecoration(
                          labelText: "Risk (%)",
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calculatePositionSize(),
                      ),
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: TextField(
                        controller: _calcSlController,
                        decoration: const InputDecoration(
                          labelText: "Stop Loss (\$)",
                        ),
                        keyboardType: TextInputType.number,
                        onChanged: (_) => _calculatePositionSize(),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 24),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceAround,
                  children: [
                    Column(
                      children: [
                        const Text(
                          "Required Lot Size",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          _calcLotResult,
                          style: const TextStyle(
                            fontSize: 24,
                            fontWeight: FontWeight.bold,
                            color: Color(0xFF2962FF),
                          ),
                        ),
                      ],
                    ),
                    Column(
                      children: [
                        const Text(
                          "Capital at Risk",
                          style: TextStyle(color: Colors.grey, fontSize: 12),
                        ),
                        Text(
                          _calcExposureResult,
                          style: const TextStyle(
                            fontSize: 24,
                            fontWeight: FontWeight.bold,
                            color: Colors.redAccent,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                const Text(
                  "Asymmetric Floor: Min 0.20 Lots applied",
                  style: TextStyle(color: Colors.grey, fontSize: 12),
                ),
              ],
            ),
          ),

          const SizedBox(height: 32),
          const Text(
            "ACCOUNT AUDITOR",
            style: TextStyle(fontWeight: FontWeight.bold, color: Colors.grey),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: Colors.white10),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  "Export your entire account history directly from MetaTrader 5 into a structured CSV file for offline analysis or tax reporting.",
                  style: TextStyle(color: Colors.white70),
                ),
                const SizedBox(height: 16),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    onPressed: _downloadAuditReport,
                    icon: const Icon(Icons.download),
                    label: const Text("Download CSV Audit"),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFF2962FF),
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(8),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

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
                            const Icon(
                              Icons.warning_amber_rounded,
                              color: Colors.orangeAccent,
                            ),
                            const SizedBox(width: 8),
                            Text(
                              "${event['country']} - ${event['impact']} Impact",
                              style: const TextStyle(
                                fontWeight: FontWeight.bold,
                                color: Colors.orangeAccent,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        Text(
                          event['title'],
                          style: const TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          "Scheduled: ${event['time']}",
                          style: const TextStyle(color: Colors.grey),
                        ),
                        if (event['insight'] != null) ...[
                          const SizedBox(height: 12),
                          Container(
                            padding: const EdgeInsets.all(12),
                            decoration: BoxDecoration(
                              color: Colors.white.withOpacity(0.05),
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Icon(
                                  Icons.lightbulb_outline,
                                  size: 16,
                                  color: Colors.white54,
                                ),
                                const SizedBox(width: 8),
                                Expanded(
                                  child: Text(
                                    event['insight'],
                                    style: const TextStyle(
                                      fontStyle: FontStyle.italic,
                                      color: Colors.white70,
                                    ),
                                  ),
                                ),
                              ],
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
}
