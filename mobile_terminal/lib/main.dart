import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:fl_chart/fl_chart.dart';

// ==========================================================
// ENVIRONMENT CONSTANTS (Set via --dart-define)
// ==========================================================
const String apiUrl = String.fromEnvironment(
  'API_BASE_URL',
  defaultValue: 'http://127.0.0.1:8000',
);
const String apiKey = String.fromEnvironment(
  'API_KEY',
  defaultValue: 'dev-paper',
);

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
      home: const MainDashboard(),
    );
  }
}

class MainDashboard extends StatefulWidget {
  const MainDashboard({super.key});

  @override
  State<MainDashboard> createState() => _MainDashboardState();
}

class _MainDashboardState extends State<MainDashboard> {
  int _currentIndex = 0;
  Map<String, dynamic> statusData = {};
  Map<String, dynamic> perfData = {};
  List<dynamic> newsData = [];
  Timer? _pollingTimer;

  @override
  void initState() {
    super.initState();
    _fetchAllData();
    // Poll the cache-enabled backend every 5 seconds
    _pollingTimer = Timer.periodic(
      const Duration(seconds: 5),
      (_) => _fetchAllData(),
    );
  }

  @override
  void dispose() {
    _pollingTimer?.cancel();
    super.dispose();
  }

  // ==========================================================
  // SECURE API COMMUNICATION
  // ==========================================================
  Future<http.Response> _secureGet(String endpoint) {
    return http.get(
      Uri.parse('$apiUrl$endpoint'),
      headers: {"api-key": apiKey}, // Injected security header
    );
  }

  Future<void> _fetchAllData() async {
    try {
      final statusRes = await _secureGet('/bot/status');
      if (statusRes.statusCode == 200) {
        setState(() => statusData = json.decode(statusRes.body));
      }

      final perfRes = await _secureGet('/bot/performance');
      if (perfRes.statusCode == 200) {
        setState(() => perfData = json.decode(perfRes.body));
      }

      final newsRes = await _secureGet('/bot/news');
      if (newsRes.statusCode == 200) {
        setState(() => newsData = json.decode(newsRes.body));
      }
    } catch (e) {
      debugPrint("API Connection Error: $e");
    }
  }

  Widget _buildBody() {
    switch (_currentIndex) {
      case 0:
        return _buildStatusTab();
      case 1:
        return _buildPerformanceTab();
      case 2:
        return _buildNewsTab();
      case 3:
        return const RiskCalculatorTab();
      default:
        return _buildStatusTab();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'TradeCore v51.0 Master',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        backgroundColor: const Color(0xFF161B22),
        elevation: 0,
        actions: [
          Padding(
            padding: const EdgeInsets.all(16.0),
            child: Row(
              children: [
                Icon(
                  statusData['is_running'] == true
                      ? Icons.circle
                      : Icons.circle_outlined,
                  color: statusData['is_running'] == true
                      ? Colors.greenAccent
                      : Colors.redAccent,
                  size: 12,
                ),
                const SizedBox(width: 8),
                Text(statusData['is_running'] == true ? "ONLINE" : "OFFLINE"),
              ],
            ),
          ),
        ],
      ),
      body: _buildBody(),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) => setState(() => _currentIndex = index),
        backgroundColor: const Color(0xFF161B22),
        selectedItemColor: const Color(0xFF00C853),
        unselectedItemColor: Colors.grey,
        type: BottomNavigationBarType.fixed,
        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.dashboard), label: 'Status'),
          BottomNavigationBarItem(icon: Icon(Icons.show_chart), label: 'Quant'),
          BottomNavigationBarItem(
            icon: Icon(Icons.newspaper),
            label: 'News Guard',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.calculate),
            label: 'Risk Calc',
          ),
        ],
      ),
    );
  }

  // ==========================================================
  // TAB 1: STATUS & PORTFOLIO
  // ==========================================================
  Widget _buildStatusTab() {
    final acc = statusData['account'] ?? {};
    final balance = acc['balance'] ?? 0.0;
    final equity = acc['equity'] ?? 0.0;
    final marginLvl = acc['margin_level'] ?? 0.0;
    final pnl = statusData['total_pnl'] ?? 0.0;
    final activeTrades = (statusData['positions'] as List?)?.length ?? 0;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _buildMetricCard(
          "Account Balance",
          "\$${balance.toStringAsFixed(2)}",
          Icons.account_balance_wallet,
          Colors.blueAccent,
        ),
        _buildMetricCard(
          "Live Equity",
          "\$${equity.toStringAsFixed(2)}",
          Icons.timeline,
          pnl >= 0 ? Colors.greenAccent : Colors.redAccent,
        ),
        _buildMetricCard(
          "Floating P&L",
          "\$${pnl.toStringAsFixed(2)}",
          Icons.attach_money,
          pnl >= 0 ? Colors.greenAccent : Colors.redAccent,
        ),
        Row(
          children: [
            Expanded(
              child: _buildMetricCard(
                "Margin Level",
                "${marginLvl.toStringAsFixed(2)}%",
                Icons.shield,
                Colors.orangeAccent,
              ),
            ),
            Expanded(
              child: _buildMetricCard(
                "Active Trades",
                "$activeTrades / 12",
                Icons.swap_horiz,
                Colors.purpleAccent,
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        const Text(
          "System Telemetry",
          style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: const Color(0xFF161B22),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                "Market Regime: ${statusData['market_regime'] ?? 'CALIBRATING...'}",
                style: const TextStyle(color: Colors.cyanAccent, fontSize: 16),
              ),
              const SizedBox(height: 4),
              Text(
                "25% Dynamic VaR Limit: \$${(statusData['daily_var'] ?? 0.0).toStringAsFixed(2)}",
                style: const TextStyle(color: Colors.orangeAccent),
              ),
            ],
          ),
        ),
      ],
    );
  }

  // ==========================================================
  // TAB 2: QUANTITATIVE PERFORMANCE
  // ==========================================================
  Widget _buildPerformanceTab() {
    final winRate = perfData['win_rate'] ?? 0.0;
    final profitFactor = perfData['profit_factor'] ?? 0.0;
    final totalRealized = perfData['total_realized'] ?? 0.0;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Row(
          children: [
            Expanded(
              child: _buildMetricCard(
                "Win Rate",
                "$winRate%",
                Icons.pie_chart,
                winRate > 40 ? Colors.greenAccent : Colors.orangeAccent,
              ),
            ),
            Expanded(
              child: _buildMetricCard(
                "Profit Factor",
                "$profitFactor",
                Icons.trending_up,
                profitFactor > 1.5 ? Colors.greenAccent : Colors.redAccent,
              ),
            ),
          ],
        ),
        _buildMetricCard(
          "Net Realized Profit",
          "\$${totalRealized.toStringAsFixed(2)}",
          Icons.account_balance,
          totalRealized >= 0 ? Colors.greenAccent : Colors.redAccent,
        ),
        const SizedBox(height: 24),
        const Text(
          "Equity Curve (All Time)",
          style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 16),
        Container(
          height: 300,
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: const Color(0xFF161B22),
            borderRadius: BorderRadius.circular(12),
          ),
          child:
              perfData['curve'] != null &&
                  (perfData['curve'] as List).isNotEmpty
              ? _buildChart(perfData['curve'])
              : const Center(child: Text("Waiting for historical sync...")),
        ),
      ],
    );
  }

  Widget _buildChart(List<dynamic> curveData) {
    List<FlSpot> spots = [];
    for (int i = 0; i < curveData.length; i++) {
      spots.add(
        FlSpot(i.toDouble(), (curveData[i]['profit'] as num).toDouble()),
      );
    }

    return LineChart(
      LineChartData(
        gridData: FlGridData(
          show: true,
          drawVerticalLine: false,
          getDrawingHorizontalLine: (value) =>
              FlLine(color: Colors.white10, strokeWidth: 1),
        ),
        titlesData: FlTitlesData(show: false),
        borderData: FlBorderData(show: false),
        lineBarsData: [
          LineChartBarData(
            spots: spots,
            isCurved: true,
            color: Colors.greenAccent,
            barWidth: 3,
            isStrokeCapRound: true,
            dotData: FlDotData(show: false),
            belowBarData: BarAreaData(
              show: true,
              color: Colors.greenAccent.withOpacity(0.1),
            ),
          ),
        ],
      ),
    );
  }

  // ==========================================================
  // TAB 3: NEWS GUARD (Now with CEO Insights)
  // ==========================================================
  Widget _buildNewsTab() {
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
        final event = newsData[index];
        final isHigh = event['impact'] == 'High';
        return Card(
          margin: const EdgeInsets.only(bottom: 12),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      isHigh ? Icons.warning : Icons.info_outline,
                      color: isHigh ? Colors.redAccent : Colors.orangeAccent,
                    ),
                    const SizedBox(width: 8),
                    Text(
                      "${event['country']} - ${event['time']}",
                      style: const TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Colors.grey,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  event['title'],
                  style: const TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 8),
                // INJECTED SPRINT 1 FEATURE: The CEO Insight
                Container(
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: Colors.blueGrey.withOpacity(0.1),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.lightbulb_outline,
                        size: 16,
                        color: Colors.cyanAccent,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          event['insight'] ?? 'Volatility expected.',
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

  Widget _buildMetricCard(
    String title,
    String value,
    IconData icon,
    Color color,
  ) {
    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Row(
          children: [
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: color.withOpacity(0.1),
                shape: BoxShape.circle,
              ),
              child: Icon(icon, color: color, size: 28),
            ),
            const SizedBox(width: 16),
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: const TextStyle(color: Colors.grey, fontSize: 14),
                ),
                const SizedBox(height: 4),
                Text(
                  value,
                  style: const TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// ==========================================================
// TAB 4: RISK CALCULATOR (Asset Multiplier Engine)
// ==========================================================
class RiskCalculatorTab extends StatefulWidget {
  const RiskCalculatorTab({super.key});
  @override
  State<RiskCalculatorTab> createState() => _RiskCalculatorTabState();
}

class _RiskCalculatorTabState extends State<RiskCalculatorTab> {
  final TextEditingController balanceCtrl = TextEditingController();
  final TextEditingController riskPctCtrl = TextEditingController(text: "2.0");
  final TextEditingController slPipsCtrl = TextEditingController();

  String selectedAssetType = "Standard Forex (EURUSD)";
  double calculatedLot = 0.0;

  final Map<String, double> assetMultipliers = {
    "Standard Forex (EURUSD)": 100000.0,
    "Yen Crosses (USDJPY)": 1000.0,
    "Metals (XAUUSD)": 100.0,
    "Crypto (BTCUSD)": 1.0,
  };

  void _calculateLot() {
    double balance = double.tryParse(balanceCtrl.text) ?? 0.0;
    double riskPct = double.tryParse(riskPctCtrl.text) ?? 0.0;
    double slPips = double.tryParse(slPipsCtrl.text) ?? 0.0;

    if (balance > 0 && riskPct > 0 && slPips > 0) {
      double riskCapital = balance * (riskPct / 100);
      double multiplier = assetMultipliers[selectedAssetType]!;
      // SL Distance conversion based on standard pip formatting
      double slDistance =
          slPips *
          (selectedAssetType.contains("Yen")
              ? 0.01
              : selectedAssetType.contains("Metals")
              ? 0.1
              : 0.0001);
      if (selectedAssetType.contains("Crypto"))
        slDistance = slPips; // Crypto maps 1:1

      double lot = riskCapital / (slDistance * multiplier);
      setState(() => calculatedLot = lot);
    }
  }

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        const Text(
          "Institutional Lot Sizer",
          style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 16),
        DropdownButtonFormField<String>(
          value: selectedAssetType,
          isExpanded: true,
          dropdownColor: const Color(0xFF161B22),
          items: assetMultipliers.keys.map((String key) {
            return DropdownMenuItem<String>(value: key, child: Text(key));
          }).toList(),
          onChanged: (val) => setState(() => selectedAssetType = val!),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: balanceCtrl,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(labelText: "Account Balance (\$)"),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: riskPctCtrl,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(labelText: "Risk Percentage (%)"),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: slPipsCtrl,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(labelText: "Stop Loss (Pips)"),
        ),
        const SizedBox(height: 24),
        ElevatedButton(
          onPressed: _calculateLot,
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFF00C853),
            padding: const EdgeInsets.all(16),
          ),
          child: const Text(
            "CALCULATE EXPOSURE",
            style: TextStyle(
              fontSize: 16,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
        ),
        const SizedBox(height: 24),
        Container(
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            color: const Color(0xFF161B22),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: Colors.blueAccent),
          ),
          child: Column(
            children: [
              const Text(
                "Required Lot Size",
                style: TextStyle(color: Colors.grey),
              ),
              const SizedBox(height: 8),
              Text(
                calculatedLot.toStringAsFixed(2),
                style: const TextStyle(
                  fontSize: 32,
                  fontWeight: FontWeight.bold,
                  color: Colors.white,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
