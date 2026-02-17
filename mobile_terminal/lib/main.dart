import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';

void main() {
  runApp(const TradeCoreApp());
}

class TradeCoreApp extends StatelessWidget {
  const TradeCoreApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TradeCore v50',
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
  final List<Widget> _pages = [
    const DashboardPage(),
    const TerminalPage(),
    const CalculatorPage(),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _pages[_currentIndex],
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) => setState(() => _currentIndex = index),
        backgroundColor: const Color(0xFF161B22),
        selectedItemColor: const Color(0xFF00C853),
        unselectedItemColor: Colors.grey,
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.dashboard),
            label: 'War Room',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.terminal),
            label: 'Terminal',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.calculate),
            label: 'Projector',
          ),
        ],
      ),
    );
  }
}

// --- PAGE 1: WAR ROOM DASHBOARD ---
class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key});

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  Timer? _timer;
  Map<String, dynamic>? _data;
  bool _isError = false;

  @override
  void initState() {
    super.initState();
    _fetchStatus();
    _timer = Timer.periodic(
      const Duration(seconds: 1),
      (timer) => _fetchStatus(),
    );
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetchStatus() async {
    try {
      final response = await http.get(
        Uri.parse('http://127.0.0.1:8000/bot/status'),
      );
      if (response.statusCode == 200) {
        setState(() {
          _data = json.decode(response.body);
          _isError = false;
        });
      }
    } catch (e) {
      setState(() => _isError = true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final currency = NumberFormat.simpleCurrency();

    if (_isError)
      return const Center(
        child: Text("⚠️ System Offline", style: TextStyle(color: Colors.red)),
      );
    if (_data == null) return const Center(child: CircularProgressIndicator());

    final account = _data!['account'];
    final positions = _data!['positions'] as List;
    final equity = account['equity'];
    final profit = account['profit'];

    return Scaffold(
      appBar: AppBar(
        title: const Text("v50 Command Center"),
        backgroundColor: Colors.transparent,
        actions: [
          Icon(Icons.wifi, color: const Color(0xFF00C853), size: 16),
          const SizedBox(width: 16),
        ],
      ),
      body: Padding(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          children: [
            // Equity Card
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(24),
              decoration: BoxDecoration(
                color: const Color(0xFF1C2128),
                borderRadius: BorderRadius.circular(16),
                border: Border.all(
                  color: profit >= 0 ? Colors.green : Colors.red,
                ),
              ),
              child: Column(
                children: [
                  const Text(
                    "LIVE EQUITY",
                    style: TextStyle(color: Colors.grey),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    currency.format(equity),
                    style: const TextStyle(
                      fontSize: 36,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  Text(
                    "${profit >= 0 ? '+' : ''}${currency.format(profit)}",
                    style: TextStyle(
                      color: profit >= 0 ? Colors.green : Colors.red,
                      fontSize: 18,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 24),
            // Position List
            const Align(
              alignment: Alignment.centerLeft,
              child: Text(
                "ACTIVE TRADES",
                style: TextStyle(color: Colors.grey),
              ),
            ),
            const SizedBox(height: 12),
            Expanded(
              child: positions.isEmpty
                  ? const Center(
                      child: Text(
                        "Scanning Markets...",
                        style: TextStyle(color: Colors.grey),
                      ),
                    )
                  : ListView.builder(
                      itemCount: positions.length,
                      itemBuilder: (ctx, i) {
                        final pos = positions[i];
                        final pnl = pos['profit'];
                        return Card(
                          color: const Color(0xFF0D1117),
                          child: ListTile(
                            leading: Icon(
                              pos['type'] == 'BUY'
                                  ? Icons.arrow_upward
                                  : Icons.arrow_downward,
                              color: pos['type'] == 'BUY'
                                  ? Colors.green
                                  : Colors.red,
                            ),
                            title: Text(
                              pos['symbol'],
                              style: const TextStyle(
                                fontWeight: FontWeight.bold,
                              ),
                            ),
                            subtitle: Text(
                              "${pos['type']} ${pos['volume']} Lots",
                            ),
                            trailing: Text(
                              currency.format(pnl),
                              style: TextStyle(
                                color: pnl >= 0 ? Colors.green : Colors.red,
                                fontWeight: FontWeight.bold,
                                fontSize: 16,
                              ),
                            ),
                          ),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

// --- PAGE 2: TERMINAL LOGS ---
class TerminalPage extends StatefulWidget {
  const TerminalPage({super.key});

  @override
  State<TerminalPage> createState() => _TerminalPageState();
}

class _TerminalPageState extends State<TerminalPage> {
  List<dynamic> logs = [];
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _fetchLogs();
    _timer = Timer.periodic(
      const Duration(seconds: 1),
      (timer) => _fetchLogs(),
    );
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetchLogs() async {
    try {
      final response = await http.get(
        Uri.parse('http://127.0.0.1:8000/bot/status'),
      );
      if (response.statusCode == 200) {
        setState(() {
          logs = json.decode(response.body)['recent_logs'];
        });
      }
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("System Logs"),
        backgroundColor: Colors.transparent,
      ),
      body: Container(
        margin: const EdgeInsets.all(16),
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: Colors.black,
          borderRadius: BorderRadius.circular(8),
        ),
        child: ListView.builder(
          itemCount: logs.length,
          itemBuilder: (ctx, i) {
            final log = logs[i].toString();
            Color color = Colors.greenAccent;
            if (log.contains("FAIL") || log.contains("REJECTED"))
              color = Colors.red;
            if (log.contains("Scanning")) color = Colors.grey;
            return Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Text(
                log,
                style: TextStyle(
                  color: color,
                  fontFamily: 'Courier',
                  fontSize: 12,
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}

// --- PAGE 3: PROP FIRM CALCULATOR ---
class CalculatorPage extends StatefulWidget {
  const CalculatorPage({super.key});

  @override
  State<CalculatorPage> createState() => _CalculatorPageState();
}

class _CalculatorPageState extends State<CalculatorPage> {
  double _capital = 100000;

  @override
  Widget build(BuildContext context) {
    final currency = NumberFormat.simpleCurrency();
    double monthlyProfit =
        _capital * 0.098; // 9.8% Target based on backend math

    return Scaffold(
      appBar: AppBar(
        title: const Text("Income Projector"),
        backgroundColor: Colors.transparent,
      ),
      body: Padding(
        padding: const EdgeInsets.all(24.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text("FUNDED CAPITAL", style: TextStyle(color: Colors.grey)),
            Text(
              currency.format(_capital),
              style: const TextStyle(fontSize: 32, fontWeight: FontWeight.bold),
            ),
            Slider(
              value: _capital,
              min: 10000,
              max: 2000000,
              divisions: 100,
              activeColor: const Color(0xFF00C853),
              onChanged: (val) => setState(() => _capital = val),
            ),
            const SizedBox(height: 32),
            Container(
              padding: const EdgeInsets.all(24),
              decoration: BoxDecoration(
                color: const Color(0xFF1C2128),
                borderRadius: BorderRadius.circular(16),
              ),
              child: Column(
                children: [
                  const Text(
                    "PROJECTED MONTHLY INCOME",
                    style: TextStyle(color: Colors.grey),
                  ),
                  const SizedBox(height: 12),
                  Text(
                    currency.format(monthlyProfit),
                    style: const TextStyle(
                      fontSize: 40,
                      fontWeight: FontWeight.bold,
                      color: Color(0xFF00C853),
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
